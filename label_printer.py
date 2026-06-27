#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
label_printer.py — Печать этикетки для ONT (как в оригинальном Сальск.exe).

После сканирования S/N печатает этикетку через GDI (win32ui) на CHITENG-CT221B.
Формат: 40×58мм, Courier New 12pt.
"""

import os
from log_utils import Logger

try:
    import win32print
    import win32ui
    import win32con
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False


PRINTER_NAME = "CHITENG-CT221B"

# Параметры этикетки (как в оригинале)
LABEL_W_MM = 40      # короткая сторона (по вертикали после поворота, ландшафт)
LABEL_H_MM = 58      # длинная сторона (по горизонтали после поворота)
DRV_MARGIN_L_MM = 3  # отступ слева (уже в DC драйвера)
DRV_MARGIN_T_MM = 3  # отступ сверху
PRINTABLE_W_MM = LABEL_H_MM - DRV_MARGIN_L_MM * 2  # 52мм — ширина текста
FONT_NAME = "Courier New"
FONT_SIZE = 11       # points (чуть меньше оригинала, чтобы длинные адреса влезали)
LINE_SPACING_MM = 1.0


def print_label(row: dict, log: Logger) -> bool:
    """
    Печать этикетки для одной ONT через GDI на CHITENG-CT221B.

    row — словарь с данными абонента.
    Возвращает True если этикетка отправлена на печать.
    """
    if not HAS_WIN32:
        log.warn("Печать этикетки: нет win32print (работает только на Windows)")
        return False

    address = row.get("address_us", "")
    chan = row.get("chan_cdata", "")
    sn = row.get("sn", "")
    desc = row.get("desc", "")
    old_vlan = row.get("old_vlan_olt", "")
    new_vlan = row.get("new_vlan", "")

    # Формируем текст этикетки
    lines = []
    if address:
        lines.append(address)
    if chan:
        lines.append(f"Ствол: {chan}")

    text = "\n".join(lines)
    log.info(f"Печать этикетки: {address or desc} (ствол: {chan})")

    try:
        # ── Ищем принтер ──
        printers = win32print.EnumPrinters(
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        )
        printer_found = None
        for p in printers:
            pname = p[2]  # (flags, desc, name, comment)
            if PRINTER_NAME in pname:
                printer_found = pname
                break

        if not printer_found:
            log.warn(f"Принтер '{PRINTER_NAME}' не найден. Использую принтер по умолчанию.")
            printer_found = win32print.GetDefaultPrinter()
            if not printer_found:
                log.warn("Нет принтера по умолчанию. Печать отменена.")
                return False

        log.debug(f"Принтер: {printer_found}")

        # ── GDI-печать (как в оригинале) ──
        text_lines = text.split('\n')
        dc = win32ui.CreateDC()
        dc.CreatePrinterDC(printer_found)

        try:
            dc.StartDoc("ONT Label")
            dc.StartPage()

            # MM_LOMETRIC: 1 единица = 0.1мм
            dc.SetMapMode(win32con.MM_LOMETRIC)

            # Создаём шрифт
            font_height = -int(FONT_SIZE * 254 / 72)  # points → 0.1мм
            font = win32ui.CreateFont({
                "name": FONT_NAME,
                "height": font_height,
                "weight": 400,
                "charset": 204,  # RUSSIAN_CHARSET
            })
            dc.SelectObject(font)

            # Метрики (в мм, т.к. MM_LOMETRIC: 1ед = 0.1мм)
            tm = dc.GetTextMetrics()
            char_height_mm = tm['tmHeight'] / 10
            ave_char_width_mm = tm['tmAveCharWidth'] / 10
            line_height_mm = char_height_mm + LINE_SPACING_MM

            # Максимум символов в строке (52мм / ширина символа)
            max_chars = max(1, int(PRINTABLE_W_MM / ave_char_width_mm) - 1)

            # Разбиваем длинные строки с переносом по словам
            wrapped_lines = []
            for line in text_lines:
                while len(line) > max_chars:
                    # Ищем место разрыва (пробел перед max_chars)
                    brk = line.rfind(' ', 0, max_chars)
                    if brk < 1:
                        brk = max_chars  # нет пробела — режем по длине
                    wrapped_lines.append(line[:brk].strip())
                    line = line[brk:].strip()
                if line:
                    wrapped_lines.append(line)

            # Рисуем строки (Y растёт вниз в отрицательных координатах)
            y_pos = -int(DRV_MARGIN_T_MM * 10)  # -30 (0.1мм)
            for line in wrapped_lines:
                dc.TextOut(int(DRV_MARGIN_L_MM * 10), y_pos, line)
                y_pos -= int(line_height_mm * 10)

            dc.EndPage()
            dc.EndDoc()
            log.info("✅ Этикетка отправлена на печать")
            return True

        finally:
            dc.DeleteDC()

    except Exception as e:
        log.warn(f"Ошибка печати этикетки: {e}")
        return False
