# -*- coding: utf-8 -*-
"""Быстрая структурная проверка модулей 1С без запуска платформы.

Что проверяет:
  * баланс парных конструкций (Процедура/КонецПроцедуры, Если/КонецЕсли, ...);
  * вызовы общего модуля, для которых нет экспортной функции;
  * многострочные строковые литералы без «|» в начале строк продолжения;
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
    # Одноимённые обработчики команд формы и методы модуля объекта
    # одной и той же обработки — это разные вещи, а не копипаста
    'ВыполнитьДиагностику', 'ПроверитьЕдиницы', 'КонтрольныеПоказатели',
    'СоставИнгредиентовИнгредиентПриИзменении', 'СоставИнгредиентовНормаБруттоПриИзменении',
    'СоставИнгредиентовНормаНеттоПриИзменении', 'СоставИнгредиентовПроцентОтходовПриИзменении',
}

# Взаимоисключающие варианты одного модуля: в конфигурацию ставится
# только один из пары, поэтому расхождение тел здесь ожидаемо
ALTERNATIVE_PAIRS = [
    ('Справочник_Техкарточки_МодульФормы.bsl',
     'Справочник_Техкарточки_МодульФормы_БезСправочникаЦен.bsl'),
]

# Соответствие модулей форм их выгрузкам: нужно, чтобы проверить,
# что обработчики событий действительно привязаны к элементам формы
FORM_SOURCES = {
    'Справочник_Техкарточки_МодульФормы.bsl':
        'bufet/Catalogs/Техкарточки/Forms/ФормаЭлемента/Ext/Form.xml',
    'Справочник_КулинарныеРецепты.bsl':
        'bufet/Catalogs/КулинарныеРецепты/Forms/ФормаЭлемента/Ext/Form.xml',
    'Форма_ПриготовлениеБлюда.bsl':
        'bufet/Documents/ПриготовлениеБлюда/Forms/ФормаДокумента/Ext/Form.xml',
}


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


def check_multiline_strings(name, text, problems):
    """Каждая строка продолжения строкового литерала обязана начинаться с «|».

    Пустая строка или текст без «|» внутри литерала — синтаксическая ошибка,
    которую Конфигуратор показывает уже после вставки модуля, а сам модуль
    при этом не компилируется целиком: перестают работать все команды формы.
    """
    lines = text.split('\n')
    inside = False
    started_at = 0

    for number, line in enumerate(lines, 1):
        code = re.sub(r'//.*$', '', line) if not inside else line

        if inside:
            if not code.lstrip().startswith('|'):
                # Дальше разбирать бессмысленно: кавычки уже разъехались
                # и каждая следующая строка дала бы ложное срабатывание
                problems.append(
                    '%s, строка %d: продолжение строкового литерала (открыт в строке %d) '
                    'не начинается с «|»' % (name, number, started_at))
                return
            code = code.lstrip()[1:]

        # Нечётное число кавычек означает, что литерал остался открытым
        if code.count('"') % 2 == 1:
            if not inside:
                started_at = number
            inside = not inside

    if inside:
        problems.append('%s: строковый литерал, открытый в строке %d, не закрыт' % (name, started_at))


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
    'ЭтоНовый', 'Сообщить', 'ОчиститьСообщения', 'Вопрос', 'ПоказатьВопрос', 'Предупреждение',
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


def form_bindings(form_xml):
    """Имена процедур, привязанных к событиям и командам формы."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.parse(form_xml).getroot()
    except Exception:
        return None

    def local(tag):
        return tag.rsplit('}', 1)[-1]

    bound = set()
    for node in root.iter():
        if local(node.tag) == 'Event':
            for child in node.iter():
                if child.text and child.text.strip():
                    bound.add(child.text.strip())
                    break
        elif local(node.tag) == 'Command':
            name = node.attrib.get('name')
            if name:
                bound.add(name)
            for child in node:
                if local(child.tag) == 'Action' and child.text:
                    bound.add(child.text.strip())
    return bound


def check_form_handlers(module_name, text, form_xml, warnings):
    """Обработчики событий, которые есть в модуле, но не привязаны в форме.

    Такой код не вызывается: событие проходит мимо, а внешне всё выглядит
    рабочим. Проверяется только если рядом есть выгрузка формы.
    """
    bound = form_bindings(form_xml)
    if bound is None:
        return

    handlers = set(re.findall(r'^Процедура\s+(\w+)\(Элемент', text, re.M))
    handlers |= set(re.findall(r'^Процедура\s+(\w+)\(Команда\)', text, re.M))

    for orphan in sorted(handlers - bound):
        warnings.append('%s: обработчик %s() не привязан ни к событию, '
                        'ни к команде формы — не вызывается' % (module_name, orphan))


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
        check_multiline_strings(name, text, problems)
        check_loops(name, text, warnings)
        check_local_calls(name, text, collect_declared(text), problems)
        exports[name] = collect_exports(text)
        bodies[name] = function_bodies(text)

        form_xml = FORM_SOURCES.get(name)
        if form_xml:
            full = os.path.join(os.path.dirname(path), os.path.basename(form_xml)) \
                if os.path.isabs(form_xml) else os.path.join(os.path.dirname(path), form_xml)
            if os.path.exists(full):
                check_form_handlers(name, text, full, warnings)

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
