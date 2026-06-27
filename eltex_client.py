#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eltex_client.py — SSH к старому OLT (Eltex LTE-8ST).

Получает VLAN ONU по MAC-адресу через CLI:
  1. ont_mac <MAC>
  2. rule show pon
  3. Парсим VID из правил (if VID == XXXX)

Использует ОДНО SSH-соединение на OLT. Переключение между MAC
через повторный ont_mac с полным дренажем буфера между командами.
"""

import re
import time
from typing import Optional
from log_utils import Logger

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False


from config_loader import get as _cfg

# Пароль для Eltex LTE читаем из config.toml (секция [ssh] или [lte])
SSH_USERNAME = _cfg("ssh", "username", default="admin")
# Сначала пробуем [lte][ssh_password], потом [ssh][password]
SSH_PASSWORD = _cfg("lte", "ssh_password",
                    default=_cfg("ssh", "password", default="password"))
SSH_TIMEOUT = 10


class EltexClient:
    """SSH-клиент для Eltex LTE OLT. Одно соединение на OLT."""

    def __init__(self, log: Logger):
        self.log = log
        self._ssh: Optional[paramiko.SSHClient] = None
        self._channel: Optional[paramiko.Channel] = None
        self._connected_ip: str = ""

    def connect(self, ip: str) -> bool:
        """Открыть SSH-соединение к OLT."""
        if self._ssh and self._connected_ip == ip:
            return True  # уже подключены

        self.disconnect()

        if not HAS_PARAMIKO:
            self.log.error("Установите paramiko: pip install paramiko")
            return False

        self.log.info(f"SSH к Eltex {ip} (admin/***)")
        try:
            self._ssh = paramiko.SSHClient()
            self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self._ssh.connect(
                ip, username=SSH_USERNAME, password=SSH_PASSWORD,
                timeout=SSH_TIMEOUT, look_for_keys=False, allow_agent=False,
            )
            self._channel = self._ssh.invoke_shell()
            time.sleep(0.5)
            self._channel.recv(8192)  # очищаем баннер
            self._connected_ip = ip
            self.log.info(f"Подключение к Eltex {ip}: УСПЕХ")
            return True
        except paramiko.AuthenticationException:
            self.log.error(f"Ошибка аутентификации Eltex {ip}")
            return False
        except Exception as e:
            self.log.error(f"Не удалось подключиться к Eltex {ip}: {e}")
            return False

    def disconnect(self):
        """Закрыть SSH."""
        if self._channel:
            try: self._channel.close()
            except: pass
            self._channel = None
        if self._ssh:
            try: self._ssh.close()
            except: pass
            self._ssh = None
        self._connected_ip = ""

    def get_vlans_batch(self, macs: list[str],
                        on_progress=None) -> list[tuple[str, str]]:
        """
        Получить VLAN для списка MAC в одной сессии.

        macs — список MAC-адресов.
        on_progress — callback(mac, vlan_or_None, current, total)

        Возвращает список (mac, vlan).
        """
        results = []
        total = len(macs)
        for idx, mac in enumerate(macs, 1):
            vlan = self.get_vlan_by_mac(self._connected_ip or "", mac)
            results.append((mac, vlan or ""))
            if on_progress:
                try:
                    on_progress(mac, vlan, idx, total)
                except Exception:
                    pass
            # Если соединение упало — переподключаемся
            if not self._channel and idx < total:
                self.log.warn("  Соединение потеряно, переподключаюсь...")
                if not self.connect(self._connected_ip):
                    self.log.error("  Не удалось переподключиться")
                    break
        return results

    def get_vlan_by_mac(self, olt_ip: str, mac: str) -> dict:
        """
        Получить VLAN и тип подключения для ONU по её MAC.

        Алгоритм:
          1. connect(olt_ip) — SSH к OLT
          2. ont_mac <MAC>            — вход в контекст ONT
          3. rule show pon            — показать PON-правила (ищем VID)
          4. show config              — показать конфиг ONT (ищем Rules profile)
          5. exit                     — выход в корневой промпт

        Возвращает dict:
          {"vlan": str или None, "is_pppoe": bool}
        """
        if not self.connect(olt_ip):
            self.log.error(f"Не удалось подключиться к Eltex {olt_ip}")
            return {"vlan": None, "is_pppoe": False}

        if not self._channel:
            self.log.error("Нет SSH-соединения")
            return {"vlan": None, "is_pppoe": False}

        self.log.info(f"Запрос VLAN+PPPoE для MAC {mac} на {olt_ip}")

        try:
            # 0. Сброс буфера: читаем всё что осталось от предыдущей команды
            time.sleep(0.2)
            while self._channel.recv_ready():
                self._channel.recv(65535)

            # 1. Вход в контекст ONT
            self._channel.send(f"ont_mac {mac}\n")
            time.sleep(0.5)

            # 2. PON-правила (ищем VID)
            self._channel.send("rule show pon\n")
            time.sleep(0.5)

            # 3. Конфиг ONT (ищем Rules profile с PPPoE)
            self._channel.send("show config\n")
            time.sleep(0.5)

            # 4. Выход
            self._channel.send("exit\n")
            time.sleep(0.5)

            # 5. Читаем всё разом (макс 5 проходов × 0.5 сек = 2.5 сек)
            out = b""
            for _ in range(5):
                time.sleep(0.5)
                try:
                    while self._channel.recv_ready():
                        chunk = self._channel.recv(65535)
                        out += chunk
                except Exception:
                    pass
            resp = out.decode("utf-8", errors="replace")

            if "invalid" in resp.lower() or "not found" in resp.lower():
                self.log.warn(f"  ONT с MAC {mac} не найден")
                return {"vlan": None, "is_pppoe": False}

            # 5. Ищем VID: "if (VID == 2031)"
            vlan = None
            m = re.search(r"if\s*\(VID\s*==\s*(\d+)\)", resp)
            if m:
                vlan = m.group(1)
                self.log.info(f"  VLAN: {vlan}")
            else:
                self.log.warn(f"  VLAN не найден для MAC {mac}")

            # 6. Ищем Rules profile (PPPoE если в названии есть PPPoE)
            is_pppoe = bool(re.search(r'Rules profile\s*:\s*\d+\s+.+PPPoE', resp))
            self.log.info(f"  PPPoE: {is_pppoe}")

            return {"vlan": vlan, "is_pppoe": is_pppoe}

        except Exception as e:
            self.log.warn(f"  Ошибка при запросе VLAN для {mac}: {e}")
            self.disconnect()
            return {"vlan": None, "is_pppoe": False}
