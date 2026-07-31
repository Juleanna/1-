# -*- coding: utf-8 -*-
"""Быстрая структурная проверка модулей 1С без запуска платформы.

Что проверяет:
  * баланс парных конструкций (Процедура/КонецПроцедуры, Если/КонецЕсли, ...);
  * вызовы общего модуля, для которых нет экспортной функции;
  * запросы к базе внутри циклов (частая причина N+1);
  * дубли одноимённых функций в разных модулях с разным телом;
  * незакрытые области (#Область / #КонецОбласти).

Чего НЕ проверяет: типы, существование реквизитов и метаданных, синтаксис
языка запросов. Для этого нужен BSL Language Server или Конфигуратор —
см. README рядом с этим файлом.

Запуск:
    python проверить_код.py Буфет
    python проверить_код.py            (проверит все папки проекта)
"""
import io
import os
import re
import sys

PAIRS = [
    ('процедура', 'конецпроцедуры'),
    ('функция', 'конецфункции'),
    ('если', 'конецесли'),
    ('цикл', 'конеццикла'),
    ('попытка', 'конецпопытки'),
]

QUERY_CALLS = re.compile(
    r'(ЗначениеРеквизитаОбъекта|ЗначенияРеквизитовОбъекта|Запрос\.Выполнить'
    r'|ВыполнитьПакет|ПолучитьОбъект|НайтиПоНаименованию|НайтиПоКоду)\(')

# Файлы, которые не являются частью решения
SKIP = {'форма_отчета.bsl'}

# Обработчики событий платформы: одинаковые имена в разных модулях — норма,
# сравнивать их тела между объектами бессмысленно
EVENT_HANDLERS = {
    'ПриСозданииНаСервере', 'ПриОткрытии', 'ПередЗаписью', 'ПриЗаписи',
    'ПередЗаписьюНаСервере', 'ПослеЗаписи', 'ПослеЗаписиНаСервере',
    'ОбработкаЗаполнения', 'ОбработкаПроверкиЗаполнения', 'ОбработкаПроведения',
    'ОбработкаОтменыПроведения', 'ПередУдалением', 'ПриКопировании',
    'ПересчитатьСебестоимость', 'ПересчитатьСебестоимостьНаСервере',
    'РассчитатьПищевуюЦенность', 'РассчитатьПищевуюЦенностьНаСервере',
    'СоставИнгредиентовДобавить', 'СоставИнгредиентовУдалить',
    'ПолучитьПищевуюЦенностьИнгредиента', 'ПолучитьПоследнююЦенуПоступления',
    'РассчитатьОбщуюСебестоимость', 'РассчитатьСтоимостьСтроки',
    'ПроверитьНаличиеИнгредиентов', 'ОбновитьВсеЦены', 'ОчиститьВсеЦены',
    'СоставИнгредиентовИнгредиентПриИзменении', 'СоставИнгредиентовНормаБруттоПриИзменении',
    'СоставИнгредиентовНормаНеттоПриИзменении', 'СоставИнгредиентовПроцентОтходовПриИзменении',
}

# Взаимоисключающие варианты одного модуля: в конфигурацию ставится
# только один из пары, поэтому расхождение тел здесь ожидаемо
ALTERNATIVE_PAIRS = [
    ('Справочник_Техкарточки_МодульФормы.bsl',
     'Справочник_Техкарточки_МодульФормы_БезСправочникаЦен.bsl'),
]


def are_alternatives(first, second):
    for left, right in ALTERNATIVE_PAIRS:
        if {first, second} == {left, right}:
            return True
    return False


def strip_code(text):
    """Убирает комментарии и содержимое строковых литералов."""
    without_comments = '\n'.join(re.sub(r'//.*$', '', line) for line in text.split('\n'))
    return re.sub(r'"(?:[^"]|"")*"', '""', without_comments, flags=re.S)


def count_word(text, word):
    return len(re.findall(r'(?<![\w])' + word + r'(?![\w])', text))


def check_balance(name, text, problems):
    clean = strip_code(text).lower()

    for opening, closing in PAIRS:
        left, right = count_word(clean, opening), count_word(clean, closing)
        if left != right:
            problems.append('%s: %s %d != %s %d' % (name, opening, left, closing, right))

    loops = count_word(clean, 'для') + count_word(clean, 'пока')
    ends = count_word(clean, 'цикл')
    if loops != ends:
        problems.append('%s: Для+Пока %d != Цикл %d' % (name, loops, ends))

    regions = len(re.findall(r'^\s*#Область', text, re.M))
    region_ends = len(re.findall(r'^\s*#КонецОбласти', text, re.M))
    if regions != region_ends:
        problems.append('%s: #Область %d != #КонецОбласти %d' % (name, regions, region_ends))


def check_loops(name, text, warnings):
    depth = 0
    for number, line in enumerate(text.split('\n'), 1):
        clean = re.sub(r'//.*$', '', line)
        lowered = clean.lower()
        if re.search(r'(?<![\w])цикл(?![\w])', lowered):
            depth += 1
        if re.search(r'(?<![\w])конеццикла(?![\w])', lowered):
            depth -= 1
        if depth > 0 and QUERY_CALLS.search(clean):
            warnings.append('%s:%d — обращение к базе внутри цикла: %s'
                            % (name, number, clean.strip()[:70]))


# Встроенные функции и конструкторы платформы: вызываются без объявления
BUILTIN = {
    'ВРег', 'НРег', 'ТРег', 'СокрЛП', 'СокрЛ', 'СокрП', 'Лев', 'Прав', 'Сред',
    'СтрДлина', 'СтрЗаменить', 'СтрШаблон', 'СтрСоединить', 'СтрРазделить',
    'СтрНайти', 'СтрЧислоСтрок', 'СтрПолучитьСтроку', 'Найти', 'ПустаяСтрока',
    'Строка', 'Число', 'Дата', 'Булево', 'Формат', 'ТипЗнч', 'Тип',
    'Окр', 'Цел', 'Макс', 'Мин', '邮',
    'НачалоДня', 'КонецДня', 'НачалоМесяца', 'КонецМесяца', 'НачалоГода',
    'КонецГода', 'НачалоНедели', 'ТекущаяДата', 'ТекущаяДатаСеанса',
    'ДобавитьМесяц', 'Год', 'Месяц', 'День',
    'ЗначениеЗаполнено', 'ЗаполнитьЗначенияСвойств', 'ПредопределенноеЗначение',
    'ЭтоНовый', 'Сообщить', 'Вопрос', 'ПоказатьВопрос', 'Предупреждение',
    'ПоказатьПредупреждение', 'ОткрытьФорму', 'ЗакрытьФорму',
    'НачатьТранзакцию', 'ЗафиксироватьТранзакцию', 'ОтменитьТранзакцию',
    'УстановитьПривилегированныйРежим', 'ПривилегированныйРежим',
    'ЗаписьЖурналаРегистрации', 'ОписаниеОшибки', 'ИнформацияОбОшибке',
    'ПодробноеПредставлениеОшибки', 'ВызватьИсключение', 'НСтр',
    'РеквизитФормыВЗначение', 'ЗначениеВРеквизитФормы', 'ПолучитьОбщийМакет',
    'ПолучитьМакет', 'ПравоДоступа', 'ИмяПользователя',
    # конструкторы через Новый
    'Запрос', 'Структура', 'Соответствие', 'Массив', 'ТаблицаЗначений',
    'ОписаниеТипов', 'ОписаниеОповещения', 'Шрифт', 'Цвет', 'ТабличныйДокумент',
    'БлокировкаДанных', 'СписокЗначений', 'ХранилищеЗначения', 'УникальныйИдентификатор',
    'ДиаграммаСерия', 'ДиаграммаТочка', 'Диаграмма',
}

# Свойства и методы, встречающиеся в примерах как часть многострочных выражений
IGNORE_CALLS = {'НастройкиВнешнегоВида', 'НастройкиИнтерактивности'}


def collect_exports(text):
    return set(re.findall(r'^(?:Функция|Процедура)\s+(\w+)\([^)]*\)\s*Экспорт', text, re.M))


def collect_declared(text):
    return set(re.findall(r'^(?:Функция|Процедура)\s+(\w+)\s*\(', text, re.M))


def collect_calls(text):
    """Вызовы без точки перед именем — то есть к функциям своего модуля."""
    clean = strip_code(text)
    return set(re.findall(r'(?<![\w.])([А-ЯЁІЇЄA-Z][\wА-Яа-яЁёІіЇїЄє]{2,})\s*\(', clean))


def check_local_calls(name, text, declared, problems):
    """Ищет вызовы функций, которых нет ни в модуле, ни среди встроенных.

    Именно так ловится опечатка и переименование: функцию переименовали,
    а вызов в другой процедуре остался со старым именем.
    """
    for called in sorted(collect_calls(text) - declared - BUILTIN - IGNORE_CALLS):
        problems.append('%s: вызов %s() — функция не объявлена в модуле '
                        'и не является встроенной' % (name, called))


def function_bodies(text):
    """Возвращает {имя: нормализованное тело} для сравнения дублей."""
    result = {}
    pattern = re.compile(r'^(?:&\w+\s*\n)?(?:Функция|Процедура)\s+(\w+)\(.*?^Конец(?:Функции|Процедуры)',
                         re.M | re.S)
    for match in pattern.finditer(text):
        body = re.sub(r'//.*$', '', match.group(0), flags=re.M)
        result[match.group(1)] = re.sub(r'\s+', ' ', body).strip()
    return result


def main():
    # Консоль Windows по умолчанию не в UTF-8 — кириллица иначе не читается
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    root = sys.argv[1] if len(sys.argv) > 1 else '.'

    modules = {}
    for folder, _, files in os.walk(root):
        if '.git' in folder or 'bufet' in folder.replace('\\', '/'):
            continue
        for name in sorted(files):
            if name.endswith('.bsl') and name not in SKIP:
                path = os.path.join(folder, name)
                modules[path] = io.open(path, encoding='utf-8').read()

    if not modules:
        print('Модули не найдены в %s' % root)
        return 1

    problems, warnings = [], []
    exports, bodies = {}, {}

    for path, text in modules.items():
        name = os.path.basename(path)
        check_balance(name, text, problems)
        check_loops(name, text, warnings)
        check_local_calls(name, text, collect_declared(text), problems)
        exports[name] = collect_exports(text)
        bodies[name] = function_bodies(text)

    # Вызовы общих модулей
    common = {name[:-4] for name in exports if 'Модуль' in name and 'Форм' not in name}
    for path, text in modules.items():
        name = os.path.basename(path)
        for module in common:
            if module + '.bsl' == name:
                continue
            for called in set(re.findall(re.escape(module) + r'\.(\w+)', text)):
                if called not in exports.get(module + '.bsl', set()):
                    problems.append('%s: вызов %s.%s — нет экспортной функции'
                                    % (name, module, called))

    # Дубли функций с разным телом.
    # Обработчики событий пропускаем: одинаковые имена в модулях разных
    # объектов — это норма платформы, а не копипаста.
    seen = {}
    for name, functions in bodies.items():
        for function, body in functions.items():
            if function in EVENT_HANDLERS:
                continue
            if function in seen and seen[function][1] != body \
                    and not are_alternatives(seen[function][0], name):
                warnings.append('Функция %s различается в %s и %s — тела копий разошлись'
                                % (function, seen[function][0], name))
            seen.setdefault(function, (name, body))

    print('Проверено модулей: %d' % len(modules))
    print()

    if problems:
        print('ОШИБКИ (%d):' % len(problems))
        for item in problems:
            print('  ' + item)
        print()

    if warnings:
        print('ПРЕДУПРЕЖДЕНИЯ (%d):' % len(warnings))
        for item in warnings:
            print('  ' + item)
        print()

    if not problems and not warnings:
        print('Замечаний нет.')

    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
