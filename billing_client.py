#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
billing_client.py — HTTP-клиент для LANBilling (billing.timernet.ru).

Авторизация: логин/пароль → POST /api.php/api/login/authorize/0 → PHPSESSID.
Запросы (только чтение):
  - search_by_contract(contract) — поиск по договору
  - search_by_address(address)   — поиск по адресу
  - get_agreement_details(agrm_id) — телефон, контакты
  - get_vlan(vg_id)              — VLAN из accountsPort
"""

import json
import re
import time
import socket
import urllib.request
import urllib.parse
from typing import Optional
from log_utils import Logger
from config_loader import get as _cfg


BILLING_HOST = "billing.timernet.ru"
API_BASE = f"https://{BILLING_HOST}/api.php/api"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


class BillingSubscriber:
    """Данные абонента из Billing."""
    def __init__(self):
        self.uid = ""
        self.agrm_id = ""
        self.vg_id = ""
        self.agent_id = ""
        self.contract = ""
        self.fio = ""
        self.phone = ""
        self.mobile = ""
        self.login = ""
        self.address = ""
        self.balance = ""
        self.vlan = ""
        self.ip = ""

    def is_valid(self) -> bool:
        return bool(self.fio or self.contract)


class BillingClient:
    """Клиент к API биллинга."""

    def __init__(self, log: Logger):
        self.log = log
        self.phpsessid = ""
        self._cookie_header = ""
        self._authorized = False
        self._billing_login = ""
        self._billing_pass = ""

    # ── Авторизация ──

    def authorize(self, login: str, password: str, force: bool = False) -> bool:
        """
        POST /api.php/api/login/authorize/0
        Получает PHPSESSID.
        Если уже авторизован и force=False — пропускает.
        """
        if self._authorized and not force:
            return True  # уже авторизованы
        # Сохраняем логин/пароль для авто-переавторизации
        self._billing_login = login
        self._billing_pass = password
        self.log.section("АВТОРИЗАЦИЯ В BILLING")

        url = f"{API_BASE}/login/authorize/0"
        data = urllib.parse.urlencode({
            "login": login, "password": password
        }).encode("utf-8")

        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("User-Agent", UA)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            resp = urllib.request.urlopen(req, timeout=15)
            body = resp.read().decode("utf-8", errors="replace")
            self.log.debug(f"Ответ authorize: {body[:200]}")

            # Извлекаем PHPSESSID из Set-Cookie
            for c in resp.headers.get_all("Set-Cookie") or []:
                m = re.search(r"PHPSESSID=([^;]+)", c)
                if m:
                    self.phpsessid = m.group(1)
                    self._cookie_header = f"PHPSESSID={self.phpsessid}"
                    self._authorized = True
                    self.log.info(f"Авторизация в Billing: УСПЕХ")
                    return True

            self.log.error("PHPSESSID не найден в ответе")
            return False

        except urllib.error.HTTPError as e:
            self.log.error(f"Ошибка авторизации HTTP {e.code}")
            return False
        except Exception as e:
            self.log.error(f"Ошибка авторизации: {e}")
            return False

    # ── HTTP запросы ──

    def _get(self, path: str, params: dict = None, _retried: bool = False) -> Optional[dict]:
        """GET к API, возвращает JSON.

        При 401/403 автоматически переавторизуется и повторяет запрос один раз.
        """
        if not self._authorized or not self.phpsessid:
            self.log.error("Нет авторизации в Billing")
            return None

        full_params = {**(params or {}), "_dc": int(time.time() * 1000)}
        url = f"{API_BASE}/{path}?{urllib.parse.urlencode(full_params, doseq=True)}"

        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        req.add_header("Accept", "*/*")
        req.add_header("X-Requested-With", "XMLHttpRequest")
        req.add_header("Referer", f"https://{BILLING_HOST}/")
        req.add_header("Cookie", self._cookie_header)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                parsed = json.loads(body)
                # DEBUG: если запрос ports — распечатать первые 3 результата
                if "ports" in path and "device_id" in (params or {}):
                    rs = parsed.get("results", [])
                    free_cnt = sum(1 for r in rs if not r.get("login"))
                    sample = [r.get("inner_vlan") for r in rs[:3]]
                    self.log.debug(f"[_get] {path}: total={len(rs)}, "
                                   f"free={free_cnt}, sample_inner={sample}")
                return parsed
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                self.log.warn(f"Сессия биллинга истекла (HTTP {e.code})")
                self._authorized = False
                # Автоматическая переавторизация (один раз)
                if not _retried and hasattr(self, '_billing_login') and hasattr(self, '_billing_pass'):
                    self.log.info("Автопереавторизация в биллинге...")
                    if self.authorize(self._billing_login, self._billing_pass, force=True):
                        return self._get(path, params, _retried=True)
            else:
                self.log.warn(f"HTTP {e.code} для {path}")
            return None
        except urllib.error.URLError as e:
            self.log.warn(f"Таймаут/сетевая ошибка для {path}: {e.reason}")
            return None
        except socket.timeout:
            self.log.warn(f"Сокет-таймаут сработал для {path} — не завис!")
            return None
        except Exception as e:
            self.log.warn(f"Ошибка запроса {path}: {e}")
            return None

    # ── Поиск по договору ──

    def search_by_contract(self, contract: str) -> Optional[list]:
        """Поиск учётных записей по номеру договора."""
        self.log.info(f"Поиск в биллинге по договору: {contract}")
        data = self._get("vgroup", {
            "get_full_data": "1", "agent_types": "", "agent_id": "0",
            "tar_id": "", "blocked": "-1", "search_template": "",
            "property": "agrm_num",
            "accounts_grid_search_field": contract,
            "agrm_num": contract,
            "page": "1", "start": "0", "limit": "100",
            "sort": json.dumps([{"property": "blocked", "direction": "ASC"}]),
        })
        if data and data.get("success") and data.get("results"):
            return data["results"]
        return None

    # ── Поиск по адресу ──

    def search_by_address(self, address: str) -> Optional[list]:
        """Поиск по адресу через users endpoint."""
        self.log.info(f"Поиск в биллинге по адресу: {address}")
        data = self._get("users", {
            "get_full": "1", "is_template": "0",
            "include_preactivated": "0", "type": "0",
            "category": "-1", "use_search_template": "false",
            "address": address,
            "page": "1", "start": "0", "limit": "200",
        })
        if data and data.get("success") and data.get("results"):
            return data["results"]
        return None

    # ── Детали договора ──

    def get_agreement_details(self, agrm_id: int) -> Optional[dict]:
        """Детали договора (телефон, email)."""
        data = self._get(f"agreements/{agrm_id}", {})
        if data and data.get("success"):
            r = data.get("results")
            if isinstance(r, dict):
                return r
            elif isinstance(r, list) and r:
                return r[0]
        return None

    # ── VLAN ──

    def get_vlan(self, vg_id: str) -> Optional[str]:
        """
        Получить VLAN из accountsPort/{vg_id}.
        Возвращает "outer:inner" или "outer" или None.
        """
        if not vg_id:
            return None
        data = self._get(f"accountsPort/{vg_id}", {})
        if data and data.get("success") and data.get("results"):
            r = data["results"]
            if isinstance(r, list) and r:
                r = r[0]
            if isinstance(r, dict):
                outer = r.get("outer_vlan", "")
                inner = r.get("inner_vlan", "")
                if outer and inner:
                    vlan = f"{outer}:{inner}"
                    self.log.debug(f"  get_vlan({vg_id}) → {vlan}")
                    return vlan
                if outer:
                    self.log.debug(f"  get_vlan({vg_id}) → outer={outer}")
                    return str(outer)
            self.log.debug(f"  get_vlan({vg_id}): results format unexpected: {type(r)} {str(r)[:200]}")
        else:
            self.log.debug(f"  get_vlan({vg_id}): no data (success={data.get('success') if data else 'no response'})")
        return None

    # ── Поиск абонента в биллинге по адресу ──

    def get_subscriber_by_address(self, address: str) -> Optional[BillingSubscriber]:
        """
        Ищет абонента в биллинге по адресу (столбец J).
        Возвращает BillingSubscriber с vlan, contract, phone.

        Алгоритм:
          1. users endpoint → uid, agreements[].agrm_num (номер договора)
          2. vgroup поиск по договору → vg_id
          3. accountsPort/{vg_id} → VLAN

        Используется в orchestrator.py шаг 5 (колонки K, L, M).
        """
        self.log.info(f"Поиск в биллинге по адресу: {address}")
        results = self.search_by_address(address)
        if not results:
            self.log.warn(f"  Адрес не найден: {address}")
            return None

        row = results[0]
        sub = BillingSubscriber()

        # Извлекаем uid и ФИО
        sub.uid = str(row.get("uid", ""))
        sub.fio = str(row.get("name", "") or row.get("login", ""))
        sub.login = str(row.get("login", ""))

        # Договор из agreements[] — первый активный
        agreements = row.get("agreements", [])
        if agreements:
            ag = agreements[0]
            sub.agrm_id = str(ag.get("agrm_id", ""))
            sub.contract = str(ag.get("agrm_num", ""))

        # Телефон из users или agreements
        for k in ["phone", "mobile", "descr"]:
            v = row.get(k, "")
            if v:
                sub.phone = str(v).strip()
                break

        # Ищем vgroup по договору, чтобы получить vg_id и VLAN
        if sub.contract:
            vg_results = self.search_by_contract(sub.contract)
            if vg_results:
                vg_row = vg_results[0]
                sub.vg_id = str(vg_row.get("vg_id", ""))
                # Дополняем телефон из vgroup если не нашли
                if not sub.phone:
                    for k in ["phone", "mobile", "tel", "contact_phone"]:
                        v = vg_row.get(k, "")
                        if v:
                            sub.phone = str(v).strip()
                            break

        # VLAN из accountsPort/{vg_id}
        if sub.vg_id:
            vlan = self.get_vlan(sub.vg_id)
            if vlan:
                sub.vlan = vlan

        self.log.info(f"  Найден: uid={sub.uid}, договор={sub.contract}, "
                      f"vg_id={sub.vg_id}, vlan={sub.vlan}")
        return sub

    # ── Поиск устройства по IP (для колонки N) ──

    def find_device_by_ip(self, ip_pattern: str) -> Optional[int]:
        """
        GET accountsPort/devices?name=сальск
        Ищет устройство по фрагменту IP в поле name.
        ip_pattern — например "201" для поиска "172.18.0.101-СКАТ-Сальск-101-CDATA-201"
        Возвращает device_id или None.
        """
        self.log.info(f"Поиск устройства биллинга по IP: {ip_pattern}")
        data = self._get("accountsPort/devices", {
            "name": "сальск",
            "page": "1", "start": "0", "limit": "100",
        })
        if not data or not data.get("success") or not data.get("results"):
            self.log.warn("  Устройства не найдены")
            return None

        for dev in data["results"]:
            name = dev.get("name", "")
            if ip_pattern in name:
                did = dev.get("device_id")
                self.log.info(f"  ✅ Найдено: {name} (device_id={did})")
                return did

        self.log.warn(f"  Устройство с IP={ip_pattern} не найдено среди {len(data['results'])} шт")
        return None

    # ── Получение новых VLAN (колонка N) ──

    def get_new_vlans(self, to_olt_ip: str, count: int,
                      skip_first: int = None) -> list[str]:
        """
        Получить новые VLANы для целевого OLT из биллинга (только чтение).

        Алгоритм:
          1. find_device_by_ip() → device_id
          2. ports?device_id=X&alldata=1 — все порты устройства
          3. Фильтр: оставляем только порты без login (свободные)
          4. Сортируем свободные по inner_vlan ASC
          5. Пропускаем первые skip_first свободных
          6. Берём следующие count inner_vlan

        Возвращает list[str] — inner_vlan для колонки N.
        """
        ip_last = to_olt_ip.rstrip(".").split(".")[-1]
        device_id = self.find_device_by_ip(ip_last)
        if not device_id:
            device_id = self.find_device_by_ip(to_olt_ip)
            if not device_id:
                self.log.error(f"  ❌ Устройство {to_olt_ip} не найдено в биллинге")
                return []

        self.log.info(f"Загрузка портов device_id={device_id}, нужно {count} свободных...")

        data = self._get("ports", {
            "device_id": device_id,
            "alldata": "1",
            "page": "1", "start": "0", "limit": "5000",
        })
        if not data or not data.get("success") or not data.get("results"):
            self.log.warn("  Нет данных по портам")
            return []

        # Фильтр: свободные = без логина (как галочка «не показывать занятые»)
        # Сортируем по inner_vlan ASC — от меньшего к большему
        all_ports = data["results"]
        free_ports = [p for p in all_ports if not p.get("login")]
        free_ports.sort(key=lambda p: int(p.get("inner_vlan", 0)))
        # Фильтр: оставляем только G-PON порты (порт на стволе, а не uplink/service)
        # Фильтр G-PON: не применяем, так как имена портов в billing API
        # не содержат "gpon"/"pon". Используем все свободные порты.
        gpon_ports = free_ports

        self.log.info(f"  Всего портов: {len(all_ports)}, свободных: {len(free_ports)}")

        free_vlans = [str(p["inner_vlan"]) for p in gpon_ports
                      if p.get("inner_vlan")]

        # skip_first из конфига (по умолчанию 100)
        if skip_first is None:
            skip_first = _cfg("generation", "skip_first_vlans", default=100)

        # Пропускаем первые skip_first свободных
        result = free_vlans[skip_first:skip_first + count]
        if len(result) < count:
            self.log.warn(f"  ⚠ Не хватает свободных VLAN: нужно {count}, доступно {len(result)} "
                          f"(пропущено {skip_first}, всего свободных {len(free_vlans)})")
        self.log.info(f"  Пропущено {skip_first}, взято {len(result)} inner_vlan: {result[:10]}{'...' if len(result)>10 else ''}")
        return result

    # ── Конвертация строки vgroup → BillingSubscriber ──

    def _row_to_subscriber(self, row: dict) -> BillingSubscriber:
        """Преобразует строку результата vgroup в BillingSubscriber."""
        sub = BillingSubscriber()
        sub.uid = str(row.get("uid", ""))
        sub.agrm_id = str(row.get("agrm_id", ""))
        sub.vg_id = str(row.get("vg_id", ""))
        sub.agent_id = str(row.get("agent_id", ""))
        sub.contract = str(row.get("agrm_num", ""))
        sub.fio = str(row.get("user_name") or row.get("login", ""))
        sub.login = str(row.get("login", ""))
        sub.balance = str(row.get("balance", ""))
        sub.vg_status = str(row.get("vg_status", "") or row.get("blocked", "") or "")

        # Телефон
        for k in ["phone", "mobile", "tel", "contact_phone"]:
            v = row.get(k, "")
            if v:
                if not sub.phone:
                    sub.phone = str(v).strip()
                else:
                    sub.mobile = str(v).strip()

        # Адрес из vgroup
        addr_list = row.get("addresses")
        if addr_list and isinstance(addr_list, list):
            for a in addr_list:
                ad = a.get("address", "") if isinstance(a, dict) else str(a)
                if ad:
                    sub.address = ad.rstrip(",").replace(",,", ", ").strip()
                    break

        return sub

    # ── Умный поиск: договор → проверка адреса ──

    def _get_billing_address(self, row: dict) -> str:
        """Извлечь адрес из строки биллинга: приоритет address_2 → addresses[] (любой type) → address_1."""
        # address_2 — фактический адрес (как в exescript)
        addr = str(row.get("address_2", "") or "").strip()
        if addr:
            return addr
        # addresses[] — берём первый непустой (type может быть None, 0, 1)
        addrs = row.get("addresses", [])
        if addrs and isinstance(addrs, list):
            for entry in addrs:
                if isinstance(entry, dict):
                    v = str(entry.get("address", "") or "").strip()
                    if v:
                        return v
        # address_1 — юридический адрес
        return str(row.get("address_1", "") or "").strip()

    def _get_phone(self, sub: BillingSubscriber, row: dict):
        """Получить телефон из всех доступных источников."""
        # 1. Из vgroup строки
        for k in ["phone", "mobile", "tel", "contact_phone"]:
            v = row.get(k, "")
            if v and not sub.phone:
                sub.phone = str(v).strip()
        # 2. Из agreements/{agrm_id}
        if not sub.phone and sub.agrm_id:
            try:
                details = self.get_agreement_details(int(sub.agrm_id))
                if details:
                    for k in ["tel", "phone", "mobile"]:
                        v = details.get(k, "")
                        if v:
                            sub.phone = str(v).strip()
                            break
            except Exception:
                pass
        # 3. Из users/{uid}
        if not sub.phone and sub.uid:
            try:
                data = self._get(f"users/{sub.uid}", {})
                if data and data.get("success"):
                    r = data.get("results", {})
                    if isinstance(r, list) and r:
                        r = r[0]
                    if isinstance(r, dict):
                        for k in ["phone", "mobile"]:
                            v = r.get(k, "")
                            if v:
                                sub.phone = str(v).strip()
                                break
            except Exception:
                pass

    def find_by_contract_and_address(self, contract: str,
                                     address_us: str) -> Optional[BillingSubscriber]:
        """
        Ищет в биллинге по договору, проверяет совпадение адреса.

        contract — номер договора из US
        address_us — адрес из US (ул Гастелло, 9)

        Сравнение адресов через addr_utils (нормализация + алиасы).
        Возвращает BillingSubscriber или None.
        """
        from addr_utils import norm_addr, match_addr

        self.log.info(f"Поиск: договор {contract}, адрес {address_us}")

        results = self.search_by_contract(contract)
        if not results:
            self.log.warn(f"  Договор {contract} не найден в биллинге")
            return None

        row = results[0]
        sub = self._row_to_subscriber(row)

        # Получаем VLAN
        if sub.vg_id:
            vlan = self.get_vlan(sub.vg_id)
            if vlan:
                sub.vlan = vlan

        # Получаем телефон
        self._get_phone(sub, row)

        # Сравниваем адреса через addr_utils
        if not address_us:
            return sub

        us_norm = norm_addr(address_us)

        # Проверяем address_2, addresses[], address_1
        billing_addr = self._get_billing_address(row)
        if billing_addr:
            bill_norm = norm_addr(billing_addr)
            if match_addr(us_norm, bill_norm):
                self.log.info(f"  ✅ Адрес совпадает: {address_us}")
                return sub

        # Если address_2 не совпал — пробуем sub.address (собранный из addresses[])
        if sub.address and sub.address.lower() != (billing_addr or "").lower():
            bill_norm2 = norm_addr(sub.address)
            if match_addr(us_norm, bill_norm2):
                self.log.info(f"  ✅ Адрес совпадает (addresses[]): {address_us}")
                return sub

        self.log.warn(f"  ⚠ Адрес НЕ совпадает: US={address_us} vs Bill(addr2)={billing_addr}")
        return None
