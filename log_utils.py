#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
log_utils.py — Фундаментальный модуль логирования.

Логирование — основа приложения. Каждый шаг, запрос, ответ, ошибка
записываются с таймстемпом и уровнем. Дублируется в GUI через callback.

Уровни:
  DEBUG   — детали запросов/ответов, SNMP-данные
  INFO    — шаги выполнения, прогресс
  WARN    — нештатные ситуации (не найден адрес, расхождение VLAN)
  ERROR   — ошибки, прерывающие шаг
  FATAL   — критическая ошибка, остановка всего процесса
"""

import os
import sys
import time
import threading
from datetime import datetime
from typing import Optional, Callable


class Logger:
    """Потокобезопасный логгер с записью в файл + callback в GUI."""

    LEVELS = {
        "DEBUG": 10,
        "INFO": 20,
        "WARN": 30,
        "ERROR": 40,
        "FATAL": 50,
    }

    def __init__(self, log_dir: str, session_name: str = ""):
        """
        log_dir — куда писать лог-файл.
        session_name — метка сессии (по умолчанию дата).
        """
        os.makedirs(log_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[str, str], None]] = []
        self._log_count = 0

        # Имя сессии
        if not session_name:
            session_name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        self.session_name = session_name
        self.log_path = os.path.join(log_dir, f"migration_{session_name}.log")
        self._file = open(self.log_path, "w", encoding="utf-8")

        # Шапка лога
        self._write_separator()
        self._write_line(f"СЕССИЯ: {session_name}")
        self._write_line(f"ПУСК: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
        self._write_separator()

    def _write_line(self, text: str):
        """Запись строки в файл (без блокировки — lock снаружи)."""
        self._file.write(text + "\n")
        self._file.flush()

    def _write_separator(self):
        self._write_line("=" * 80)

    def on_log(self, callback: Callable[[str, str], None]):
        """Подписаться на логи: callback(level, message)."""
        self._callbacks.append(callback)

    def log(self, level: str, message: str, *details):
        """
        Основной метод логирования.

        level: DEBUG / INFO / WARN / ERROR / FATAL
        message: текст сообщения
        details: доп. строки (будут с отступом и >>)
        """
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        level_pad = level.ljust(5)
        line = f"[{ts}] [{level_pad}] {message}"

        with self._lock:
            self._write_line(line)
            for d in details:
                self._write_line(f"         >> {d}")
            self._log_count += 1
            self._file.flush()

        # Callback в GUI (вне блокировки)
        for cb in self._callbacks:
            try:
                cb(level, message)
            except Exception:
                pass  # GUI мог закрыться

    def debug(self, msg: str, *details):
        self.log("DEBUG", msg, *details)

    def info(self, msg: str, *details):
        self.log("INFO", msg, *details)

    def warn(self, msg: str, *details):
        self.log("WARN", msg, *details)

    def error(self, msg: str, *details):
        self.log("ERROR", msg, *details)

    def fatal(self, msg: str, *details):
        self.log("FATAL", msg, *details)

    def section(self, title: str):
        """Начать новый раздел в логе."""
        with self._lock:
            self._write_line("")
            self._write_line(f"--- {title} ---")
            self._write_line("")

    def close(self):
        """Закрыть лог."""
        with self._lock:
            self._write_separator()
            self._write_line(f"ИТОГО: {self._log_count} записей")
            self._write_line(f"ЗАВЕРШЕНИЕ: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
            self._write_separator()
            self._file.close()

    @property
    def total_count(self) -> int:
        return self._log_count


# ── Удобная функция для создания логгера по умолчанию ──

def create_logger(base_dir: str = None) -> Logger:
    """Создать логгер в папке logs/ рядом со скриптом."""
    if base_dir is None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(base_dir, "logs")
    return Logger(log_dir)
