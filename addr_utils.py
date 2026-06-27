#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
addr_utils.py — Нормализация и сравнение адресов.

Единый модуль для всех операций с адресами.
Используется orchestrator.py, billing_client.py, check_full.py и др.

Алгоритм нормализации (_norm_addr):
  1. lowercase + trim
  2. разделители → пробелы
  3. удаление префиксов (г, ул, дом, кв, …)
  4. удаление окончаний регионов (-ский, -цкий, -град, …)
  5. удаление почтовых индексов (6 цифр)
  6. поиск номера дома (первое слово с цифрой)
  7. извлечение квартиры (кв N)

Сравнение (_match_addr):
  1. точное совпадение
  2. без точек
  3. алиас "николая"
  4. алиас "карла" ↔ "к"
  5. совпадение множеств слов
"""

import re


def norm_addr(a: str) -> str:
    """Нормализовать адрес: убрать префиксы, регионы, индексы, выделить дом+кв."""
    if not a:
        return ""
    a = a.lower().strip(" ,.")
    a = a.replace(",", " ").replace(".", " ")
    a = re.sub(r'\s+', ' ', a).strip()
    words = a.split()
    if not words:
        return ""

    # Префиксы — слова, которые ничего не значат для сравнения
    _prefixes = {"россия", "рф", "обл", "область", "р-н", "район",
                 "г", "город", "ул", "улица", "дом", "д", "пос",
                 "поселок", "снт", "п", "хутор", "тер", "ао", "край",
                 "пер", "переулок", "проспект", "пр-кт", "шоссе",
                 "бульвар", "проезд", "площадь", "тупик", "аллея",
                 "№", "номер", "корпус", "корп", "строение", "стр",
                 "оф", "пом", "этаж", "ком",
                 # Города и микрорайоны (НО не "кв" — он обрабатывается отдельно в конце)
                 "сальск", "заречный", "капустино", "кучурда", "кучур-да",
                 "новосальск", "низовский"}
    words = [w for w in words if w not in _prefixes]

    # Окончания регионов (только известные регионы/города, а не похожие улицы)
    _region_words = {"ростовская", "ростовский", "сальский", "сальская",
                     "донской", "донская"}
    words = [w for w in words if w not in _region_words]

    # Почтовые индексы (6 цифр)
    words = [w for w in words if not (w.isdigit() and len(w) == 6)]

    if not words:
        return ""

    # Нормализуем слэши
    a = re.sub(r'\s*/\s*', '/', " ".join(words))
    words = a.split()

    # Ищем первое слово с цифрой — это номер дома
    house_idx = -1
    for i, w in enumerate(words):
        if re.search(r'\d', w):
            house_idx = i
            break

    if house_idx >= 0:
        base = " ".join(words[max(0, house_idx - 1):])
    elif len(words) >= 2:
        base = " ".join(words[-2:])
    else:
        base = " ".join(words)

    # Извлекаем квартиру
    m = re.search(r'(.*)\bкв\s*(\d[\d/]*)$', base)
    if m:
        return m.group(1).strip() + ' кв ' + m.group(2)
    return base


def _normalize_yo(s: str) -> str:
    """Заменить ё на е для сравнения."""
    return s.replace('ё', 'е').replace('Ё', 'Е')


def _extract_kv(s: str) -> tuple[str, str]:
    """
    Извлечь номер квартиры из нормализованного адреса.
    Возвращает (адрес_без_кв, номер_кв).
    """
    m = re.search(r'\bкв\s*(\d[\d/]*)\s*$', s)
    if m:
        addr_wo = s[:m.start()].strip()
        return addr_wo, m.group(1)
    return s, ""


def match_addr(a: str, b: str) -> bool:
    """
    Сравнить два нормализованных адреса с учётом алиасов.
    a и b — уже нормализованные через norm_addr().
    """
    if not a or not b:
        return False
    a = a.lower()
    b = b.lower()

    # Нормализация ё → е
    a = _normalize_yo(a)
    b = _normalize_yo(b)

    if a == b:
        return True

    # Проверка по квартире: если в обоих адресах есть "кв N",
    # номера квартир должны совпадать, иначе адреса разные
    a_wo_kv, a_kv = _extract_kv(a)
    b_wo_kv, b_kv = _extract_kv(b)
    if a_kv and b_kv and a_kv != b_kv:
        return False

    # Дальше сравниваем без учёта квартиры
    a = a_wo_kv
    b = b_wo_kv

    # Убираем точки (к.маркса → к маркса)
    def _clean(s):
        return re.sub(r'\s+', ' ', s.replace('.', ' ')).strip()
    ca, cb = _clean(a), _clean(b)
    if ca == cb:
        return True

    # Убираем "николая" (николая островского → островского)
    ca = re.sub(r'\bниколая\s+', '', ca)
    cb = re.sub(r'\bниколая\s+', '', cb)
    if ca == cb:
        return True

    # "карла" ↔ "к" (карла маркса → к маркса)
    ca = re.sub(r'\bкарла\b', 'к', ca)
    cb = re.sub(r'\bкарла\b', 'к', cb)
    if ca == cb:
        return True

    # Проверяем вхождение: одно содержит другое
    # НО только если у обоих адресов есть номер дома (иначе "новостройка" ложно совпадёт
    # с "новостройка 14/181" через issubset)
    a_has_num = bool(re.search(r'\d', ca))
    b_has_num = bool(re.search(r'\d', cb))
    if a_has_num != b_has_num:
        return False
    a_words = set(ca.split())
    b_words = set(cb.split())
    if a_words == b_words or a_words.issubset(b_words) or b_words.issubset(a_words):
        return True
    return False


def has_house_number(a: str) -> bool:
    """Проверить, содержит ли адрес номер дома (цифру)."""
    if not a:
        return False
    # Извлекаем номер дома после нормализации
    normed = norm_addr(a)
    return bool(re.search(r'\d', normed))


def extract_house_number(a: str) -> str:
    """Извлечь номер дома из адреса (включая буквенный суффикс: 2а, 74а)."""
    normed = norm_addr(a)
    m = re.search(r'(\d[\d/]*[а-яa-z]?)', normed)
    return m.group(1) if m else ""
