#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
command_builder.py — Генерация команд для C-Data из строки таблицы.

На основе маппинга: старый адрес, MAC, новый VLAN, S/N →
строит конфигурационную команду для C-Data OLT.
"""

from log_utils import Logger
from config_loader import get as _cfg

import re as _re

def _sanitize_cli(value: str) -> str:
    """
    Очистить строку от символов, ломающих CLI C-Data.
    Команды C-Data чувствительны к кавычкам, точкам с запятой,
    обратным слешам и не-ASCII пробелам.
    Оставляет: буквы, цифры, дефис, подчёркивание, точку, слеш, пробел.
    """
    if not value:
        return ""
    return _re.sub(r'[^\w\s.\-/:]', '', value).strip()

# Service VLAN для каждого C-Data OLT (из config.toml)
def _get_service_vlan() -> dict:
    """Загрузить service_vlan из config.toml."""
    sv = _cfg("cdata", "service_vlan", default={})
    if not sv:
        # fallback на случай отсутствия в конфиге
        return {
            "172.18.0.200": 885, "172.18.0.201": 887,
            "172.18.0.202": 889, "172.18.0.203": 891,
            "172.18.0.204": 893, "172.18.0.205": 895,
        }
    return sv


def build_formula(row: dict, slot: dict, log: Logger) -> str:
    """
    Собрать команду C-Data из данных строки таблицы + свободного слота.

    Формат из эталонного файла (4 команды на абонента):
      1. ont add {port} {ont_id} sn-auth {sn} ont-lineprofile-id 1 ont-srvprofile-id 1
      2. ont description {port} {ont_id} {description}
      3. ont port native-vlan {port} {ont_id} eth 1 vlan {vlan} priority 0
      4. ont port attribute {port} {ont_id} eth 1 igmp-profile profile-id 1

    row — словарь с ключами: mac, new_vlan, sn, desc
    slot — словарь: {"port": trunk, "ont_id": id}
    """
    new_vlan = row.get("new_vlan", "")
    sn = row.get("sn", "")
    desc = row.get("desc", "") or row.get("description", "") or ""
    contract = row.get("contract", "")

    # Формат описания: если desc уже начинается с цифр — не трогаем
    # (значит номер договора уже есть в описании, как "22600092_Lesnaya_18")
    # Если нет — добавляем договор спереди: "20720005_Surikova_13"
    import re
    # Если desc уже содержит префикс (цифры, ЮТС, ЮЛС, ULS) — не трогаем
    has_prefix = bool(re.match(r'^\d', desc)) or 'ЮТС-' in desc or 'ЮЛС-' in desc or 'ULS-' in desc
    if contract and not has_prefix:
        # ЮЛС/ЮТС → ULS_XXXX
        if re.match(r'Ю[ЛТ]С-', contract):
            ul = re.sub(r'Ю[ЛТ]С-', 'ULS_', contract)
            desc_full = f"{ul}_{desc}"
        else:
            desc_full = f"{contract}_{desc}"
    else:
        # ЮЛС/ЮТС/ULS в desc → заменяем на ULS_
        desc_full = re.sub(r'(?:Ю[ЛТ]С|ULS)-', 'ULS_', desc) if ('Ю' in desc or 'ULS-' in desc) else desc

    port = slot.get("port", "0")
    ont_id = slot.get("ont_id", 0)

    # Санитизация: убираем символы, ломающие CLI C-Data
    sn_safe = _sanitize_cli(sn)
    desc_safe = _sanitize_cli(desc_full)

    # 1. Добавление ONT по SN
    cmd_add = (
        f"ont add {port} {ont_id} sn-auth {sn_safe} "
        f"ont-lineprofile-id 1 ont-srvprofile-id 1"
    )

    # 2. Описание абонента
    cmd_desc = f"ont description {port} {ont_id} {desc_safe}"

    # 3. Native VLAN (есть в примере: ont port native-vlan 4 22 eth 1 vlan 2999 priority 0)
    # Native VLAN: для PPPoE (5/100) оставляем как есть, для остальных — новый VLAN
    if new_vlan in ("5", "100", ""):
        vlan_for_native = new_vlan if new_vlan else "5"
    else:
        vlan_for_native = new_vlan
    cmd_native = f"ont port native-vlan {port} {ont_id} eth 1 vlan {vlan_for_native} priority 0"

    # 4. IGMP (есть в примере: ont port attribute 4 22 eth 1 igmp-profile profile-id 1)
    cmd_igmp = f"ont port attribute {port} {ont_id} eth 1 igmp-profile profile-id 1"

    # 5. Service-port: настройка тегирования
    # Service VLAN загружается из config.toml при каждом вызове
    _sv_map = _get_service_vlan()
    svlan = str(_sv_map.get(row.get("olt_cdata", ""), 887))
    if new_vlan in ("5", "100", ""):
        svlan = "5"
        tag_action = "transparent"
        user_vlan = "5"
    else:
        svlan = str(_sv_map.get(row.get("olt_cdata", ""), 887))
        tag_action = "default"
        user_vlan = new_vlan

    cmd_service = (
        f"service-port autoindex vlan {svlan} "
        f"gpon 0/0 port {port} ont {ont_id} gemport 1 "
        f"multi-service user-vlan {user_vlan} tag-action {tag_action}"
    )

    formula = f"{cmd_add}\n{cmd_desc}\n{cmd_native}\n{cmd_igmp}\n{cmd_service}"

    log.debug(f"Формула [{port}/{ont_id}]: {desc}")
    return formula


def build_commands_batch(data: list[dict], log: Logger,
                          free_slots: list[dict] = None) -> list[str]:
    """
    Собрать список команд C-Data для всех абонентов.

    Слот БЕРЁТСЯ из row['chan_cdata'], а не из free_slots по индексу,
    чтобы номер слота в формуле совпадал с колонкой H.
    """
    log.info(f"Сборка команд C-Data для {len(data)} абонентов")

    commands = []
    import re

    for i, row in enumerate(data, 1):
        new_vlan = row.get("new_vlan", "")
        orig_idx = i  # номер в исходных данных

        if not new_vlan or new_vlan == "?":
            reason = "нет нового VLAN" if not new_vlan else "VLAN=? (требует проверки)"
            commands.append(f"; [{orig_idx}] {row.get('desc', '')} — {reason}")
            continue

        # Извлекаем порт и ont_id из row['chan_cdata'] (формат: "5 57" или "5/57")
        chan = row.get("chan_cdata", "")
        # Пробуем сначала пробел, потом слэш (на случай, если шаг 7 не отработал)
        m = re.match(r'(\d+)\s+(\d+)', chan)
        if not m:
            m = re.match(r'(\d+)/(\d+)', chan)
        if not m:
            log.warn(f"  [{orig_idx}] Неверный формат слота: '{chan}'")
            commands.append(f"; [{orig_idx}] {row.get('desc', '')} — неверный слот '{chan}'")
            continue

        port, ont_id = m.group(1), m.group(2)
        slot = {"port": port, "ont_id": ont_id}
        formula = build_formula(row, slot, log)

        commands.append(formula)
        tag_type = "transparent" if new_vlan in ("5", "") else "default"
        log.debug(f"  [{orig_idx}] слот {port}/{ont_id}: "
                  f"VLAN={new_vlan} ({tag_type})")

    log.info(f"Собрано команд: {len(commands)}")
    return commands


def format_for_notepad(commands: list[str]) -> str:
    """
    Преобразовать команды в формат для Блокнота.
    Каждый элемент commands — либо команда, либо комментарий "; [...] пропущен".
    """
    lines = []
    for i, cmd in enumerate(commands, 1):
        lines.append(f"; === Абонент {i} ===")
        lines.append("enable")
        lines.append("config")
        lines.append(cmd)
        lines.append("exit")
        lines.append("")
    return "\n".join(lines)
