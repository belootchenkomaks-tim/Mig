#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cdata_client.py — SSH-клиент к C-Data OLT.

Выполняет команды на C-Data OLT:
- Запрос свободных слотов/портов
- Сканирование S/N ONT
- Отправка конфигурационных команд
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

# Пароль для C-Data читаем из config.toml (секция [ssh])
SSH_USERNAME = _cfg("ssh", "username", default="admin")
SSH_PASSWORD = _cfg("ssh", "password", default="timercom3")
SSH_TIMEOUT = 15
SSH_PORT = 22


class CDataClient:
    """SSH-клиент для C-Data OLT."""

    def __init__(self, log: Logger):
        self.log = log
        self._ssh: Optional[paramiko.SSHClient] = None
        self._shell: Optional[paramiko.Channel] = None

    def connect(self, ip: str) -> bool:
        """
        SSH-подключение к C-Data OLT.
        """
        if not HAS_PARAMIKO:
            self.log.error("Установите paramiko: pip install paramiko")
            return False

        self.log.info(f"SSH к C-Data {ip} (пользователь: {SSH_USERNAME})")
        try:
            self._ssh = paramiko.SSHClient()
            self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self._ssh.connect(
                ip, port=SSH_PORT,
                username=SSH_USERNAME, password=SSH_PASSWORD,
                timeout=SSH_TIMEOUT, allow_agent=False, look_for_keys=False,
            )
            self._shell = self._ssh.invoke_shell()
            time.sleep(0.5)
            self._shell.recv(4096)  # очищаем буфер приветствия
            self.log.info(f"Подключение к C-Data {ip}: УСПЕХ")
            return True
        except paramiko.AuthenticationException:
            self.log.error(f"Ошибка аутентификации C-Data {ip}")
            return False
        except paramiko.SSHException as e:
            self.log.error(f"SSH ошибка C-Data {ip}: {e}")
            return False
        except Exception as e:
            self.log.error(f"Не удалось подключиться к C-Data {ip}: {e}")
            return False

    def disconnect(self):
        """Закрыть SSH-соединение."""
        if self._shell:
            self._shell.close()
        if self._ssh:
            self._ssh.close()
        self.log.debug("SSH к C-Data закрыт")

    def send_command(self, command: str, wait: float = 0.5) -> str:
        """
        Отправить команду и получить ответ.
        """
        if not self._shell:
            self.log.error("Нет SSH-соединения")
            return ""

        self.log.debug(f"  → {command}")
        self._shell.send(command + "\n")
        time.sleep(wait)
        output = ""
        while self._shell.recv_ready():
            output += self._shell.recv(4096).decode("utf-8", errors="replace")
        self.log.debug(f"  ← {output[:300]}")
        return output

    # ── SNMP ──

    @staticmethod
    def _snmp_path():
        """Путь к snmpwalk.exe."""
        import os
        script_dir = os.path.dirname(os.path.abspath(__file__))
        snmp_dir = os.path.join(script_dir, "snmp")
        if os.path.isdir(snmp_dir):
            return snmp_dir
        return ""

    def _snmp_walk(self, olt_ip: str, oid: str) -> list[tuple[str, str]]:
        """
        SNMP walk на OLT. Возвращает [(index, value), ...].
        Использует snmpwalk.exe из папки snmp/
        """
        import subprocess
        import os

        snmp_dir = self._snmp_path()
        if not snmp_dir:
            self.log.error("snmp/ не найдена")
            return []

        # snmpbulkwalk быстрее — используем его, если есть
        walk_exe = os.path.join(snmp_dir, "snmpbulkwalk.exe")
        if not os.path.exists(walk_exe):
            walk_exe = os.path.join(snmp_dir, "snmpwalk.exe")
        if not os.path.exists(walk_exe):
            self.log.error(f"snmpwalk.exe не найден: {walk_exe}")
            return []

        community = "timercom"
        args = [
            walk_exe, "-v2c", "-c", community,
            "-Cr100", "-Cc",  # bulk-параметры: 100 записей за раз
            "-r", "2", "-t", "5",
            "-On", olt_ip, oid,
        ]
        self.log.debug(f"SNMP walk: {' '.join(args)}")

        try:
            result = subprocess.run(
                args, capture_output=True, text=True,
                timeout=30, cwd=snmp_dir
            )
        except subprocess.TimeoutExpired:
            self.log.warn(f"SNMP walk timeout: {olt_ip}")
            return []
        except Exception as e:
            self.log.warn(f"SNMP walk error: {e}")
            return []

        if result.returncode != 0:
            self.log.warn(f"SNMP walk вернул код {result.returncode}")
            return []

        results = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            # Парсинг: .1.3.6.1.2.1.2.2.1.2.123 = STRING: "pon0/0/1:2"
            m = re.search(r'\.(\d+)\s*=\s*STRING:\s*"?([^"\n]+?)"?\s*$', line)
            if m:
                results.append((m.group(1), m.group(2).strip()))
        return results

    # ── Свободные слоты ──

    def get_free_slots(self, olt_ip: str, channel: str, needed: int = 0) -> list[dict]:
        """
        Получить свободные ONT-слоты на C-Data через SNMP.

        Алгоритм (из gpon_diagv2.py):
          1. SNMP walk ifDescr (.1.3.6.1.2.1.2.2.1.2)
          2. Парсим имена интерфейсов: pon0/0/{trunk}:{ont_id}
          3. Собираем занятые ONT ID на указанном стволе
          4. Свободные = пропуски в последовательности от 1 до 128

        channel — номер ствола (например "0", "3", "5")
        needed  — сколько слотов требуется (0 = не проверять)
        Возвращает список: [{"port": trunk, "ont_id": id, "free": True}, ...]
        """
        self.log.info(f"C-Data SNMP: свободные слоты на {olt_ip}, ствол {channel}")

        OID_IFDESCR = ".1.3.6.1.2.1.2.2.1.2"
        entries = self._snmp_walk(olt_ip, OID_IFDESCR)
        if not entries:
            self.log.warn("  SNMP не вернул данные")
            return []

        # Собираем занятые ONT ID на запрошенном стволе
        occupied_ids = set()
        for idx, name in entries:
            m = re.search(r"pon0/0/(\d+):(\d+)", name)
            if m:
                trunk = m.group(1)
                ont_id = int(m.group(2))
                if trunk == channel:
                    occupied_ids.add(ont_id)

        # Свободные = все ID 1..128, не занятые
        free_slots = []
        for ont_id in range(1, 129):
            if ont_id not in occupied_ids:
                free_slots.append({
                    "port": channel,
                    "ont_id": ont_id,
                    "free": True,
                })

        self.log.info(f"  Занято на стволе {channel}: {len(occupied_ids)}, "
                      f"свободно: {len(free_slots)}")
        if needed > 0 and len(free_slots) < needed:
            self.log.warn(f"  ⚠ На 1 стволе только {len(free_slots)} слотов, "
                          f"а нужно {needed}. Добавьте стволы вручную в CSV колонку Chan_cdata.")

        return free_slots

    def scan_sn(self, olt_ip: str, port: str) -> Optional[str]:
        """
        Сканировать S/N ONT на указанном порту C-Data.
        Возвращает серийный номер или None.
        """
        self.log.info(f"C-Data: сканирование S/N на порту {port}")
        # Команда на C-Data: show ont optical-info interface gpon 0/1/1
        # Пока заглушка
        return "SN-UNKNOWN"

    def apply_config(self, commands: list[str]) -> bool:
        """
        Отправить конфигурационные команды на C-Data.
        """
        self.log.info(f"C-Data: отправка {len(commands)} команд")
        for i, cmd in enumerate(commands, 1):
            resp = self.send_command(cmd, wait=0.3)
            self.log.debug(f"  [{i}/{len(commands)}] {cmd}: {resp[:100]}")
        return True
