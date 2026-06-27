#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
us_client.py — HTTP-клиент для Userside.

Авторизация: form-based (support_aksai).
Поиск: device list по MAC → customer_id → show-страница → парсинг адреса.
"""

import re
import urllib.parse
from typing import Optional
from log_utils import Logger

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


from config_loader import get as _cfg

# Загружаем из config.toml
US_HOST = _cfg("userside", "host", default="176.118.236.220:8080")
US_LOGIN = _cfg("userside", "login", default="support_aksai")
US_PASSWORD = _cfg("userside", "password", default="HPn-in9-ayw-ZHy")


class UsersideClient:
    """
    Клиент Userside (UserSide ERP).
    Сессия через requests.Session().
    """

    def __init__(self, log: Logger):
        self.log = log
        self.base_url = f"http://{US_HOST}/oper/"
        self._session: Optional[requests.Session] = None

    # ── Авторизация ──

    def authorize(self, force: bool = False) -> bool:
        """Form-based логин. Возвращает True если успешно.
        Если сессия уже создана и force=False — пропускает."""
        if not HAS_REQUESTS:
            self.log.error("Установите requests: pip install requests")
            return False

        if self._session and not force:
            return True  # сессия уже есть

        self.log.section("АВТОРИЗАЦИЯ В USERSIDE")
        self.log.info(f"Хост: {US_HOST}, логин: {US_LOGIN}")

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

        try:
            # GET — получаем куку
            r1 = self._session.get(self.base_url, timeout=15)
            self.log.debug(f"GET /oper/: {len(r1.text)} символов")

            # POST — логинимся
            r2 = self._session.post(
                self.base_url,
                data={"action": "login", "username": US_LOGIN, "password": US_PASSWORD},
                headers={"Referer": self.base_url},
                timeout=15,
            )

            # Проверка: есть ли logout в ответе
            if "logout" in r2.text.lower():
                self.log.info("Авторизация в Userside: УСПЕХ")
                return True

            self.log.error("Не удалось авторизоваться в Userside")
            self.log.debug(f"Фрагмент: {r2.text[:300]}")
            return False

        except Exception as e:
            self.log.error(f"Ошибка авторизации в US: {e}")
            return False

    # ── Поиск по MAC ──

    def search_by_mac(self, mac: str) -> Optional[str]:
        """
        Поиск ONU по MAC-адресу через ajax_loadlist.

        URL: /oper/?core_section=device&action=ajax_loadlist
             &type5=3&type_device=onu&search=%20MAC%20

        В ответе — HTML-таблица с устройствами. Извлекаем customer_id
        из ссылки на карточку абонента.

        Автоматически определяет сброс сессии (ответ < 500 байт = редирект на логин)
        и переавторизуется с повторным запросом.

        Возвращает customer_id или None.
        """
        # Размер ответа < 200 байт — почти гарантированно редирект на страницу логина.
        # Такое бывает, если сессия US истекла (таймаут ~20-30 мин).
        # При обнаружении — принудительная переавторизация и повтор.
        # Реальный редирект на логин — это 3000+ байт. 157 байт = «Нет записей» от ajax_loadlist.
        SESSION_LOST_THRESHOLD = 200

        def _do_search_ajax(type5_value: str) -> tuple[Optional[str], int]:
            """Поиск через ajax_loadlist (быстрый, но не всегда находит устройства)."""
            if not self._session:
                return None, 0

            url = (
                f"{self.base_url}?core_section=device&action=ajax_loadlist"
                f"&type5={type5_value}&type_device=onu"
                f"&search={urllib.parse.quote(mac)}"
            )
            self.log.debug(f"  URL (ajax): {url}")
            try:
                r = self._session.get(url, timeout=15)
                html = r.text
                sz = len(html)
                ids = re.findall(r'core_section=customer[^"]*action=show[^"]*id=(\d+)', html)
                if ids:
                    return ids[0], sz
                return None, sz
            except Exception as e:
                self.log.warn(f"  Ошибка поиска по MAC {mac}: {e}")
                return None, 0

        def _do_search_list() -> Optional[str]:
            """
            Поиск через action=list (полная страница) — как пользователь делает вручную.
            URL: /oper/?core_section=device&action=list&type5=3&cat=0&type_device=onu&search=MAC
            Этот эндпоинт находит устройства, которые ajax_loadlist пропускает.
            """
            if not self._session:
                return None
            url = (
                f"{self.base_url}?core_section=device&action=list"
                f"&type5=3&cat=0&type_device=onu"
                f"&search={urllib.parse.quote(mac)}"
            )
            self.log.debug(f"  URL (list): {url}")
            try:
                r = self._session.get(url, timeout=15)
                html = r.text
                # Ищем ссылку на карточку абонента
                # Формат: href="?core_section=customer&amp;action=show&amp;id=8601"
                ids = re.findall(r'core_section=customer[^"\']*action=show[^"\']*id=(\d+)', html)
                if ids:
                    return ids[0]
                self.log.debug(f"  action=list: {len(html)} символов, customer_id не найден")
                return None
            except Exception as e:
                self.log.debug(f"  action=list error: {e}")
                return None

        def _is_login_page(html_size: int) -> bool:
            """Проверить, похож ли размер ответа на страницу логина (редирект при потере сессии)."""
            # Login page в US — 3000+ байт. «Нет записей» = ~157 байт.
            return 2000 < html_size < 10000

        self.log.info(f"Поиск в US по MAC: {mac}")

        # ── Попытка 1: action=list (как вы делаете вручную) ──
        # Это основной метод — он находит устройства, которые ajax_loadlist пропускает
        cid = _do_search_list()
        if cid:
            self.log.info(f"  MAC {mac} → customer_id={cid} (action=list)")
            return cid

        # ── Попытка 2: ajax_loadlist type5=3 (ONU) ──
        cid, size = _do_search_ajax("3")
        if cid:
            self.log.info(f"  MAC {mac} → customer_id={cid} (ajax type5=3)")
            return cid

        # ── Проверка: не потеряна ли сессия? ──
        session_lost = _is_login_page(size)
        if session_lost:
            self.log.warn(f"  Ответ {size} байт — вероятно, сессия истекла. Переавторизация...")
            if self.authorize(force=True):
                # После переавторизации пробуем оба метода заново
                cid = _do_search_list()
                if cid:
                    self.log.info(f"  MAC {mac} → customer_id={cid} (action=list, после реавторизации)")
                    return cid
                cid, _ = _do_search_ajax("3")
                if cid:
                    self.log.info(f"  MAC {mac} → customer_id={cid} (ajax, после реавторизации)")
                    return cid

        # ── Попытка 3: ajax_loadlist type5=0 (все типы устройств) ──
        self.log.debug(f"  Fallback: поиск с type5=0 (все устройства)...")
        cid, _ = _do_search_ajax("0")
        if cid:
            self.log.info(f"  MAC {mac} → customer_id={cid} (ajax type5=0, fallback)")
            return cid

        self.log.warn(f"  MAC {mac} не найден в US")
        if size > 0:
            self.log.debug(f"  Размер ответа: {size} символов")
        return None

    # ── Получение данных абонента ──

    def fetch_subscriber_page(self, customer_id: str) -> Optional[str]:
        """
        Загрузить show-страницу абонента.
        core_section=customer обязателен, иначе редирект на логин.
        """
        if not self._session:
            return None

        url = f"{self.base_url}?core_section=customer&action=show&id={customer_id}"
        self.log.debug(f"GET карточки US: {url}")
        try:
            r = self._session.get(url, timeout=15)
            self.log.debug(f"  Размер: {len(r.text)} символов")
            return r.text
        except Exception as e:
            self.log.warn(f"  Ошибка загрузки карточки: {e}")
            return None

    @staticmethod
    def clean_contract(raw: str) -> str:
        """Из '20910009 от 31.07.2018' → '20910009'
            Из 'ЮТС-0289 от 28.12.2018' → 'ЮТС-0289'"""
        # Сначала пробуем ЮТС-XXXX или ЮЛС-XXXX
        m = re.search(r'(ЮТС-\d+|ЮЛС-\d+)', raw)
        if m:
            return m.group(1)
        # Потом обычный числовой договор
        m = re.search(r'(\d{6,12})', raw)
        return m.group(1) if m else raw.strip()

    def extract_field(self, html: str, field_name: str) -> str:
        """
        Извлечь поле из HTML карточки US.
        """
        # Экранируем имя поля, но точка после него — опциональна (Кв. → Кв)
        field_pat = re.escape(field_name) + r'\.?'
        patterns = [
            # left_data блок: "Адрес :</div><div ...>Значение</div>"
            re.compile(
                r'left_data[^>]*>.*?' + field_pat +
                r'\s*:.*?</div>\s*<div[^>]*>\s*(.*?)\s*</div>',
                re.IGNORECASE | re.DOTALL
            ),
            # label/td формат
            re.compile(
                r'<td[^>]*class="[^"]*field[^"]*"[^>]*>.*?' + field_pat +
                r'.*?</td>\s*<td[^>]*>\s*(.*?)\s*</td>',
                re.IGNORECASE | re.DOTALL
            ),
        ]
        for pat in patterns:
            m = pat.search(html)
            if m:
                val = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                if val:
                    return val
        return ""

    def get_subscriber_data(self, customer_id: str) -> dict:
        """
        Получить данные абонента из US по customer_id.
        Возвращает словарь: address, contract, fio, phone
        """
        html = self.fetch_subscriber_page(customer_id)
        if not html:
            return {"address": "", "contract": "", "fio": "", "phone": ""}

        raw_contract = self.extract_field(html, "Договор")
        address = self.extract_field(html, "Адрес")
        # Извлекаем квартиру из отдельного поля Кв. и добавляем к адресу
        kv = self.extract_field(html, "Кв")
        if kv and (kv.isdigit() or re.match(r'^\d+[а-яa-zA-Z]?$', kv)):
            address = address.rstrip(" ,") + f", кв {kv}"
        phone = self.extract_field(html, "Телефон")
        if not phone:
            phone = self.extract_field(html, "Мобильный телефон")
        data = {
            "address": address,
            "contract": self.clean_contract(raw_contract),
            "fio": self.extract_field(html, "ФИО"),
            "phone": phone,
        }

        self.log.info(f"  Данные для customer_id={customer_id}:")
        self.log.info(f"    Адрес: {data['address']}")
        self.log.info(f"    Договор: {data['contract']}")
        self.log.info(f"    ФИО: {data['fio']}")
        return data

    def search_and_get_address(self, mac: str) -> Optional[str]:
        """
        Получить адрес абонента в US по MAC.
        Возвращает строку адреса или None.
        """
        cid = self.search_by_mac(mac)
        if not cid:
            return None
        data = self.get_subscriber_data(cid)
        return data.get("address") or None

    def check_session(self) -> bool:
        """
        Проверить, жива ли сессия US, выполнив простой GET.
        Возвращает True если сессия валидна.
        """
        if not self._session:
            return False
        try:
            r = self._session.get(self.base_url, timeout=10)
            return "logout" in r.text.lower() or len(r.text) > 1000
        except Exception:
            return False
