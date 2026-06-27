#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrator.py — Пошаговый мигратор абонентов OLT.
Шаг 1: загружаем CSV, добавляем 11 колонок (G-Q), выполняем пункт 2 ТЗ.
"""

import os
import csv
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime

from log_utils import Logger
from excel_utils import read_input_csv, olt_name_to_ip


# ── Поля для CSV-снэпшота (порядок колонок A-Q) ──
SNAPSHOT_FIELDS = [
    "olt", "chan", "id", "mac", "desc", "rssi",          # A-F
    "olt_cdata", "chan_cdata",                             # G-H
    "old_vlan_olt", "address_us", "old_vlan_billing",     # I-K
    "contract", "phone", "new_vlan",                       # L-N
    "sn", "formula", "note",                               # O-Q
]

SNAPSHOT_HEADERS = [
    "Номер ОЛТ Элтекс", "Ствол", "ID", "MAC", "Описание", "Уровень, dBm",  # A-F
    "Номер ОЛТ CData", "Ствол, номер на стволе",                            # G-H
    "Старый влан ОЛТ", "Адрес в US", "Старый влан биллинг",                 # I-K
    "Номер договора", "Телефон", "Новые вланы",                              # L-N
    "S/N", "Формула", "Примечание",                                          # O-Q
]


def _snapshot_path(base_dir: str) -> str:
    return os.path.join(base_dir, "snapshot_step1.csv")


def load_snapshot(base_dir: str) -> list[dict] | None:
    """Загрузить промежуточное состояние из CSV."""
    path = _snapshot_path(base_dir)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        rows = list(reader)
    if not rows:
        return None
    # Мапим русские заголовки обратно в ключи
    header_to_key = dict(zip(SNAPSHOT_HEADERS, SNAPSHOT_FIELDS))
    result = []
    for row in rows:
        mapped = {}
        for rus_header, val in row.items():
            key = header_to_key.get(rus_header, rus_header)
            mapped[key] = val
        result.append(mapped)
    return result


def save_snapshot(data: list[dict], base_dir: str):
    """Сохранить промежуточное состояние в CSV (с BOM для Excel)."""
    path = _snapshot_path(base_dir)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        # Пишем русскоязычные заголовки, а данные — по ключам
        writer = csv.DictWriter(f, fieldnames=SNAPSHOT_FIELDS, delimiter=";",
                                extrasaction="ignore")
        # Свои заголовки вместо fieldnames
        header_row = dict(zip(SNAPSHOT_FIELDS, SNAPSHOT_HEADERS))
        writer.writerow(header_row)
        for row in data:
            writer.writerow(row)
    print(f"  💾 Снэпшот: {path}")
    print(f"  📊 Открой в Excel: {path}")


# ── Главная функция шага 1 ──

def step1_add_columns_and_header(
    input_path: str,
    output_dir: str,
    from_olt: str,     # например "Salsk107"
    from_chan: str,    # например "3"
    to_olt_ip: str,    # например "172.18.0.200"
    to_chan: str,      # например "5"
    log: Logger,
) -> list[dict] | None:
    """
    Шаг 1 (пункт 2 ТЗ):
      1. Загружаем CSV (A-F)
      2. Добавляем 11 пустых колонок (G-Q)
      3. Добавляем заголовок "X/Y на A/B"
      4. Заполняем G-H (целевой OLT C-Data и ствол)

    Возвращает список словарей — строки таблицы.
    """
    log.section("ШАГ 1: ДОБАВЛЕНИЕ КОЛОНОК И ЗАГОЛОВКА (П.2 ТЗ)")

    # 1. Загрузка
    log.info(f"Входной файл: {input_path}")
    rows = read_input_csv(input_path, log)
    if not rows:
        log.fatal("Нет данных для обработки")
        return None

    log.info(f"Загружено строк: {len(rows)}")

    # 2. Формируем заголовок
    # Из from_olt извлекаем число (Salsk107 → 107)
    import re
    m = re.search(r'(\d+)$', from_olt)
    from_num = m.group(1) if m else from_olt
    # Из to_olt_ip извлекаем последний октет (172.18.0.200 → 200)
    m2 = re.search(r'(\d+)$', to_olt_ip)
    to_num = m2.group(1) if m2 else to_olt_ip

    header = f"{from_num}/{from_chan} на {to_num}/{to_chan}"
    log.info(f"Заголовок таблицы: {header}")

    # 3. Добавляем 11 колонок к каждой строке
    data = []
    for i, r in enumerate(rows, 1):
        row = {
            # A-F из CSV
            "olt": olt_name_to_ip(r["olt"], log),  # Salsk107 → 172.18.0.107
            "chan": r["chan"],
            "id": str(i),                     # 1, 2, 3...
            "mac": r["mac"],
            "desc": r["desc"],
            "rssi": r["rssi"],
            # G-H: целевой OLT (заполняем)
            "olt_cdata": to_olt_ip,
            "chan_cdata": f"{to_chan}/{i}",   # ствол/номер (5/1, 5/2...)
            # I-Q: пока пустые
            "old_vlan_olt": "",
            "address_us": "",
            "old_vlan_billing": "",
            "contract": "",
            "phone": "",
            "new_vlan": "",
            "sn": "",
            "formula": "",
            "note": "",
        }
        data.append(row)
        log.debug(f"  [{i:3d}] {row['desc']:30s} → C-Data {to_olt_ip}/{to_chan}")

    log.info(f"Колонки G, H заполнены: OLT={to_olt_ip}, ствол={to_chan}")
    log.info(f"Колонки I-Q созданы (пустые)")
    log.info(f"Всего колонок: A(6) + G-Q(11) = 17")
    log.info(f"Результат: {len(data)} строк")

    # Сохраняем снэпшот
    save_snapshot(data, output_dir)
    log.info(f"Снэпшот сохранён для следующих шагов")
    log.info(f"")
    log.info(f"Следующий шаг: колонка I — запрос VLAN со старого OLT (Eltex)")

    return data


# ── GUI для ввода параметров ──

class Step1Dialog:
    """Простое окно для ввода параметров шага 1."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Шаг 1 — Параметры миграции")
        self.root.geometry("600x400")
        self.root.configure(bg="#E9E9E1")

        self.result = None

        # Переменные
        self.input_path = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.from_olt = tk.StringVar(value="Salsk107")
        self.from_chan = tk.StringVar(value="3")
        self.to_olt_ip = tk.StringVar(value="172.18.0.200")
        self.to_chan = tk.StringVar(value="5")

        self._build_ui()
        self._center_window()

    def _center_window(self):
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill=tk.BOTH, expand=True)

        tk.Label(main, text="Шаг 1 — Параметры миграции",
                 font=("Segoe UI", 14, "bold"), fg="#1E376E",
                 bg="#E9E9E1").pack(anchor="w", pady=(0, 16))

        # Файлы
        frm = ttk.LabelFrame(main, text=" Файлы ", padding=10)
        frm.pack(fill=tk.X, pady=(0, 8))

        for i, (label, var, cmd) in enumerate([
            ("Входной CSV:", self.input_path, self._browse_input),
            ("Папка сохранения:", self.output_dir, self._browse_output),
        ]):
            tk.Label(frm, text=label, anchor="e", width=16,
                     font=("Segoe UI", 10)).grid(row=i, column=0, sticky="e", padx=(0, 8), pady=4)
            ttk.Entry(frm, textvariable=var, font=("Segoe UI", 10)).grid(
                row=i, column=1, sticky="ew", pady=4)
            tk.Button(frm, text="…", command=cmd, width=3,
                     font=("Segoe UI", 9)).grid(row=i, column=2, padx=(4, 0), pady=4)
            frm.columnconfigure(1, weight=1)

        # Параметры
        frm2 = ttk.LabelFrame(main, text=" Параметры (пункт 2 ТЗ) ", padding=10)
        frm2.pack(fill=tk.X, pady=(0, 16))

        # Откуда
        sf = ttk.Frame(frm2)
        sf.pack(fill=tk.X, pady=4)
        tk.Label(sf, text="Откуда (OLT):", font=("Segoe UI", 10)).pack(side=tk.LEFT)
        ttk.Entry(sf, textvariable=self.from_olt, width=14,
                  font=("Segoe UI", 10)).pack(side=tk.LEFT, padx=(8, 4))
        tk.Label(sf, text="ствол:", font=("Segoe UI", 10)).pack(side=tk.LEFT)
        ttk.Entry(sf, textvariable=self.from_chan, width=6,
                  font=("Segoe UI", 10)).pack(side=tk.LEFT, padx=(8, 0))

        # Куда
        sf2 = ttk.Frame(frm2)
        sf2.pack(fill=tk.X, pady=4)
        tk.Label(sf2, text="Куда (C-Data IP):", font=("Segoe UI", 10)).pack(side=tk.LEFT)
        ttk.Entry(sf2, textvariable=self.to_olt_ip, width=18,
                  font=("Segoe UI", 10)).pack(side=tk.LEFT, padx=(8, 4))
        tk.Label(sf2, text="ствол:", font=("Segoe UI", 10)).pack(side=tk.LEFT)
        ttk.Entry(sf2, textvariable=self.to_chan, width=6,
                  font=("Segoe UI", 10)).pack(side=tk.LEFT, padx=(8, 0))

        # Кнопка
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X)

        tk.Button(btn_frame, text="✅ Выполнить шаг 1",
                  font=("Segoe UI", 11, "bold"),
                  bg="#1E376E", fg="white", padx=20, pady=6,
                  command=self._on_ok, cursor="hand2").pack(side=tk.LEFT)

        tk.Button(btn_frame, text="Отмена",
                  font=("Segoe UI", 10),
                  command=self.root.destroy).pack(side=tk.RIGHT, padx=(8, 0))

    def _browse_input(self):
        path = filedialog.askopenfilename(
            title="Входной CSV",
            filetypes=[("CSV", "*.csv"), ("Excel", "*.xlsx"), ("Все", "*.*")]
        )
        if path:
            self.input_path.set(path)
            # Авто-заполнение папки сохранения
            if not self.output_dir.get():
                base = os.path.dirname(path)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                self.output_dir.set(os.path.join(base, f"migration_{ts}"))

    def _browse_output(self):
        path = filedialog.askdirectory(title="Папка для результатов")
        if path:
            self.output_dir.set(path)

    def _on_ok(self):
        # Проверки
        in_path = self.input_path.get().strip()
        if not in_path or not os.path.exists(in_path):
            messagebox.showerror("Ошибка", "Укажите существующий входной файл")
            return

        out_dir = self.output_dir.get().strip()
        if not out_dir:
            out_dir = os.path.join(os.path.dirname(in_path),
                                   f"migration_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            self.output_dir.set(out_dir)

        self.result = {
            "input_path": in_path,
            "output_dir": out_dir,
            "from_olt": self.from_olt.get().strip(),
            "from_chan": self.from_chan.get().strip(),
            "to_olt_ip": self.to_olt_ip.get().strip(),
            "to_chan": self.to_chan.get().strip(),
        }
        self.root.destroy()

    def run(self):
        self.root.mainloop()
        return self.result


# ── Точка входа ──

def main():
    # Диалог
    dlg = Step1Dialog()
    params = dlg.run()
    if not params:
        print("  ❌ Отменено пользователем")
        return

    # Логгер
    log = Logger(params["output_dir"], f"step1_{datetime.now().strftime('%H%M%S')}")
    log.info(f"Параметры: {params}")

    # Выполняем шаг 1
    data = step1_add_columns_and_header(
        input_path=params["input_path"],
        output_dir=params["output_dir"],
        from_olt=params["from_olt"],
        from_chan=params["from_chan"],
        to_olt_ip=params["to_olt_ip"],
        to_chan=params["to_chan"],
        log=log,
    )

    if data:
        log.info(f"")
        log.info(f"✅ Шаг 1 выполнен. Данные готовы для шага 2 (колонка I).")
        print(f"\n  ✅ Шаг 1 выполнен. Снэпшот: {_snapshot_path(params['output_dir'])}")

    log.close()


if __name__ == "__main__":
    main()
