#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config_loader.py — Загрузчик конфигурации из config.toml.

Все настройки: пароли, IP, service_vlan, skip_first_vlans.
Единая точка доступа — нигде в коде не должно быть хардкода.
"""

import os
import re
from typing import Any

_TOML_CACHE = None


def _find_config() -> str:
    """Ищет config.toml рядом со скриптом или на уровень выше."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for candidate in [
        os.path.join(script_dir, "config.toml"),
        os.path.join(os.path.dirname(script_dir), "config.toml"),
    ]:
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError(f"config.toml не найден в {script_dir}")


def _parse_toml(text: str) -> dict:
    """
    Примитивный TOML-парсер без внешних зависимостей.
    Поддерживает: секции, строки (в кавычках), числа, inline-таблицы, комментарии.
    """
    result = {}
    current_section = result
    section_path = []
    _inline_key = None
    _inline_lines = []
    _in_inline = False
    _brace_depth = 0

    for line in text.split("\n"):
        line_stripped = line.strip()
        # Комментарий или пустая строка (если не внутри inline-таблицы)
        if not _in_inline and (not line_stripped or line_stripped.startswith("#")):
            continue

        # Секция [section] или [section.sub] (только если не внутри inline)
        if not _in_inline:
            m = re.match(r'^\[([^\]]+)\]$', line_stripped)
            if m:
                section_path = m.group(1).split(".")
                current_section = result
                for part in section_path:
                    part = part.strip()
                    if part not in current_section:
                        current_section[part] = {}
                    current_section = current_section[part]
                continue

        # Ключ = значение
        m = re.match(r'^([^=]+)=\s*(.+)$', line if _in_inline else line_stripped)
        if m:
            key = m.group(1).strip()
            raw_val = m.group(2).strip()

            # Начало inline-таблицы { ... }
            if raw_val.startswith("{"):
                _inline_key = key
                _inline_lines = [raw_val]
                _in_inline = True
                _brace_depth = raw_val.count("{") - raw_val.count("}")
                if _brace_depth <= 0:
                    # Однострочная inline-таблица
                    _in_inline = False
                    _inline_key = None
                    current_section[key] = _parse_inline_table(raw_val)
                continue

            if not _in_inline:
                current_section[key] = _parse_value(raw_val)
            else:
                _inline_lines.append(line)
                _brace_depth += line.count("{") - line.count("}")
                if _brace_depth <= 0:
                    # inline-таблица завершена
                    full = " ".join(_inline_lines)
                    current_section[_inline_key] = _parse_inline_table(full)
                    _in_inline = False
                    _inline_key = None
                    _inline_lines = []
                    _brace_depth = 0
                    _inline_lines = []
        elif _in_inline:
            # Продолжение inline-таблицы (строка без '=')
            _inline_lines.append(line)
            _brace_depth += line.count("{") - line.count("}")
            if _brace_depth <= 0:
                full = " ".join(_inline_lines)
                current_section[_inline_key] = _parse_inline_table(full)
                _in_inline = False
                _inline_key = None
                _inline_lines = []
                _brace_depth = 0
                _inline_lines = []

    return result


def _parse_inline_table(raw: str) -> dict:
    """Распарсить inline-таблицу { k1 = v1, k2 = v2 }."""
    tbl = {}
    # Убираем внешние { }
    inner = raw.strip()
    if inner.startswith("{"):
        inner = inner[1:]
    if inner.endswith("}"):
        inner = inner[:-1]
    inner = inner.strip()

    if not inner:
        return tbl

    # Разбиваем по запятым, учитывая кавычки
    parts = []
    current = ""
    in_quotes = False
    quote_char = None
    for ch in inner:
        if ch in ('"', "'"):
            if not in_quotes:
                in_quotes = True
                quote_char = ch
            elif ch == quote_char:
                in_quotes = False
                quote_char = None
            current += ch
        elif ch == "," and not in_quotes:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())

    for pair in parts:
        if not pair:
            continue
        km = re.match(r'^\s*(?:"([^"]*)"|([^=]+))?\s*=\s*(.+)$', pair)
        if km:
            k = km.group(1) or km.group(2) or ""
            k = k.strip()
            v = _parse_value(km.group(3).strip())
            tbl[k] = v

    return tbl


def _parse_value(raw: str) -> Any:
    """Распарсить значение: строка, число, bool."""
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        try:
            return float(raw)
        except ValueError:
            return raw


def load() -> dict:
    """Загрузить и закешировать config.toml."""
    global _TOML_CACHE
    if _TOML_CACHE is not None:
        return _TOML_CACHE

    path = _find_config()
    with open(path, "r", encoding="utf-8") as f:
        _TOML_CACHE = _parse_toml(f.read())
    return _TOML_CACHE


def get(*keys: str, default: Any = None) -> Any:
    """
    Достать значение из конфига по цепочке ключей.
    Пример: get("userside", "login") → "support_aksai"
            get("generation", "skip_first_vlans") → 100
    """
    cfg = load()
    val = cfg
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k)
            if val is None:
                return default
        else:
            return default
    return val if val is not None else default


def reload():
    """Принудительно перечитать config.toml."""
    global _TOML_CACHE
    _TOML_CACHE = None
    return load()
