#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
orchestrator.py — Центральный оркестратор миграции.

Связывает все модули в единый конвейер обработки.
Каждый шаг логируется. Прогресс сообщается через callback.

Цепочка шагов (по ТЗ):
  1. Загрузить CSV → список абонентов
  2. Подготовить таблицу (добавить колонки G-R)
  3. Запросить старые VLANы с OLT (Eltex) → колонка I
  4. Поиск адреса в US по MAC → колонка J + жёлтая подсветка
  5. Запрос в биллинг (VLAN, договор, телефон) → K, L, M + оранж/красн
  6. Новые VLANы из биллинга → колонка N
  7. Свободные слоты на C-Data
  8. S/N сканирование + печать этикетки
  9. Сборка формул → P
  10. Сохранение: итоговая таблица + 3 выгрузки + файл команд
"""

import os
import re
import time
import concurrent.futures
from typing import Optional, Callable
from log_utils import Logger
from excel_utils import (
    read_input_csv, olt_name_to_ip, write_output_table,
    save_export_tables, save_notepad,
    save_commands_to_word
)
from billing_client import BillingClient
from us_client import UsersideClient
from eltex_client import EltexClient
from cdata_client import CDataClient
from command_builder import build_commands_batch, format_for_notepad
from label_printer import print_label


class MigrationOrchestrator:
    """
    Оркестратор процесса миграции.
    Запускает шаги последовательно, передавая прогресс.
    """

    def __init__(self, log: Logger,
                 on_progress: Optional[Callable[[int, int, str], None]] = None,
                 on_color_row: Optional[Callable[[str, int, dict], bool]] = None,
                 on_confirm_colors: Optional[Callable[[list], str]] = None,
                 on_ask_sn: Optional[Callable[[str, str], str]] = None):
        """
        on_progress(current, total, message) — обновление прогресса
        on_color_row(color, row_index, row_data) — запрос продолжения при цветных строках
            Возвращает True = продолжать, False = остановиться
        """
        self.log = log
        self.on_progress = on_progress
        self.on_color_row = on_color_row
        self.on_confirm_colors = on_confirm_colors
        self.on_ask_sn = on_ask_sn
        self._sn_queue = __import__('queue').Queue()
        self._skip_label_print = False

        # Клиенты (инициализируются при start)
        self.billing: Optional[BillingClient] = None
        self.userside: Optional[UsersideClient] = None
        self.eltex: Optional[EltexClient] = None
        self.cdata: Optional[CDataClient] = None

        # Параметры миграции
        self.from_olt = ""      # например "Salsk107"
        self.from_chan = ""     # например "3"
        self.to_olt_ip = ""     # например "172.18.0.200"
        self.to_chan = ""       # например "5"
        self.header = ""        # например "107/3 на 200/5"

        # Результат
        self.data: list[dict] = []

        # Прогресс-бар (привязка к числу абонентов)
        self._progress_cur: int = 0
        self._progress_total: int = 100
        self._progress_lock = __import__('threading').Lock()

        # Оценка времени — плавный ETA от elapsed time
        self._progress_start_time: float = 0.0
        self._progress_wait_start: float = 0.0

    def _progress_pause(self):
        """Приостановить таймер ETA — ожидание ввода пользователя."""
        self._progress_wait_start = __import__('time').time()

    def _progress_resume(self):
        """Возобновить таймер — вычесть время ожидания из elapsed."""
        if self._progress_wait_start:
            paused = __import__('time').time() - self._progress_wait_start
            self._progress_start_time += paused
            self._progress_wait_start = 0.0

    def _progress(self, current: int, total: int, message: str):
        self.log.info(f"[{current}/{total}] {message}")
        if self.on_progress:
            try:
                self.on_progress(current, total, message)
            except Exception:
                pass

    def _inc_progress(self, step: int = 1, message: str = ""):
        """
        Увеличить счётчик прогресса на step (потокобезопасно).
        ETA = elapsed * (total - cur) / cur — плавный отсчёт с первой секунды.
        """
        with self._progress_lock:
            self._progress_cur += step
            cur = self._progress_cur
            total = self._progress_total
        now = __import__('time').time()
        if self._progress_start_time == 0:
            self._progress_start_time = now
        elapsed = now - self._progress_start_time
        # Плавный ETA: экстраполяция от elapsed времени
        eta_sec = int(elapsed * (total - cur) / max(cur, 1))
        # Первые 5 тиков — подстраховка: не ниже ~5 сек на тик
        if cur <= 5:
            default_eta = total * 5  # грубая оценка: 5 сек на шаг
            eta_sec = max(eta_sec, default_eta)
        if eta_sec > 0:
            if eta_sec < 60:
                message += f" [осталось ~{eta_sec}с]"
            elif eta_sec < 3600:
                message += f" [осталось ~{eta_sec//60}мин {eta_sec%60}с]"
            else:
                message += f" [осталось ~{eta_sec//3600}ч {(eta_sec%3600)//60}мин]"
        pct = min(int(cur / max(total, 1) * 100), 100)
        self.log.info(f"[{cur}/{total}] {message}")
        if self.on_progress:
            try:
                self.on_progress(cur, total, message)
            except Exception:
                pass

    def _need_continue(self, color: str, row_index: int, row_data: dict) -> bool:
        """Спросить пользователя — продолжать ли при цветных строках."""
        self.log.warn(f"Строка {row_index}: цвет {color.upper()}")
        if self.on_color_row:
            try:
                return self.on_color_row(color, row_index, row_data)
            except Exception:
                return False
        return True

    # ── Параллельный исполнитель ──

    def _parallel_map(self, func, items, max_workers=3, desc="") -> list:
        """
        Запустить func(i, item) для каждого item в parallel.
        Возвращает список результатов в исходном порядке.
        """
        results = [None] * len(items)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            fut_map = {ex.submit(func, i, item): i for i, item in enumerate(items)}
            for future in concurrent.futures.as_completed(fut_map):
                idx = fut_map[future]
                try:
                    results[idx] = future.result()
                except Exception as e:
                    self.log.warn(f"{desc} [{idx}]: {e}")
                    results[idx] = None
        return results

    # ── Основной запуск ──

    def start(self,
              input_path: str,
              output_dir: str,
              from_olt: str, from_chan: str,
              to_olt_name: str, to_chan: str,
              billing_login: str, billing_password: str):
        """
        Запустить полный цикл миграции.

        Параметры:
          input_path — путь к CSV/XLSX
          output_dir — куда сохранять результаты
          from_olt — имя исходного OLT (Salsk107)
          from_chan — исходный ствол (3)
          to_olt_name — номер целевого OLT (200) или IP
          to_chan — целевой ствол (5)
          billing_login / billing_password — для авторизации в биллинге
        """
        start_time = time.time()
        self.log.section("ЗАПУСК МИГРАЦИИ")
        self.log.info(f"Входной файл: {input_path}")
        self.log.info(f"Папка сохранения: {output_dir}")
        self.log.info(f"Откуда: {from_olt} / {from_chan}")
        self.log.info(f"Куда: {to_olt_name} / {to_chan}")

        # Нормализуем
        self.from_olt = from_olt
        self.from_chan = from_chan
        if to_olt_name.startswith("172."):
            self.to_olt_ip = to_olt_name
        else:
            self.to_olt_ip = f"172.18.0.{to_olt_name}"
        self.to_chan = to_chan
        self.header = f"{from_olt}/{from_chan} на {to_olt_name}/{to_chan}"

        # Создаём папку "Таблица {header}" как в ТЗ
        safe_header = self.header.replace("/", "-").replace("\\", "-").replace(":", "-")
        table_dir = os.path.join(output_dir, f"Таблица {safe_header}")
        os.makedirs(table_dir, exist_ok=True)
        output_dir = table_dir  # все файлы теперь сохраняются в эту папку
        self.log.info(f"Папка результата: {table_dir}")

        # ── 1. Загрузка данных ──
        self._progress(0, 10, "ШАГ 1/10: Загрузка входного файла...")
        rows = read_input_csv(input_path, self.log)
        if not rows:
            self.log.fatal("Нет данных для миграции. Прерывание.")
            return False
        total = len(rows)
        # Прогресс: total абонентов (шаги 3-5) + 8 overhead-шагов (загрузка, авторизация, старт пар-го, шаги 6-10)
        self._progress_total = total + 8
        self._progress_cur = 0
        self._inc_progress(1, f"Загружено {total} абонентов")

        # Инициализируем data с пустыми полями G-R
        self.data = []
        for r in rows:
            self.data.append({
                "id": r["id"],
                "mac": r["mac"],
                "desc": r["desc"],
                "rssi": r["rssi"],
                # Поля, заполняемые в процессе
                "olt_cdata": self.to_olt_ip,
                "chan_cdata": self.to_chan,
                "old_vlan_olt": "",
                "address_us": "",
                "old_vlan_billing": "",
                "contract": "",
                "phone": "",
                "new_vlan": "",
                "sn": "",
                "formula": "",
                "note": "",
            })

        # ── 2. Авторизация сервисов ──
        self._inc_progress(1, "ШАГ 2/10: Авторизация сервисов...")

        # Billing
        self.billing = BillingClient(self.log)
        if not self.billing.authorize(billing_login, billing_password):
            self.log.fatal("Не удалось авторизоваться в Billing. Прерывание.")
            return False

        # Userside
        self.userside = UsersideClient(self.log)
        if not self.userside.authorize():
            self.log.fatal("Не удалось авторизоваться в Userside. Прерывание.")
            return False

        # Eltex (SNMP — без авторизации)
        self.eltex = EltexClient(self.log)

        # C-Data (SSH — подключаемся позже, при необходимости)
        self.cdata = CDataClient(self.log)
        olt_ip_from = olt_name_to_ip(self.from_olt, self.log)

        # ── Шаг 7 параллельно: C-Data свободные слоты (SNMP) ──
        # Не зависит ни от каких данных абонентов — можно сразу
        import concurrent.futures as _cf
        _cdata_future = _cf.ThreadPoolExecutor(max_workers=1).submit(
            self.cdata.get_free_slots, self.to_olt_ip, self.to_chan,
            len(self.data)
        )

        # ── 3. Старые VLANы (последовательно — одна SSH-сессия быстрее) ──
        # ──         # ── 3+4+5: N SSH-сессий, каждая обрабатывает свою группу ──
        from config_loader import get as _cfg_threads
        _max_workers = _cfg_threads("parallel", "max_workers", default=3)
        self._inc_progress(1, f"ШАГ 3-5/10: Eltex VLAN + Userside + Billing ({_max_workers} сессии, {total} абонентов)...")
        yellow_count = 0
        orange_count = 0
        red_count = 0

        # Создаём N SSH-сессий и подключаем их
        # Делим абонентов на N групп
        _groups = [[] for _ in range(_max_workers)]
        for i, row in enumerate(self.data):
            _groups[i % _max_workers].append((i, row))

        def _process_group(group_idx, group):
            """Обработать группу абонентов на одной SSH-сессии.
               Возвращает (yellow, orange, red)."""
            from eltex_client import EltexClient as _Eltex
            _session = _Eltex(self.log)
            _session.connect(olt_ip_from)
            # Создаём свои сессии US + Billing на поток (3-4 активных)
            from addr_utils import norm_addr as _norm_addr, match_addr as _match_addr
            import re as _re
            from us_client import UsersideClient as _USClient
            _us_session = _USClient(self.log)
            _us_session.authorize()
            from billing_client import BillingClient as _BClient
            _bill_session = _BClient(self.log)
            _bill_session.authorize(billing_login, billing_password)
            y = o = r = 0
            for idx, row in group:
                mac = row["mac"]

                # Шаг 3: VLAN + PPPoE из Eltex (та же SSH-сессия)
                _eltex_result = _session.get_vlan_by_mac(olt_ip_from, mac)
                vlan = _eltex_result.get("vlan")
                row["old_vlan_olt"] = vlan if vlan else "?"
                row["_eltex_is_pppoe"] = _eltex_result.get("is_pppoe", False)
                if not vlan:
                    row["note"] += "VLAN OLT не найден; "

                # Шаг 4: адрес из US (своя сессия на поток)
                row["_raw_us"] = None
                if _us_session.authorize():  # force=False — не пересоздаёт, если уже есть
                    cid = _us_session.search_by_mac(mac)
                    if cid:
                        us_data = _us_session.get_subscriber_data(cid)
                        row["_raw_us"] = us_data
                        row["address_us"] = us_data.get("address", "")
                        if not row["contract"]:
                            row["contract"] = us_data.get("contract", "")
                        # US телефон сохраняем отдельно (эталон — billing)
                        _us_phone = us_data.get("phone", "")
                        if _us_phone:
                            row["_phone_us"] = _us_phone
                        # US ФИО — для сверки с billing
                        _us_fio = us_data.get("fio", "")
                        if _us_fio:
                            row["_fio_us"] = _us_fio
                if not row.get("address_us"):
                    row["note"] += "Адрес US не найден; "
                    y += 1

                # Шаг 5: биллинг
                contract = row.get("contract", "")
                addr_us = row.get("address_us", "")
                old_vlan_olt = row.get("old_vlan_olt", "")
                vg_results = None
                # Проверка: расторгнутый договор (8 цифр + буква) → поиск по адресу
                _is_terminated = False
                row["_raw_billing_by_contract"] = None
                row["_raw_billing_by_addr"] = None
                row["_raw_addr_rejected"] = None
                if contract:
                    clean = _re.sub(r'\s+от\s+\d{2}\.\d{2}\.\d{4}', '', contract).strip()
                    vg_results = _bill_session.search_by_contract(clean)
                    row["_raw_billing_by_contract"] = vg_results
                    if vg_results:
                        vg_row = vg_results[0]
                        vg_sub = _bill_session._row_to_subscriber(vg_row)
                        # Расторгнут: 8 цифр + буква, или blocked=1
                        blocked = str(vg_row.get("blocked", "0"))
                        if blocked != "0" or _re.match(r'^\d{8}\D', clean):
                            _is_terminated = True
                            vg_results = None  # сбрасываем — ищем по адресу
                            self.log.info(f"  Договор {clean} расторгнут, ищу по адресу")
                    else:
                        # Договор из US найден и blocked=0. Проверяем адрес биллинга + VLAN.
                        # Если адрес биллинга пустой или не совпадает с US-адресом —
                        # это может быть чужой договор (MAC привязан к старому абоненту).
                        _bill_contract_addr = _bill_session._get_billing_address(vg_row)
                        _addr_mismatch = False
                        if _bill_contract_addr:
                            from addr_utils import norm_addr as _na, match_addr as _ma
                            if not _ma(_na(addr_us), _na(_bill_contract_addr)):
                                self.log.info(f"  Адрес US≠billing для договора {clean}, ищу по адресу US")
                                _addr_mismatch = True
                        else:
                            self.log.info(f"  Договор {clean} без адреса в биллинге, ищу по адресу US")
                            _addr_mismatch = True
                        
                        # Дополнительно: проверяем VLAN из billing против VLAN из Eltex
                        if not _addr_mismatch and old_vlan_olt not in ("", "?", "5", "100"):
                            _vg_id = vg_row.get("vg_id", "")
                            if _vg_id:
                                _vlan_b = _bill_session.get_vlan(_vg_id)
                                if _vlan_b:
                                    _inner = str(_vlan_b).split(":")[-1]
                                    if _inner != old_vlan_olt:
                                        self.log.info(f"  VLAN billing ({_inner}) ≠ VLAN Eltex ({old_vlan_olt}), ищу по адресу")
                                        vg_results = None
                        
                        if _addr_mismatch:
                            vg_results = None

                # Поиск по адресу: если нет договора, договор расторгнут, или не найден
                # Пропускаем, если в адресе нет номера дома — запрос будет слишком широким
                from addr_utils import has_house_number
                if not vg_results and addr_us and has_house_number(addr_us):
                    # Строим поисковый запрос как в billing_sync
                    _city_words = {"сальск", "низовский", "новосальск", "капустино", "кучур-да", "кучурда",
                                   "заречный", "сальский", "г", "ул", "улица", "город", "",
                                   "пер", "переулок"}
                    # Разбиваем по запятым, затем каждую часть на отдельные слова
                    _parts = [w.strip().strip(",") for w in addr_us.replace('.', ' ').split(',') if w.strip()]
                    _words = []
                    for _part in _parts:
                        for _word in _part.split():
                            _word = _word.strip(",. ")
                            if _word:
                                _words.append(_word)
                    _keywords = [w.lower() for w in _words if w.lower() not in _city_words and (w.isdigit() or len(w) > 1)]
                    # Убираем дубликаты, сохраняя порядок
                    _seen = set()
                    _unique = []
                    for w in _keywords:
                        if w not in _seen:
                            _seen.add(w)
                            _unique.append(w)
                    # Первые 3 + 4-й если цифра (квартира)
                    _base = _unique[:3]
                    if len(_unique) > 3 and _unique[3].isdigit():
                        _base.append(_unique[3])
                    addr_query = " ".join(_base)
                    # Разделяем буквенный суффикс номера (118е → 118 е) для поиска
                    addr_query = re.sub(r'(\d+)([а-яa-z])', r'\1 \2', addr_query)
                    raw = _bill_session.search_by_address(f"Сальск {addr_query}")
                    # Fallback: если слэш в запросе не дал результатов — пробуем с пробелом
                    if not raw and '/' in addr_query:
                        _fallback = addr_query.replace('/', ' / ')
                        _fallback = re.sub(r'\s+', ' ', _fallback).strip()
                        self.log.debug(f"  Address search fallback: 'Сальск {_fallback}'")
                        raw = _bill_session.search_by_address(f"Сальск {_fallback}")
                    row["_raw_billing_by_addr"] = raw
                    if raw:
                        self.log.debug(f"  Address search returned {len(raw)} results")
                        for _ri2, _rr in enumerate(raw):
                            _a2 = str(_rr.get("address_2","") or "")[:50]
                            _ag = _rr.get("agreements",[])
                            _an = str(_ag[0].get("agrm_num","")) if _ag else "?"
                            self.log.debug(f"    RAW[{_ri2}]: {_an} addr={_a2}")
                    else:
                        self.log.debug(f"  Address search returned None/empty")
                    if raw:
                        # Номер дома — последнее число ДО кв (если есть квартира)
                        _raw_parts = [p.strip() for p in addr_us.split(',') if p.strip()]
                        _raw_parts = [p for p in _raw_parts if p.lower() not in {"сальск", "новосальск",
                                     "капустино", "кучур-да", "кучурда", "низовский", "заречный",
                                     "г", "ул", "пос", "район", "город"}]
                        house_num = ""
                        _skip_apt = False
                        for p in reversed(_raw_parts):
                            pl = p.lower()
                            if pl in ("кв", "кв.", "квартира"):
                                _skip_apt = True  # следующая цифра — квартира, пропускаем
                                continue
                            if p and any(c.isdigit() for c in p):
                                if _skip_apt:
                                    _skip_apt = False  # это квартира, берём следующую
                                    continue
                                house_num = p
                                break
                        # Быстрый pre-filter: номер дома из US адреса
                        from addr_utils import extract_house_number
                        _us_house = extract_house_number(addr_us)
                        if _us_house:
                            raw_filtered = []
                            for row_b in raw:
                                a2 = str(row_b.get("address_2", "") or "").lower()
                                # word boundary: ищем "37", но не "137"
                                if "сальск" in a2 and re.search(r'\b' + re.escape(_us_house) + r'\b', a2):
                                    raw_filtered.append(row_b)
                            if raw_filtered:
                                raw = raw_filtered

                        # Собираем ВСЕХ кандидатов, подошедших по адресу
                        _candidates = []
                        for _ri, row_b in enumerate(raw):
                            a2 = str(row_b.get("address_2", "") or "").lower()
                            _dbg_reason = None
                            if "сальск" not in a2:
                                _dbg_reason = "no сальск"
                            else:
                                from addr_utils import norm_addr as _na, match_addr as _ma
                                _us_norm = _na(addr_us)
                                _bill_norm = _na(a2)
                                if not _ma(_us_norm, _bill_norm):
                                    _dbg_reason = f"addr mismatch [{_us_norm}] vs [{_bill_norm}]"
                                else:
                                    agreements = row_b.get("agreements", [])
                                    agrm_num = str(agreements[0].get("agrm_num", "")) if agreements else ""
                                    if not agrm_num:
                                        agrm_num = str(row_b.get("agrm_num", "") or "")
                                    if agrm_num and _re.match(r'^\d{8}\D', agrm_num):
                                        _dbg_reason = f"terminated contract {agrm_num}"
                                    if agrm_num and _re.match(r'^\d{1,4}$', agrm_num):
                                        _dbg_reason = f"TV contract {agrm_num}"
                            if _dbg_reason:
                                self.log.debug(f"    ROW {_ri}: SKIP ({_dbg_reason}) addr={a2[:50]}")
                            else:
                                _candidates.append(row_b)
                                self.log.debug(f"    ROW {_ri}: ✅ MATCH (agrm={agrm_num}) addr={a2[:50]}")

                    self.log.debug(f"  Address search: {len(_candidates)} candidates after filtering")
                    # Сохраняем всех кандидатов (для выбора в диалоге)
                    if _candidates:
                        row["_raw_addr_rejected"] = _candidates
                        # Пытаемся найти кандидата с VLAN, совпадающим с Eltex
                        _vlan_matched = None
                        if old_vlan_olt not in ("", "?", "5", "100"):
                            for _cand in _candidates:
                                _cand_vg_id = _cand.get("vg_id", "")
                                # Если vg_id нет, пробуем найти через договор
                                if not _cand_vg_id:
                                    _cand_agrm = ""
                                    _cand_agreements = _cand.get("agreements", [])
                                    if _cand_agreements:
                                        _cand_agrm = str(_cand_agreements[0].get("agrm_num", ""))
                                    elif _cand.get("agrm_num"):
                                        _cand_agrm = str(_cand.get("agrm_num", ""))
                                    if _cand_agrm:
                                        _cand_full = _bill_session.search_by_contract(_cand_agrm)
                                        if _cand_full:
                                            _cand_vg_id = _cand_full[0].get("vg_id", "")
                                if _cand_vg_id:
                                    _cand_vlan_b = _bill_session.get_vlan(_cand_vg_id)
                                    if _cand_vlan_b:
                                        _cand_inner = str(_cand_vlan_b).split(":")[-1]
                                        if _cand_inner == old_vlan_olt:
                                            _vlan_matched = _cand
                                            self.log.info(f"  Кандидат {_cand_inner} совпадает с Eltex {old_vlan_olt}")
                                            break
                        
                        if _vlan_matched:
                            # Нашли по VLAN — берём, очищаем rejected
                            row["_raw_addr_rejected"] = []
                            vg_results = [_vlan_matched]
                            agreements = _vlan_matched.get("agreements", [])
                            if agreements:
                                _found_contract = str(agreements[0].get("agrm_num", ""))
                                if _found_contract:
                                    contract = _found_contract
                                    row["contract"] = contract
                            self.log.info(f"  Найден по VLAN: договор {contract}")
                        elif len(_candidates) == 1:
                            # Один кандидат, но VLAN не совпал — всё равно берём с пометкой
                            vg_results = [_candidates[0]]
                            agreements = _candidates[0].get("agreements", [])
                            if agreements:
                                _found_contract = str(agreements[0].get("agrm_num", ""))
                                if _found_contract:
                                    contract = _found_contract
                                    row["contract"] = contract
                            if old_vlan_olt in ("5", "100"):
                                self.log.info(f"  Единственный кандидат (PPPoE): договор {contract}")
                            else:
                                row["note"] += f"Кандидат без подтверждения по VLAN; "
                                self.log.info(f"  Единственный кандидат (без подтверждения VLAN): договор {contract}")
                        else:
                            # Несколько кандидатов — пользователь выберет в диалоге
                            self.log.info(f"  {len(_candidates)} кандидатов на адресе, требуется выбор в диалоге")
                            vg_results = None
                            if not row.get("contract"):
                                row["contract"] = ""
                    else:
                        vg_results = None

                vg_row = None
                if vg_results and vg_results[0].get("vg_id"):
                    # Из vgroup-результата — сразу используем
                    vg_row = vg_results[0]
                    for cand in vg_results:
                        bl = str(cand.get("blocked", "0"))
                        an = str(cand.get("agrm_num", ""))
                        if bl == "0" and not _re.match(r'^\d{8}\D', an):
                            vg_row = cand
                            break
                elif vg_results and contract:
                    # users endpoint (нет vg_id) — ищем vgroup по договору
                    vg_row = vg_results[0]
                    vg2 = _bill_session.search_by_contract(contract)
                    if vg2:
                        for cand in vg2:
                            if str(cand.get("blocked","0")) == "0":
                                vg_row = cand
                                break
                        else:
                                vg_row = vg2[0]

                # Общая логика для vg_row: извлекаем VLAN, телефон, сверяем адрес
                if vg_row:
                    vg_sub = _bill_session._row_to_subscriber(vg_row)
                    # Сверка ФИО с US (если есть)
                    _fio_bill = vg_sub.fio.lower().strip()
                    _fio_us = row.get("_fio_us", "").lower().strip()
                    if _fio_us and _fio_bill:
                        # Сравниваем первые слова (фамилия)
                        _bill_words = _fio_bill.split()
                        _us_words = _fio_us.split()
                        if _bill_words and _us_words:
                            _bill_surname = _bill_words[0].rstrip(',')
                            _us_surname = _us_words[0].rstrip(',')
                            from difflib import SequenceMatcher as _SM
                            if _SM(None, _bill_surname, _us_surname).ratio() < 0.85:
                                row["note"] += f"ФИО US≠billing ({_fio_us[:20]} vs {_fio_bill[:20]}); "
                                self.log.info(f"  ФИО US≠billing: US={_fio_us[:30]} | billing={_fio_bill[:30]}")
                    # Маска /32 = PPPoE
                    if '/32' in vg_sub.ip:
                        row["_billing_ip_mask_32"] = True
                    # ЮТС/ЮЛС — юрлицо, не помечаем оранж
                    if _re.match(r'Ю[ЛТ]С', vg_sub.contract):
                        old_vlan_olt = "legal"
                    # Всегда запрашиваем billing VLAN, даже если Eltex вернул 5/100.
                    # Может оказаться, что billing VLAN != 5 — значит это НЕ PPPoE
                    if vg_sub.vg_id and old_vlan_olt not in ("legal"):
                        vlan_b = _bill_session.get_vlan(vg_sub.vg_id)
                        if vlan_b:
                            row["old_vlan_billing"] = str(vlan_b).split(":")[-1]  # inner_vlan
                    # Телефон пишем из billing (эталон), US сохраняем отдельно
                    _bill_session._get_phone(vg_sub, vg_row)
                    _bill_phone = str(vg_sub.phone or vg_sub.mobile or "")
                    if _bill_phone:
                        row["phone"] = _bill_phone
                    if addr_us and old_vlan_olt not in ("5", "100", "legal"):
                        # Если VLAN из billing совпал с Eltex — адрес подтверждён, не пишем предупреждение
                        _bill_vlan_check = row.get("old_vlan_billing", "").split(":")[-1] if ":" in row.get("old_vlan_billing", "") else row.get("old_vlan_billing", "")
                        if _bill_vlan_check != old_vlan_olt:
                            bill_addr = _bill_session._get_billing_address(vg_row)
                            if bill_addr:
                                if not _match_addr(_norm_addr(addr_us), _norm_addr(bill_addr)):
                                    row["note"] += "Адрес US≠billing; "
                                    r += 1
                else:
                    # ЮТС/ЮЛС — юрлицо, не помечаем оранж
                    _is_legal = bool(contract and _re.match(r'Ю[ЛТ]С', contract))
                    if old_vlan_olt not in ("5", "100", "", "legal") and not _is_legal:
                        row["note"] += "Абонент в биллинге не найден; "
                        o += 1

                if old_vlan_olt not in ("5", "100", "legal"):
                    v_olt = row.get("old_vlan_olt", "")
                    v_bill_raw = row.get("old_vlan_billing", "")
                    # Берём inner_vlan если outer:inner
                    v_bill = v_bill_raw.split(":")[-1] if ":" in v_bill_raw else v_bill_raw
                    if v_olt and v_bill and v_olt != v_bill and v_olt != "?":
                        row["note"] += f"Расхождение VLAN: OLT={v_olt} Billing={v_bill_raw}; "
                        r += 1

                # ── SN: ставим в очередь, диалог будет позже ──
                row["sn"] = f"SCAN-{idx+1:04d}"
                self._sn_queue.put((idx, row.get("desc", "")))

                # ── Прогресс: +1 за каждую обработанную строку ──
                self._inc_progress(1, f"Обработан: {row.get('desc','')} ({row.get('address_us','')[:60]})")

            # возвращаем счётчики
            return (y, o, r)

        # Запускаем N потоков
        import concurrent.futures as _futures, traceback as _tb
        _group_errors = [False] * _max_workers
        with _futures.ThreadPoolExecutor(max_workers=_max_workers) as _pool:
            _futs = {}  # future -> group_index
            for i in range(_max_workers):
                f = _pool.submit(_process_group, i, _groups[i] if i < len(_groups) else [])
                _futs[f] = i
            for _f in _futures.as_completed(_futs):
                group_idx = _futs[_f]
                try:
                    yy, oo, rr = _f.result(timeout=120)  # ← таймаут 2 минуты на поток
                    yellow_count += yy
                    orange_count += oo
                    red_count += rr
                except _futures.TimeoutError:
                    _group_errors[group_idx] = True
                    self.log.error(f"Поток {group_idx} превысил таймаут 120 сек!")
                except Exception as e:
                    _group_errors[group_idx] = True
                    self.log.error(f"Ошибка в потоке {group_idx}: {e}")
                    self.log.debug(_tb.format_exc())

        # Помечаем абонентов из упавших потоков
        for gi, has_err in enumerate(_group_errors):
            if has_err:
                self.log.warn(f"Поток {gi} упал — данные абонентов группы могут быть неполными")
                for idx, row in _groups[gi]:
                    if not row.get("note"):
                        row["note"] = ""
                    row["note"] += "Ошибка обработки потока; "
                    if not row.get("old_vlan_olt"):
                        row["old_vlan_olt"] = "?"
        self.log.info("  3 потока завершены")
        # ── Диалог с пользователем при цветных строках ──
        colored_rows = []
        for i, row in enumerate(self.data):
            color = None
            v_olt = row.get("old_vlan_olt","")
            v_bill_raw = row.get("old_vlan_billing","")
            # inner_vlan для сравнения
            v_bill = v_bill_raw.split(":")[-1] if ":" in v_bill_raw else v_bill_raw

            if not row.get("address_us") or row.get("address_us", "").strip().lower() in ("н.д.", "н/д", "нет"):
                color = "yellow"
                if row.get("address_us", "").strip().lower() in ("н.д.", "н/д", "нет"):
                    row["note"] += "Адрес US='н.д.' — требуется проверка; "
            elif not v_bill and v_olt in ("", "?"):
                # Нет данных ни от Eltex, ни от Billing — new_vlan будет "?"
                color = "orange"
                row["note"] += "Нет данных OLT и Billing; "
            elif not v_bill and v_olt not in ("5", "100"):
                # billing пуст, OLT != 5/100 — неопределённость
                color = "orange"
            elif v_olt == "?" and v_bill:
                # OLT не вернул VLAN, но billing нашёл — неопределённость, оранж
                color = "orange"
                row["note"] += "VLAN OLT=? Billing найден; "
            elif v_olt and v_bill and v_olt != v_bill and v_olt != "?":
                color = "red"
            
            # Если адресный поиск вернул несколько кандидатов — требуется выбор
            _candidates = row.get("_raw_addr_rejected", [])
            if _candidates and len(_candidates) > 1:
                color = "orange"
                row["note"] += f"На адресе {len(_candidates)} абонентов, требуется выбор; "
            
            if color:
                colored_rows.append((color, i, row))

        # Диалог: передаём цветные строки + клиенты для живого перезапроса
        if colored_rows and self.on_confirm_colors:
            self._progress_pause()
            self.on_confirm_colors(colored_rows, self.billing, self.userside, olt_ip_from)
            self._progress_resume()

        if yellow_count > 0:
            self.log.warn(f"Жёлтых строк: {yellow_count}")
        if orange_count > 0:
            self.log.warn(f"Оранжевых строк: {orange_count}")
        if red_count > 0:
            self.log.warn(f"Красных строк: {red_count}")

        # ── 6. Новые VLANы ──
        self._inc_progress(1, "ШАГ 6/10: Получение новых VLAN...")

        # Считаем, сколько абонентов НЕ PPPoE (vlan != 5 и billing не подтверждает 5)
        # PPPoE = Eltex VLAN 5/100 И billing VLAN тоже 5/100 или пустой
        non_pppoe_count = 0
        pppoe_count = 0
        false_pppoe = 0
        for row in self.data:
            old_vlan = row.get("old_vlan_olt", "")
            bill_vlan = row.get("old_vlan_billing", "")
            if old_vlan in ("5", "100"):
                # Eltex сказал 5 — но проверяем billing
                if bill_vlan and bill_vlan not in ("5", "100"):
                    # Billing говорит другой VLAN — это НЕ PPPoE!
                    row["note"] += f"VLAN OLT={old_vlan} но billing={bill_vlan}; "
                    non_pppoe_count += 1
                    false_pppoe += 1
                else:
                    # billing пуст или тоже 5 — действительно PPPoE
                    pppoe_count += 1
            elif old_vlan in ("", "?"):
                # OLT не ответил — если billing есть, нужен новый VLAN
                if bill_vlan and bill_vlan not in ("5", "100", ""):
                    non_pppoe_count += 1
                else:
                    pppoe_count += 1
            else:
                non_pppoe_count += 1
        self.log.info(f"  Абонентов всего: {total}, "
                      f"PPPoE: {pppoe_count}, нужно новых VLAN: {non_pppoe_count}, "
                      f"из них ложных PPPoE (VLAN OLT=5 но billing≠5): {false_pppoe}")

        # Запрашиваем VLANы с учётом пропуска первых 100
        new_vlans = self.billing.get_new_vlans(self.to_olt_ip,
                                                count=non_pppoe_count,
                                                skip_first=100)
        vlan_idx = 0
        for i, row in enumerate(self.data):
            old_vlan = row.get("old_vlan_olt", "")
            bill_vlan = row.get("old_vlan_billing", "")
            
            # Определяем: действительно PPPoE или нет?
            # Три источника:
            #   1. Eltex show config (Rules profile содержит PPPoE)
            #   2. Billing IP mask (/32 = PPPoE)
            #   3. Eltex rule show pon (VLAN=5)
            _eltex_pppoe_flag = row.get("_eltex_is_pppoe", False)
            _bill_mask_32 = row.get("_billing_ip_mask_32", False)
            _vlan_is_5 = old_vlan in ("5", "100")
            # Если 2 из 3 подтверждают — PPPoE
            pppoe_votes = sum([_eltex_pppoe_flag, _bill_mask_32, _vlan_is_5])
            is_pppoe = pppoe_votes >= 2
            
            if is_pppoe:
                # PPPoE — новый VLAN не нужен
                row["new_vlan"] = old_vlan
                self.log.info(f"  [{i+1}] {row['mac']}: пропущен (PPPoE), "
                              f"vlan={old_vlan}")
            elif old_vlan in ("", "?") and (not bill_vlan or bill_vlan in ("", "5", "100")):
                # Нет данных от Eltex. Если billing найден с маской /32 — PPPoE
                # Если нет — VLAN не определён, нужна ручная проверка
                if _eltex_pppoe_flag or _bill_mask_32:
                    row["new_vlan"] = "5"
                    self.log.info(f"  [{i+1}] {row['mac']}: нет VLAN OLT, "
                                  f"но {'Eltex' if _eltex_pppoe_flag else 'маска /32'}: vlan=5")
                else:
                    row["new_vlan"] = "?"
                    row["note"] += "VLAN=? (нет данных OLT+US+Billing); "
                    self.log.warn(f"  [{i+1}] {row['mac']}: VLAN=? (нет данных OLT, US и Billing)")
            else:
                if vlan_idx < len(new_vlans):
                    row["new_vlan"] = str(new_vlans[vlan_idx])
                    self.log.info(f"  [{i+1}] {row['mac']}: новый vlan={new_vlans[vlan_idx]}")
                    vlan_idx += 1
                else:
                    row["new_vlan"] = ""
                    row["note"] += "Не хватило новых VLAN; "
                    self.log.warn(f"  [{i+1}] {row['mac']}: НЕ ХВАТИЛО новых VLAN!")

        # ── 6b. Логирование параметров генерации из конфига ──
        from config_loader import get as _cfg_loader
        _skip_first = _cfg_loader("generation", "skip_first_vlans", default=100)
        _service_vlan_200 = _cfg_loader("cdata", "service_vlan", "172.18.0.200", default="?")
        self.log.info(f"  Конфиг: skip_first_vlans={_skip_first}, service_vlan_200={_service_vlan_200}")

        # ── 7. Свободные слоты на C-Data ──
        self._inc_progress(1, "ШАГ 7/10: Ожидание свободных слотов C-Data (SNMP)...")
        free_slots = _cdata_future.result(timeout=30)
        if not free_slots:
            self.log.warn("Не удалось получить свободные слоты на C-Data")

        # Назначаем номера на стволе (колонка H) всем подряд
        for i, row in enumerate(self.data):
            if i < len(free_slots):
                slot = free_slots[i]
                row["chan_cdata"] = f"{slot['port']} {slot['ont_id']}"
            else:
                row["chan_cdata"] = f"{self.to_chan} ?"
                row["note"] += "Не хватило слотов на C-Data; "
        self.log.info(f"  Назначено слотов: {min(len(self.data), len(free_slots))}")

        # ── 8. S/N — очередь диалогов, по одному ──
        self._inc_progress(1, "ШАГ 8/10: Ввод S/N...")
        _sn_total = self._sn_queue.qsize()
        _sn_done = 0
        while not self._sn_queue.empty():
            idx, desc = self._sn_queue.get()
            _sn_done += 1
            default_sn = self.data[idx].get("sn", f"SCAN-{idx+1:04d}")
            if self.on_ask_sn:
                self._progress_pause()
                sn = self.on_ask_sn(desc, default_sn, _sn_done, _sn_total)
                self._progress_resume()
                self.data[idx]["sn"] = sn or default_sn
            # Только обновляем сообщение, без инкремента (уже посчитаны в шагах 3-5)
            self._progress(self._progress_cur, self._progress_total,
                           f"S/N [{_sn_done}/{_sn_total}]: {desc}")
            # Печать этикетки — сразу после SN
            if not self._skip_label_print:
                try:
                    ok = print_label(self.data[idx], self.log)
                    if ok:
                        self.log.info(f"  🖨 Этикетка отправлена на печать")
                    else:
                        self.log.warn(f"  ⚠ Печать этикетки не выполнена")
                except Exception as e:
                    self.log.warn(f"  ⚠ Ошибка печати: {e}")

        # ── 9. Сборка формул (команд C-Data) ──
        self._inc_progress(1, "ШАГ 9/10: Сборка команд C-Data...")
        commands = build_commands_batch(self.data, self.log, free_slots)
        for i, row in enumerate(self.data):
            if i < len(commands):
                row["formula"] = commands[i]

        # ── 10. Сохранение результатов ──
        self._inc_progress(1, "ШАГ 10/10: Сохранение результатов...")
        os.makedirs(output_dir, exist_ok=True)

        # Имя файла — заменяем недопустимые символы
        safe_header = self.header.replace("/", "-").replace("\\", "-").replace(":", "-")
        table_path = os.path.join(output_dir, f"Общая таблица {safe_header}.xlsx")
        result_data = write_output_table(
            table_path, self.header,
            self.from_olt, self.from_chan,
            self.to_olt_ip, self.to_chan,
            self.data, self.log
        )

        # 3 выгрузки
        save_export_tables(
            os.path.join(output_dir, f"Общая таблица {safe_header}.xlsx"),
            result_data, self.log
        )

        # Файл для OLT (Блокнот)
        notepad_text = format_for_notepad(commands)
        notepad_path = os.path.join(output_dir, f"Для ОЛТ {safe_header}.txt")
        with open(notepad_path, "w", encoding="utf-8") as f:
            f.write(notepad_text)
        self.log.info(f"Файл команд: {notepad_path}")

        # Файл для OLT (Word)
        word_path = os.path.join(output_dir, f"Для ОЛТ {safe_header}.docx")
        save_commands_to_word(word_path, commands, self.log)

        # Итог
        elapsed = time.time() - start_time
        self.log.section("МИГРАЦИЯ ЗАВЕРШЕНА")
        self.log.info(f"Всего абонентов: {total}")
        self.log.info(f"Время выполнения: {elapsed:.1f} сек")
        self.log.info(f"Результаты: {output_dir}")

        self._progress(self._progress_total, self._progress_total, f"✅ Готово! {elapsed:.1f} сек")
        return True
