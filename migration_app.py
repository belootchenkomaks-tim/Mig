#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migration_app.py — GUI для миграции абонентов OLT.

Минималистичный интерфейс:
  - Поля: входной файл, папка сохранения, параметры миграции
  - Авторизация Billing (логин/пароль)
  - Кнопка СТАРТ
  - Progress bar + окно лога
"""

import os
import sys
import csv
import json
import threading
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from datetime import datetime
from log_utils import Logger
from orchestrator import MigrationOrchestrator
import updater


# ── Цвета (корпоративный стиль) ──
C = {
    "bg": "#E9E9E1",
    "hdr": "#1E376E",
    "white": "#FFFFFF",
    "sep": "#D4D4D4",
    "green": "#2E7D32",
    "red": "#C62828",
}


# ── Вспомогательные функции для коррекции вставки в Entry ──

def _safe_paste(event):
    """Вставить из буфера обмена в Entry (любая раскладка)."""
    try:
        clipboard = event.widget.clipboard_get()
        event.widget.insert(tk.INSERT, clipboard)
    except tk.TclError:
        pass
    return "break"

def _safe_copy(entry):
    """Скопировать выделенное из Entry в буфер."""
    try:
        selected = entry.selection_get()
        entry.clipboard_clear()
        entry.clipboard_append(selected)
    except tk.TclError:
        pass
    return "break"

def _safe_cut(entry):
    """Вырезать выделенное из Entry."""
    try:
        selected = entry.selection_get()
        entry.clipboard_clear()
        entry.clipboard_append(selected)
        entry.delete(tk.SEL_FIRST, tk.SEL_LAST)
    except tk.TclError:
        pass
    return "break"


def _paste_to_entry(entry):
    """Вставить текст в Entry (из контекстного меню)."""
    try:
        text = entry.clipboard_get()
        entry.insert(tk.INSERT, text)
    except tk.TclError:
        pass


def _add_context_menu(entry):
    """Добавить контекстное меню (правая кнопка мыши)."""
    menu = tk.Menu(entry, tearoff=0)
    menu.add_command(label="Вырезать", command=lambda: _safe_cut(entry))
    menu.add_command(label="Копировать", command=lambda: _safe_copy(entry))
    menu.add_command(label="Вставить", command=lambda: _paste_to_entry(entry))
    menu.add_separator()
    menu.add_command(label="Выделить всё",
                     command=lambda: entry.selection_range(0, tk.END))

    def _show(event):
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
    entry.bind("<Button-3>", _show, add="+")


class MigrationApp:
    """Главное окно приложения."""

    def __init__(self):
        self.root = tk.Tk()
        _ver = updater.APP_VERSION
        self.root.title(f"Миграция абонентов OLT v{_ver}")
        self.root.geometry("900x700")
        self.root.minsize(800, 600)
        self.root.configure(bg=C["bg"])

        # Логгер (создаётся при старте)
        self.log: Logger = None
        self.orchestrator: MigrationOrchestrator = None
        self._running = False
        self._skip_all_sn = False

        # ETA: плавный обратный отсчёт (секундомер)
        self._eta_seconds = 0
        self._eta_timer_id = None

        # Переменные формы
        self._in_path = tk.StringVar()
        self._out_dir = tk.StringVar()
        self._bill_login = tk.StringVar()
        self._bill_pass = tk.StringVar()

        # Автозаполнение billing из billing_auth.txt
        self._autofill_billing()

        # Попытка установить иконку
        try:
            icon_path = os.path.join(os.path.dirname(__file__), "icon.ico")
            if os.path.exists(icon_path):
                self.root.iconbitmap(icon_path)
        except Exception:
            pass

        self._build_ui()
        self._center_window()
        self._fix_entry_bindings()

    def _autofill_billing(self):
        """Автозаполнить логин/пароль из billing_auth.txt."""
        auth_path = os.path.join(os.path.dirname(__file__), "billing_auth.txt")
        if os.path.exists(auth_path):
            try:
                with open(auth_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._bill_login.set(data.get("login", ""))
                self._bill_pass.set(data.get("password", ""))
            except Exception:
                pass

    # ── Исправление вставки в Entry (для любой раскладки) ──

    def _fix_entry_bindings(self):
        """Привязать Ctrl+V, контекстное меню ко всем Entry (любая раскладка)."""
        self._apply_to_all_entries(self.root)

    def _apply_to_all_entries(self, parent):
        """Рекурсивно обойти виджеты и поправить Entry."""
        for child in parent.winfo_children():
            if isinstance(child, (tk.Entry, ttk.Entry)):
                self._add_entry_bindings(child)
            self._apply_to_all_entries(child)

    @staticmethod
    def _add_entry_bindings(entry):
        """Привязки для вставки/копирования — Ctrl+Ins / Shift+Ins (любая раскладка)."""
        entry.bind("<<Paste>>", _safe_paste)  # заменяет встроенный
        entry.bind("<<Copy>>", lambda e: _safe_copy(e.widget))
        entry.bind("<<Cut>>", lambda e: _safe_cut(e.widget))
        # Shift+Insert = вставка (системный shortcut, работает всегда)
        entry.bind("<Shift-KeyPress-Insert>", _safe_paste)
        # Ctrl+Ins = копирование
        entry.bind("<Control-KeyPress-Insert>", lambda e: _safe_copy(e.widget))
        # Контекстное меню
        _add_context_menu(entry)

    @staticmethod
    def _read_csv_olt_chan(path: str) -> tuple:
        """Прочитать 'Откуда' (OLT, ствол) из первой строки CSV."""
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f, delimiter=";")
                for row in reader:
                    olt = row.get("OLT", "").strip()
                    chan = row.get("Chan", "").strip()
                    if olt and chan:
                        return olt, chan
        except Exception:
            pass
        return "", ""

    def _center_window(self):
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # ── Построение UI ──

    def _build_ui(self):
        # Основной контейнер с отступами
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill=tk.BOTH, expand=True)

        # ── Верх: заголовок + кнопка обновления ──
        header_frame = tk.Frame(main, bg=C["bg"])
        header_frame.pack(fill=tk.X, pady=(0, 16))

        header = tk.Label(
            header_frame, text=f"Миграция абонентов OLT  v{updater.APP_VERSION}",
            font=("Segoe UI", 16, "bold"),
            fg=C["hdr"], bg=C["bg"]
        )
        header.pack(side=tk.LEFT)

        self._update_btn = tk.Button(
            header_frame, text="🔄",
            font=("Segoe UI", 10),
            bg=C["hdr"], fg="white",
            width=3, height=1,
            cursor="hand2",
            command=self._check_updates,
        )
        self._update_btn.pack(side=tk.RIGHT, padx=(8, 0))

        # ── Параметры (2 колонки) ──
        params_frame = ttk.LabelFrame(main, text=" Параметры миграции ", padding=12)
        params_frame.pack(fill=tk.X, pady=(0, 12))

        # Сетка: label + поле
        for i, (label, var, btn_cmd) in enumerate([
            ("Входной CSV/XLSX:", self._in_path, self._browse_input),
            ("Папка сохранения:", self._out_dir, self._browse_output),
        ]):
            tk.Label(params_frame, text=label, anchor="e", width=18,
                     font=("Segoe UI", 10)).grid(row=i, column=0, sticky="e", padx=(0, 8), pady=4)
            entry = tk.Entry(params_frame, textvariable=var,
                             font=("Segoe UI", 10), bg=C["white"])
            entry.grid(row=i, column=1, sticky="ew", pady=4)
            btn = tk.Button(params_frame, text="…", command=btn_cmd,
                           font=("Segoe UI", 9), width=3)
            btn.grid(row=i, column=2, padx=(4, 0), pady=4)
        params_frame.columnconfigure(1, weight=1)

        # Куда (целевой OLT и ствол)
        dir_frame = ttk.Frame(params_frame)
        dir_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        tk.Label(dir_frame, text="  →  Куда:", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        tk.Label(dir_frame, text="C-Data OLT", font=("Segoe UI", 10)).pack(side=tk.LEFT, padx=(8, 0))
        self._to_olt = tk.StringVar(value="200")
        ttk.Entry(dir_frame, textvariable=self._to_olt, width=12,
                  font=("Segoe UI", 10)).pack(side=tk.LEFT, padx=(4, 2))
        tk.Label(dir_frame, text="ствол", font=("Segoe UI", 10)).pack(side=tk.LEFT)
        self._to_chan = tk.StringVar(value="5")
        ttk.Entry(dir_frame, textvariable=self._to_chan, width=6,
                  font=("Segoe UI", 10)).pack(side=tk.LEFT, padx=2)

        tk.Label(dir_frame, text="  (Откуда — из CSV)", font=("Segoe UI", 9), fg="gray").pack(side=tk.LEFT, padx=(12, 0))

        # ── Авторизация Billing ──
        auth_frame = ttk.LabelFrame(main, text=" Авторизация Billing ", padding=12)
        auth_frame.pack(fill=tk.X, pady=(0, 12))

        tk.Label(auth_frame, text="Логин:", width=18, anchor="e",
                 font=("Segoe UI", 10)).grid(row=0, column=0, padx=(0, 8), pady=4)
        ttk.Entry(auth_frame, textvariable=self._bill_login,
                  font=("Segoe UI", 10)).grid(row=0, column=1, sticky="ew", pady=4)

        tk.Label(auth_frame, text="Пароль:", width=18, anchor="e",
                 font=("Segoe UI", 10)).grid(row=1, column=0, padx=(0, 8), pady=4)
        ttk.Entry(auth_frame, textvariable=self._bill_pass, show="*",
                  font=("Segoe UI", 10)).grid(row=1, column=1, sticky="ew", pady=4)
        auth_frame.columnconfigure(1, weight=1)

        # ── Строка статуса (над кнопкой и прогрессом) ──
        status_frame = ttk.Frame(main)
        status_frame.pack(fill=tk.X, pady=(0, 0))
        self._status_text = tk.StringVar(value="")
        self._status_eta = tk.StringVar(value="")
        self._status_label = tk.Label(
            status_frame, textvariable=self._status_text,
            font=("Segoe UI", 9), fg=C["hdr"], bg=C["bg"],
            anchor="w", height=1
        )
        self._status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._status_eta_label = tk.Label(
            status_frame, textvariable=self._status_eta,
            font=("Segoe UI", 9), fg="#BF8A0F", bg=C["bg"],
            anchor="e", height=1, width=22
        )
        self._status_eta_label.pack(side=tk.RIGHT, padx=(8, 0))

        # ── Кнопка СТАРТ + прогресс ──
        ctrl_frame = ttk.Frame(main)
        ctrl_frame.pack(fill=tk.X, pady=(0, 8))

        self._start_btn = tk.Button(
            ctrl_frame, text="🚀 СТАРТ МИГРАЦИИ",
            font=("Segoe UI", 11, "bold"),
            bg=C["hdr"], fg="white",
            padx=20, pady=6,
            command=self._on_start,
            cursor="hand2"
        )
        self._start_btn.pack(side=tk.LEFT)

        # Progress bar
        self._progress_var = tk.IntVar(value=0)
        self._progress_bar = ttk.Progressbar(
            ctrl_frame, variable=self._progress_var,
            maximum=100, length=300, mode="determinate"
        )
        self._progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(16, 0), pady=4)

        # ── Окно лога ──
        log_frame = ttk.LabelFrame(main, text=" Лог выполнения ", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self._log_text = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD,
            font=("Consolas", 9),
            bg="#1E1E1E", fg="#D4D4D4",
            state=tk.DISABLED,
            height=16,
        )
        self._log_text.pack(fill=tk.BOTH, expand=True)

        # Цветовые теги для лога
        self._log_text.tag_configure("INFO", foreground="#D4D4D4")
        self._log_text.tag_configure("DEBUG", foreground="#6A9955")
        self._log_text.tag_configure("WARN", foreground="#CE9178")
        self._log_text.tag_configure("ERROR", foreground="#F44747")
        self._log_text.tag_configure("FATAL", foreground="#FF0000", font=("Consolas", 9, "bold"))
        self._log_text.tag_configure("SECTION", foreground="#569CD6", font=("Consolas", 9, "bold"))

    # ── Выбор файлов ──

    def _browse_input(self):
        path = filedialog.askopenfilename(
            title="Выберите входной файл",
            filetypes=[("CSV файлы", "*.csv"), ("Excel файлы", "*.xlsx"), ("Все файлы", "*.*")]
        )
        if path:
            self._in_path.set(path)

    def _browse_output(self):
        path = filedialog.askdirectory(title="Папка для сохранения результатов")
        if path:
            self._out_dir.set(path)

    # ── Логирование в GUI ──

    def _on_log(self, level: str, message: str):
        """Callback от логгера — выводит в окно лога."""
        self.root.after(0, lambda: self._append_log(level, message))

    def _append_log(self, level: str, message: str):
        """Добавить строку в лог с цветом."""
        try:
            self._log_text.configure(state=tk.NORMAL)
            ts = datetime.now().strftime("%H:%M:%S")

            if level == "SECTION":
                self._log_text.insert(tk.END, f"\n{'='*60}\n", "SECTION")
                self._log_text.insert(tk.END, f"  {message}\n", "SECTION")
                self._log_text.insert(tk.END, f"{'='*60}\n", "SECTION")
            else:
                tag = level if level in ("INFO", "DEBUG", "WARN", "ERROR", "FATAL") else "INFO"
                icon = {"INFO": "ℹ", "DEBUG": "●", "WARN": "⚠", "ERROR": "✖", "FATAL": "‼"}
                self._log_text.insert(
                    tk.END,
                    f"{ts} {icon.get(level, '•')} {message}\n",
                    tag
                )

            self._log_text.see(tk.END)
            self._log_text.configure(state=tk.DISABLED)
        except Exception:
            pass  # Окно могло быть закрыто

    # ── Автообновление ──

    def _check_updates(self):
        """Проверить обновления на GitHub (в отдельном потоке)."""

        def _do():
            try:
                result = updater.check_for_updates(on_status=self._on_update_status)
                if result is None:
                    self.root.after(0, lambda: messagebox.showinfo(
                        "Обновлений нет",
                        f"У вас актуальная версия {updater.APP_VERSION}."
                    ))
                    return

                # Обновление есть
                answer = self.root.after(0, lambda: messagebox.askyesno(
                    "Обновление доступно",
                    f"Версия {result['tag']} доступна "
                    f"(текущая: {updater.APP_VERSION}).\n\n"
                    f"Что нового:\n{result['body'][:500]}\n\n"
                    "Скачать и установить?"
                ))
                # `answer` будет None из-за after() — используем замыкание
                def _ask():
                    if messagebox.askyesno(
                        "Обновление доступно",
                        f"Версия {result['tag']} доступна "
                        f"(текущая: {updater.APP_VERSION}).\n\n"
                        f"Что нового:\n{result['body'][:500]}\n\n"
                        "Скачать и установить?"
                    ):
                        self._download_and_update(result['download_url'], result['tag'])
                self.root.after(0, _ask)

            except RuntimeError as e:
                self.root.after(0, lambda: messagebox.showerror(
                    "Ошибка проверки", str(e)
                ))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror(
                    "Ошибка", f"Не удалось проверить обновления:\n{e}"
                ))
            finally:
                self.root.after(0, lambda: self._on_update_status(""))

        self._on_update_status("⏳ Проверка обновлений...")
        threading.Thread(target=_do, daemon=True).start()

    def _on_update_status(self, msg: str):
        """Отобразить статус проверки обновления в GUI."""
        self._status_text.set(msg if msg else "Готов к работе")
        if msg:
            self._append_log("INFO", msg)

    def _download_and_update(self, url: str, version: str):
        """Скачать новую версию и применить."""

        def _do():
            try:
                batch_path = updater.download_and_install(
                    url, version,
                    on_progress=lambda s: self.root.after(0, lambda: self._on_update_status(s))
                )

                if batch_path == "DEV_MODE":
                    self.root.after(0, lambda: messagebox.showinfo(
                        "Режим разработчика",
                        "Вы запущены как .py, а не .exe.\n"
                        "Соберите новую версию через build.bat вручную."
                    ))
                    return

                self.root.after(0, lambda: updater.apply_update(
                    batch_path,
                    root_close_cb=self.root.destroy
                ))

            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror(
                    "Ошибка скачивания", f"Не удалось скачать обновление:\n{e}"
                ))
                self.root.after(0, lambda: self._on_update_status(""))

        threading.Thread(target=_do, daemon=True).start()

    # ── Прогресс ──

    def _on_progress(self, current: int, total: int, message: str):
        """Callback прогресса."""
        pct = min(int(current / max(total, 1) * 100), 100)
        self.root.after(0, lambda: self._update_progress(pct, message))

    def _update_progress(self, pct: int, message: str):
        self._progress_var.set(pct)
        if message:
            # Разделяем на основную часть и ETA
            m = re.search(r'(\[осталось[^\]]*\])', message)
            if m:
                main_text = message[:m.start()].strip()
                eta_text = m.group(1)
                # Парсим ETA в секунды: [осталось ~5мин 44с] или [осталось ~30с]
                em = re.search(r'осталось\s*~\s*(?:(\d+)мин\s*)?(\d+)с', eta_text)
                if em:
                    mins = int(em.group(1)) if em.group(1) else 0
                    secs = int(em.group(2))
                    self._eta_seconds = mins * 60 + secs
            else:
                main_text = message
                eta_text = ""
            if len(main_text) > 100:
                main_text = main_text[:97] + "..."
            self._status_text.set(main_text)
            self._status_eta.set(eta_text)
        # Запускаем/перезапускаем секундный таймер
        self._start_eta_timer()

    def _start_eta_timer(self):
        """Запустить/перезапустить плавный обратный отсчёт (1 сек/тик)."""
        if not self._running:
            return
        if self._eta_timer_id:
            try: self.root.after_cancel(self._eta_timer_id)
            except: pass
            self._eta_timer_id = None

        def _tick():
            if not self._running:
                self._eta_timer_id = None
                return
            if self._eta_seconds > 0:
                self._eta_seconds -= 1
                if self._eta_seconds < 60:
                    self._status_eta.set(f"[осталось ~{self._eta_seconds}с]")
                else:
                    m = self._eta_seconds // 60
                    s = self._eta_seconds % 60
                    self._status_eta.set(f"[осталось ~{m}мин {s}с]")
            self._eta_timer_id = self.root.after(1000, _tick)

        self._eta_timer_id = self.root.after(1000, _tick)

    # ── Диалоги из фонового потока ──

    def _confirm_colors(self, colored_rows: list, billing=None, userside=None, olt_ip_from=""):
        # pylint: disable=unused-argument
        """
        Интерактивный диалог просмотра/редактирования цветных строк.
        Вызывается из фонового потока.
        """
        event = threading.Event()
        _dialog_done = False
        _color_of = {i: c for c, i, _ in colored_rows}

        # --- Обработчики закрытия (вынесены из _show для читаемости) ---
        def _on_close_dialog(win):
            nonlocal _dialog_done
            _dialog_done = True
            try: win.destroy()
            except: pass
            event.set()

        def _continue_dialog(win):
            nonlocal _dialog_done
            _dialog_done = True
            win.destroy()
            event.set()

        # --- Построение диалога (на главном потоке через root.after) ---
        def _show():
            nonlocal _dialog_done
            _top = None
            try:
                _top = tk.Toplevel(self.root)
                _top.title("Строки с расхождениями")
                _top.geometry("1500x650")
                _top.update_idletasks()
                _top.geometry(f"+{(_top.winfo_screenwidth()-1500)//2}+{max(0, (_top.winfo_screenheight()-650)//2)}")
                _top.transient(self.root)
                _top.grab_set()
                _top.protocol("WM_DELETE_WINDOW", lambda: _on_close_dialog(_top))

                # ── Подсказка ──
                info_frame = tk.Frame(_top, bg="#FFF8E1")
                info_frame.pack(fill=tk.X, padx=10, pady=(10, 2))
                tk.Label(info_frame,
                         text="🟡 жёлтый — нет адреса  |  🟠 оранж — не найден в биллинге  |  🔴 красный — расхождение",
                         font=("Segoe UI", 9), bg="#FFF8E1", fg="#5D4037").pack(anchor="w", padx=5, pady=3)

                # ── Разделитель: таблица сверху, действия снизу ──
                paned = tk.PanedWindow(_top, orient=tk.VERTICAL, bg="#E9E9E1")
                paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

                # ── Таблица (Canvas + Frame с Label-строками — текст переносится) ──
                table_frame = tk.Frame(paned)
                paned.add(table_frame, height=260)

                _table_canvas = tk.Canvas(table_frame, highlightthickness=0, bg="white")
                _table_vsb = tk.Scrollbar(table_frame, orient="vertical", command=_table_canvas.yview)
                _table_inner = tk.Frame(_table_canvas, bg="white")
                _table_inner.bind("<Configure>",
                    lambda e: _table_canvas.configure(scrollregion=_table_canvas.bbox("all")))
                _table_canvas.create_window((0, 0), window=_table_inner, anchor="nw")
                _table_canvas.configure(yscrollcommand=_table_vsb.set)
                _table_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
                _table_vsb.pack(side=tk.RIGHT, fill=tk.Y)
                # Колесо мыши на canvas
                def _table_on_mw(event):
                    try: _table_canvas.yview_scroll(-1 * (event.delta // 120), "units")
                    except tk.TclError: pass
                _table_canvas.bind("<MouseWheel>", _table_on_mw)
                _table_inner.bind("<MouseWheel>", _table_on_mw)
                # Linux (Button-4 = вверх, Button-5 = вниз)
                _table_canvas.bind("<Button-4>", lambda e: _table_canvas.yview_scroll(-3, "units"))
                _table_canvas.bind("<Button-5>", lambda e: _table_canvas.yview_scroll(3, "units"))
                _table_inner.bind("<Button-4>", lambda e: _table_canvas.yview_scroll(-3, "units"))
                _table_inner.bind("<Button-5>", lambda e: _table_canvas.yview_scroll(3, "units"))

                # Параметры колонок (ширина, wraplength для текстовых)
                _col_w = {
                    "№": 35, "●": 30, "Абонент": 160, "Адрес": 520,
                    "Договор": 105, "VLAN OLT": 85, "VLAN Bill": 85, "Примечание": 470
                }
                _col_wl = {"Адрес": 510, "Примечание": 460}  # wraplength для переноса
                _col_anchor = {"№": "center", "●": "center",
                               "VLAN OLT": "center", "VLAN Bill": "center"}

                # Шапка (фиксированная ширина через pack_propagate(False))
                _hdr_frame = tk.Frame(_table_inner, bg="#E0E0E0")
                _hdr_frame.pack(fill=tk.X, pady=(0, 1))
                for c in ("№", "●", "Абонент", "Адрес", "Договор", "VLAN OLT", "VLAN Bill", "Примечание"):
                    cf = tk.Frame(_hdr_frame, width=_col_w[c], height=22, bg="#E0E0E0")
                    cf.pack(side=tk.LEFT, padx=1)
                    cf.pack_propagate(False)
                    tk.Label(cf, text=c, font=("Segoe UI", 8, "bold"),
                             anchor=_col_anchor.get(c, "w"),
                             bg="#E0E0E0", fg="#333").pack(fill=tk.BOTH, expand=True)

                # Строки данных
                _row_frames = {}  # idx → {frame, labels{col:Label}, color}
                _selected_idx = None

                _COLOR_BG = {"yellow": "#FFF8E1", "orange": "#FFF3E0", "red": "#FFEBEE"}

                def _highlight_row(idx):
                    nonlocal _selected_idx
                    if _selected_idx is not None and _selected_idx in _row_frames:
                        old_color = _row_frames[_selected_idx]["color"]
                        bg_old = _COLOR_BG.get(old_color, "white")
                        _row_frames[_selected_idx]["frame"].configure(bg=bg_old)
                        for cf in _row_frames[_selected_idx]["cells"].values():
                            cf.configure(bg=bg_old)
                        for lbl in _row_frames[_selected_idx]["labels"].values():
                            lbl.configure(bg=bg_old)
                    _selected_idx = idx
                    if idx in _row_frames:
                        bg_new = "#BBDEFB"
                        _row_frames[idx]["frame"].configure(bg=bg_new)
                        for cf in _row_frames[idx]["cells"].values():
                            cf.configure(bg=bg_new)
                        for lbl in _row_frames[idx]["labels"].values():
                            lbl.configure(bg=bg_new)

                _color_tags = {"yellow": "#E6A817", "orange": "#E65100", "red": "#C62828"}

                for color, idx, row in colored_rows:
                    rf = tk.Frame(_table_inner, bg=_COLOR_BG.get(color, "white"))
                    rf.pack(fill=tk.X, pady=0)
                    vals = {
                        "№": str(idx+1),
                        "●": "●",
                        "Абонент": row.get("desc", ""),
                        "Адрес": (row.get("address_us", "") or ""),
                        "Договор": row.get("contract", ""),
                        "VLAN OLT": row.get("old_vlan_olt", ""),
                        "VLAN Bill": row.get("old_vlan_billing", ""),
                        "Примечание": (row.get("note", "") or ""),
                    }
                    labels = {}
                    cells = {}
                    bg_color = _COLOR_BG.get(color, "white")
                    # Пасс 1: создаём все ячейки с pack_propagate(False) для точной ширины
                    for c in ("№", "●", "Абонент", "Адрес", "Договор", "VLAN OLT", "VLAN Bill", "Примечание"):
                        fg = _color_tags.get(color, "#333")
                        cf = tk.Frame(rf, width=_col_w[c], bg=bg_color)
                        cf.pack(side=tk.LEFT, padx=1)
                        cf.pack_propagate(False)
                        lbl = tk.Label(cf, text=vals[c], font=("Segoe UI", 8),
                                       anchor=_col_anchor.get(c, "w"),
                                       bg=bg_color, fg=fg,
                                       wraplength=(_col_wl.get(c, _col_w[c]) - 4),
                                       justify=tk.LEFT)
                        lbl.pack(fill=tk.BOTH, expand=True)
                        labels[c] = lbl
                        cells[c] = cf
                    # Пасс 2: нормализуем высоту строки — все ячейки по самой высокой
                    _max_h = max(lbl.winfo_reqheight() for lbl in labels.values())
                    _max_h = max(_max_h, 22)  # минимум как у заголовка
                    for cf in cells.values():
                        cf.configure(height=_max_h)
                    # Клик по строке + колёсико
                    def _on_row_click(e, i=idx):
                        _highlight_row(i)
                        _on_select_row(i)
                    rf.bind("<Button-1>", _on_row_click)
                    for lbl in labels.values():
                        lbl.bind("<Button-1>", _on_row_click)
                        lbl.bind("<MouseWheel>", _table_on_mw)
                        lbl.bind("<Button-4>", lambda e: _table_canvas.yview_scroll(-3, "units"))
                        lbl.bind("<Button-5>", lambda e: _table_canvas.yview_scroll(3, "units"))
                    for cf in cells.values():
                        cf.bind("<MouseWheel>", _table_on_mw)
                        cf.bind("<Button-4>", lambda e: _table_canvas.yview_scroll(-3, "units"))
                        cf.bind("<Button-5>", lambda e: _table_canvas.yview_scroll(3, "units"))
                    _row_frames[idx] = {"frame": rf, "labels": labels, "cells": cells, "color": color}

                def _update_row_values(idx, vals_dict):
                    """Обновить отображаемые значения строки."""
                    if idx in _row_frames:
                        for c, v in vals_dict.items():
                            if c in _row_frames[idx]["labels"]:
                                _row_frames[idx]["labels"][c].configure(text=str(v) if v else "")

                def _set_row_color(idx, new_color):
                    """Сменить цвет строки (например, resolved → зелёный)."""
                    if idx in _row_frames:
                        _row_frames[idx]["color"] = new_color
                        bg = _COLOR_BG.get(new_color, "white")
                        fg = _color_tags.get(new_color, "#333")
                        _row_frames[idx]["frame"].configure(bg=bg)
                        for cf in _row_frames[idx]["cells"].values():
                            cf.configure(bg=bg)
                        for c, lbl in _row_frames[idx]["labels"].items():
                            lbl.configure(fg=fg)

                # ── Панель действий ──
                action_frame = tk.LabelFrame(paned, text="Действия",
                                             font=("Segoe UI", 9, "bold"), padx=8, pady=6)
                paned.add(action_frame, height=200)
                _action_inner = tk.Frame(action_frame)
                _action_inner.pack(fill=tk.BOTH, expand=True)
                _vlan_anim_job = None  # таймер анимации "Vlan . .. ..."

                def _clear_actions():
                    nonlocal _vlan_anim_job
                    if _vlan_anim_job:
                        try: self.root.after_cancel(_vlan_anim_job)
                        except: pass
                        _vlan_anim_job = None
                    for w in _action_inner.winfo_children():
                        w.destroy()

                def _show_raw_result(title, text):
                    _clear_actions()
                    tk.Label(_action_inner, text=title,
                             font=("Segoe UI", 9, "bold")).pack(anchor="w")
                    text_w = tk.Text(_action_inner, height=7, wrap="word",
                                     font=("Consolas", 8), fg="#333")
                    text_w.insert("1.0", text)
                    text_w.configure(state="disabled")
                    text_w.pack(fill=tk.BOTH, expand=True)

                def _live_retry(idx, row):
                    _clear_actions()
                    loading = tk.Label(_action_inner, text="⏳ Выполняю запрос...",
                                       fg="orange", font=("Segoe UI", 9, "bold"))
                    loading.pack(anchor="w")
                    def _do_query():
                        contract = row.get("contract", "")
                        addr = row.get("address_us", "")
                        result_parts = []
                        found_contract = ""
                        found_addr = ""
                        if contract and billing:
                            raw = billing.search_by_contract(contract)
                            txt = "По договору:\n"
                            if raw:
                                try:
                                    txt += json.dumps(raw[:3], indent=2, ensure_ascii=False, default=str)[:800]
                                except Exception:
                                    txt += str(raw)[:800]
                                vb = raw[0]
                                ba = billing._get_billing_address(vb) or ""
                                found_addr = ba
                                found_contract = vb.get("agrm_num","") or contract
                            else:
                                txt += "(пусто — не найден)"
                            result_parts.append(txt)
                        if addr and billing and not found_addr:
                            raw = billing.search_by_address(addr)
                            txt = "\n\nПо адресу:\n"
                            if raw:
                                try:
                                    txt += json.dumps(raw[:3], indent=2, ensure_ascii=False, default=str)[:800]
                                except Exception:
                                    txt += str(raw)[:800]
                                for rb in raw[:10]:
                                    a2 = str(rb.get("address_2","") or "").lower()
                                    if "сальск" not in a2: continue
                                    ags = rb.get("agreements",[])
                                    an = str(ags[0].get("agrm_num","")) if ags else ""
                                    if an and re.match(r'^\d{8}\D',an): continue
                                    found_addr = a2
                                    found_contract = an
                                    break
                            else:
                                txt += "(пусто)"
                            result_parts.append(txt)
                        result_text = "".join(result_parts) if result_parts else "(нет данных)"
                        if _dialog_done: return
                        if found_addr:
                            row["address_us"] = row.get("address_us","") or found_addr
                        if found_contract:
                            row["contract"] = found_contract
                        def _update():
                            _update_row_values(idx, {
                                "№": str(idx+1),
                                "●": "🟠" if _color_of.get(idx) else "●",
                                "Абонент": row.get("desc",""),
                                "Адрес": row.get("address_us",""),
                                "Договор": row.get("contract",""),
                                "VLAN OLT": row.get("old_vlan_olt",""),
                                "VLAN Bill": row.get("old_vlan_billing",""),
                                "Примечание": row.get("note",""),
                            })
                            _show_raw_result("📋 Результат перезапроса:", result_text)
                        self.root.after(0, _update)
                    threading.Thread(target=_do_query, daemon=True).start()

                def _live_retry_us(idx, row):
                    _clear_actions()
                    loading = tk.Label(_action_inner, text="⏳ Выполняю запрос US...",
                                       fg="orange", font=("Segoe UI", 9, "bold"))
                    loading.pack(anchor="w")
                    def _do_query():
                        mac = row.get("mac", "")
                        result_text = "(нет MAC)"
                        if mac and userside:
                            if userside.authorize():
                                cid = userside.search_by_mac(mac)
                                if cid:
                                    data = userside.get_subscriber_data(cid)
                                    if data:
                                        if _dialog_done: return
                                        row["address_us"] = data.get("address", row.get("address_us",""))
                                        row["contract"] = data.get("contract", row.get("contract",""))
                                        row["phone"] = data.get("phone", row.get("phone",""))
                                        row["_raw_us"] = data
                                        result_text = f"Адрес: {row['address_us']}\nДоговор: {row['contract']}\nТелефон: {row['phone']}"
                                else:
                                    result_text = "Не найден в US"
                        def _update():
                            _set_row_color(idx, "resolved")
                            _update_row_values(idx, {
                                "№": str(idx+1), "●": "✅",
                                "Абонент": row.get("desc",""),
                                "Адрес": row.get("address_us",""),
                                "Договор": row.get("contract",""),
                                "VLAN OLT": row.get("old_vlan_olt",""),
                                "VLAN Bill": row.get("old_vlan_billing",""),
                                "Примечание": "",
                            })
                            _show_raw_result("📋 Результат перезапроса US:", result_text)
                        self.root.after(0, _update)
                    threading.Thread(target=_do_query, daemon=True).start()

                def _search_contract_by_number(idx, row):
                    _clear_actions()
                    tk.Label(_action_inner, text="Введите номер договора для поиска:",
                             font=("Segoe UI", 9, "bold")).pack(anchor="w")
                    search_frame = tk.Frame(_action_inner)
                    search_frame.pack(fill=tk.X, pady=4)
                    contract_var = tk.StringVar()
                    tk.Entry(search_frame, textvariable=contract_var, width=25,
                             font=("Segoe UI", 10)).pack(side=tk.LEFT, padx=(0, 6))
                    result_area = tk.Frame(_action_inner)
                    result_area.pack(fill=tk.BOTH, expand=True)

                    def _do_search():
                        contract = contract_var.get().strip()
                        if not contract: return
                        for w in result_area.winfo_children(): w.destroy()
                        tk.Label(result_area, text="⏳ Ищу...",
                                 fg="orange").pack(anchor="w")
                        def _query():
                            raw = billing.search_by_contract(contract) if billing else None
                            if _dialog_done: return
                            def _show_result():
                                for w in result_area.winfo_children(): w.destroy()
                                if not raw:
                                    tk.Label(result_area, text="❌ Договор не найден",
                                             fg="red", font=("Segoe UI", 9)).pack(anchor="w")
                                    return
                                for cand in raw[:5]:
                                    agrm = cand.get("agrm_num","")
                                    fio = cand.get("user_name","") or cand.get("login","")
                                    blocked = cand.get("blocked","0")
                                    addr = str(cand.get("address_2","") or "")[:60]
                                    status = "🔴 расторгнут" if str(blocked) != "0" else "🟢 активен"
                                    card = tk.Frame(result_area, relief="groove", bd=1, padx=4, pady=2)
                                    card.pack(fill=tk.X, pady=2)
                                    info = f"{agrm} | {fio} | {status}"
                                    if addr: info += f"\n   {addr}"
                                    tk.Label(card, text=info, font=("Segoe UI", 8),
                                             anchor="w", justify=tk.LEFT).pack(side=tk.LEFT, fill=tk.X, expand=True)
                                    if str(blocked) == "0":
                                        def _apply(dog=agrm, f=fio, a=addr):
                                            row["contract"] = dog
                                            row["address_us"] = row.get("address_us","") or a
                                            row["note"] = ""
                                            _set_row_color(idx, "resolved")
                                            _update_row_values(idx, {
                                                "№": str(idx+1), "●": "✅",
                                                "Абонент": row.get("desc",""),
                                                "Адрес": row.get("address_us","")[:50],
                                                "Договор": dog,
                                                "VLAN OLT": row.get("old_vlan_olt",""),
                                                "VLAN Bill": row.get("old_vlan_billing",""),
                                                "Примечание": "",
                                            })
                                            _clear_actions()
                                            tk.Label(_action_inner, text=f"✅ Взят договор {dog} ({f})",
                                                     fg="green", font=("Segoe UI", 9, "bold")).pack(anchor="w")
                                        tk.Button(card, text="✅ Взять", font=("Segoe UI", 8),
                                                  command=_apply, bg="#4CAF50", fg="white",
                                                  width=8).pack(side=tk.RIGHT)
                            self.root.after(0, _show_result)
                        threading.Thread(target=_query, daemon=True).start()
                    tk.Button(search_frame, text="🔍 Найти",
                              command=_do_search,
                              bg="#FF9800", fg="white", font=("Segoe UI", 9)).pack(side=tk.LEFT)

                def _show_stored(title, data):
                    txt = str(data) if data else "(нет сохранённых данных)"
                    _show_raw_result(title, txt)

                def _manual_edit(idx, row):
                    _clear_actions()
                    _header_frame = tk.Frame(_action_inner)
                    _header_frame.pack(fill=tk.X)
                    tk.Label(_header_frame, text="Ручное редактирование:",
                             font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)
                    _mac_entry = tk.Entry(_header_frame, width=22,
                                          font=("Consolas", 8), relief="flat",
                                          fg="#555", bg="#E9E9E1")
                    _mac_entry.insert(0, row.get("mac", ""))
                    _mac_entry.configure(state="readonly")
                    _mac_entry.pack(side=tk.RIGHT, padx=(8, 0))
                    edit_frame = tk.Frame(_action_inner)
                    edit_frame.pack(fill=tk.X, pady=4)
                    fields = [
                        ("address_us", "Адрес:", 40),
                        ("contract", "Договор:", 20),
                        ("phone", "Телефон:", 20),
                        ("old_vlan_olt", "VLAN OLT:", 10),
                        ("old_vlan_billing", "VLAN биллинг:", 10),

                    ]
                    entries = {}
                    for key, label, width in fields:
                        fr = tk.Frame(edit_frame)
                        fr.pack(fill=tk.X, pady=1)
                        tk.Label(fr, text=label, width=14, anchor="e",
                                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 4))
                        var = tk.StringVar(value=row.get(key, ""))
                        entries[key] = var
                        tk.Entry(fr, textvariable=var, width=width,
                                 font=("Segoe UI", 8)).pack(side=tk.LEFT, fill=tk.X, expand=True)
                        if key == "contract" and billing:
                            tk.Button(fr, text="🔍 Найти", font=("Segoe UI", 7),
                                      bg="#FF9800", fg="white",
                                      command=lambda c=var: threading.Thread(
                                          target=_search_and_fill_by_contract, args=(c,), daemon=True
                                      ).start()
                                      ).pack(side=tk.RIGHT, padx=(4, 0))
                    def _search_and_fill_by_contract(contract_var):
                        """Поиск в биллинге по договору и заполнение полей."""
                        contract_num = contract_var.get().strip()
                        if not contract_num:
                            return
                        try:
                            raw = billing.search_by_contract(contract_num)
                            if not raw:
                                self.root.after(0, lambda: tk.Label(
                                    _action_inner, text="❌ Договор не найден",
                                    fg="red", font=("Segoe UI", 8)
                                ).pack(anchor="w"))
                                return
                            vb = raw[0]
                            # Адрес
                            ba = billing._get_billing_address(vb) or ""
                            # VLAN биллинг
                            vlan_b = ""
                            vg_id = vb.get("vg_id") or vb.get("id")
                            if vg_id:
                                vlan_raw = billing.get_vlan(str(vg_id))
                                if vlan_raw:
                                    vlan_b = str(vlan_raw).split(":")[-1]
                            # Телефон — из поля phone/mobile строки vgroup
                            phone = str(vb.get("phone", "") or vb.get("mobile", "") or "").strip()
                            # Маска /32 = PPPoE
                            is_pppoe_mask = '/32' in vb.get("ip", "")
                            # Обновляем поля в главном потоке
                            def _fill():
                                entries["address_us"].set(ba)
                                row["address_us"] = ba
                                if not vlan_b:
                                    vlan_b = "5"
                                entries["old_vlan_billing"].set(vlan_b)
                                row["old_vlan_billing"] = vlan_b
                                entries["phone"].set(phone)
                                row["phone"] = phone
                                entries["contract"].set(vb.get("agrm_num", contract_num))
                                row["contract"] = vb.get("agrm_num", contract_num)
                                if is_pppoe_mask:
                                    row["_billing_ip_mask_32"] = True
                                # Обновить таблицу
                                _update_row_values(idx, {
                                    "№": str(idx+1),
                                    "●": "🟡" if _color_of.get(idx) else "●",
                                    "Абонент": row.get("desc",""),
                                    "Адрес": row.get("address_us",""),
                                    "Договор": row.get("contract",""),
                                    "VLAN OLT": row.get("old_vlan_olt",""),
                                    "VLAN Bill": row.get("old_vlan_billing",""),
                                    "Примечание": row.get("note",""),
                                })
                                tk.Label(_action_inner, text="✅ Найдено и заполнено",
                                         fg="green", font=("Segoe UI", 8)).pack(anchor="w")
                            self.root.after(0, _fill)
                        except Exception as exc:
                            self.root.after(0, lambda: tk.Label(
                                _action_inner, text=f"❌ Ошибка: {exc}",
                                fg="red", font=("Segoe UI", 8)
                            ).pack(anchor="w"))

                    def _save():
                        # Проверка обязательных полей
                        missing = []
                        for key, label, _ in fields:
                            if key == "phone":
                                continue  # телефон необязателен
                            if not entries[key].get().strip():
                                missing.append(label.rstrip(":"))
                        if missing:
                            tk.Label(_action_inner,
                                     text=f"⚠️ Не заполнены: {', '.join(missing)}",
                                     fg="red", font=("Segoe UI", 8, "bold")
                            ).pack(anchor="w")
                            return
                        for key, var in entries.items():
                            row[key] = var.get().strip()
                        # Очистить примечание и снять цвет
                        row["note"] = ""
                        _set_row_color(idx, "resolved")
                        _update_row_values(idx, {
                            "№": str(idx+1),
                            "●": "●",
                            "Абонент": row.get("desc",""),
                            "Адрес": row.get("address_us",""),
                            "Договор": row.get("contract",""),
                            "VLAN OLT": row.get("old_vlan_olt",""),
                            "VLAN Bill": row.get("old_vlan_billing",""),
                            
                            "Примечание": "",
                        })
                        tk.Label(_action_inner, text="✅ Сохранено, строка принята",
                                 fg="green", font=("Segoe UI", 8, "bold")
                        ).pack(anchor="w")
                    tk.Button(edit_frame, text="💾 Сохранить",
                              command=_save, bg="#1E376E", fg="white",
                              font=("Segoe UI", 9), padx=10).pack(pady=4)

                def _show_rejected_with_scroll(idx, row):
                    _clear_actions()
                    tk.Label(_action_inner, text="Другие адреса (отбракованные):",
                             font=("Segoe UI", 9, "bold")).pack(anchor="w")
                    canv_frame = tk.Frame(_action_inner)
                    canv_frame.pack(fill=tk.BOTH, expand=True)
                    canvas = tk.Canvas(canv_frame, highlightthickness=0)
                    vsb = tk.Scrollbar(canv_frame, orient="vertical", command=canvas.yview)
                    scroll_frame = tk.Frame(canvas)
                    scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
                    canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
                    canvas.configure(yscrollcommand=vsb.set)
                    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
                    vsb.pack(side=tk.RIGHT, fill=tk.Y)
                    def _on_mousewheel(event):
                        try: canvas.yview_scroll(-1 * (event.delta // 120), "units")
                        except tk.TclError: pass
                    canvas.bind("<MouseWheel>", _on_mousewheel)

                    def _shorten_addr(long_addr):
                        import re as _re
                        a = long_addr
                        a = _re.sub(r'^Россия[^,]*,\s*обл[^,]*,\s*р-н[^,]*,\s*', '', a)
                        a = _re.sub(r'^г[. ]\s*', '', a)
                        a = _re.sub(r'^с[. ]\s*', '', a)
                        a = _re.sub(r',\s*,', ',', a)
                        a = _re.sub(r'\s*ул\s+', '', a)
                        a = _re.sub(r'\s*пер\s+', '', a)
                        a = _re.sub(r'\s*площадь\s+', '', a)
                        a = _re.sub(r'\s*дом\s+', '', a)
                        a = a.strip(' ,').rstrip(',')
                        a = re.sub(r',(?!\s)', ', ', a)
                        a = a.strip() + ','
                        return a

                    def _get_contract(cand: dict) -> str:
                        """Извлечь номер договора из кандидата (разные форматы API)."""
                        # Прямое поле
                        for key in ("agrm_num", "num", "number", "contract", "dog"):
                            v = cand.get(key, "")
                            if v:
                                return str(v).strip()
                        # Вложенные agreements
                        ags = cand.get("agreements", [])
                        if ags:
                            v = ags[0].get("agrm_num", "")
                            if v:
                                return str(v).strip()
                        return ""

                    rejected = row.get("_raw_addr_rejected", [])
                    _fetch_vlan = len(rejected) < 20 and billing is not None
                    _vlan_label_map = {}  # contract_num -> label_widget
                    shown = 0
                    for rj in rejected:
                        a2 = str(rj.get("address_2", "") or rj.get("address","") or "?")
                        a2_short = _shorten_addr(a2)
                        ag = _get_contract(rj)
                        fio = str(rj.get("user_name", "") or rj.get("name", "") or "")
                        if "сальск" not in a2.lower() and "сальский" not in a2.lower():
                            continue
                        shown += 1
                        card = tk.Frame(scroll_frame, relief="groove", bd=1, padx=4, pady=1)
                        card.pack(fill=tk.X, pady=1)
                        info_frame2 = tk.Frame(card)
                        info_frame2.pack(side=tk.LEFT, fill=tk.X, expand=True)
                        tk.Label(info_frame2, text=a2, font=("Segoe UI", 8, "bold"),
                                 anchor="w", justify=tk.LEFT).pack(fill=tk.X)
                        # Формат: д.{договор} | {ФИО} | Vlan {vlan}
                        wants_vlan = _fetch_vlan and bool(ag)
                        if wants_vlan:
                            txt = f"д.{ag} | {fio} | Vlan ..."
                        else:
                            txt = f"д.{ag} | {fio}"
                        sub_label = tk.Label(info_frame2, text=txt,
                                             font=("Segoe UI", 7), fg="#555",
                                             anchor="w")
                        sub_label.pack(fill=tk.X)
                        if wants_vlan:
                            _vlan_label_map[ag] = sub_label
                            # После загрузки VLAN обновим: добавим " | Vlan {num}" в конец

                        def _pick(addr_short=a2_short, dog=ag):
                            row["address_us"] = addr_short
                            row["contract"] = dog or row.get("contract","")
                            row["note"] = ""
                            # Сразу обновляем договор и адрес, VLAN загрузим асинхронно
                            _set_row_color(idx, "resolved")
                            _update_row_values(idx, {
                                "№": str(idx+1), "●": "✅",
                                "Абонент": row.get("desc",""),
                                "Адрес": addr_short,
                                "Договор": dog or row.get("contract",""),
                                "VLAN OLT": row.get("old_vlan_olt",""),
                                "VLAN Bill": row.get("old_vlan_billing","") or "загрузка...",
                                "Примечание": "",
                            })
                            # VLAN загружаем в фоне
                            if dog and billing:
                                def _load_vlan():
                                    try:
                                        vd = billing.search_by_contract(dog)
                                        vlan_str = row.get("old_vlan_billing","")
                                        if vd:
                                            vg = vd[0].get("vg_id")
                                            if vg:
                                                r = billing.get_vlan(vg)
                                                if r:
                                                    vlan_str = str(r).split(":")[-1]
                                        if vlan_str:
                                            row["old_vlan_billing"] = vlan_str
                                            self.root.after(0, lambda: _update_row_values(idx, {
                                                "VLAN Bill": vlan_str,
                                            }))
                                    except Exception:
                                        pass
                                threading.Thread(target=_load_vlan, daemon=True).start()
                            _clear_actions()
                            tk.Label(_action_inner, text=f"✅ Взято: {addr_short}",
                                     fg="green", font=("Segoe UI", 9)).pack(anchor="w")
                        tk.Button(card, text="✅", font=("Segoe UI", 8),
                                  command=_pick, bg="#4CAF50", fg="white",
                                  width=4).pack(side=tk.RIGHT)

                    # Параллельная загрузка VLAN для всех кандидатов (в фоновом потоке)
                    if _fetch_vlan and _vlan_label_map:
                        def _load_all_vlans_parallel():
                            import concurrent.futures as _cf
                            def _one(cn, lb):
                                try:
                                    vd = billing.search_by_contract(cn)
                                    vs = "?"
                                    if vd:
                                        vg = vd[0].get("vg_id")
                                        if vg:
                                            r = billing.get_vlan(vg)
                                            if r: vs = str(r).split(":")[-1]
                                except Exception:
                                    vs = "?"
                                def _vlan_upd(lbl=lb, val=vs):
                                    try:
                                        txt = lbl.cget("text")
                                        if "Vlan" in txt:
                                            lbl.configure(text=txt.rsplit("Vlan", 1)[0] + f"Vlan {val}")
                                    except tk.TclError:
                                        pass  # виджет уже уничтожен (окно закрыто)
                                self.root.after(0, _vlan_upd)
                            with _cf.ThreadPoolExecutor(max_workers=5) as ex:
                                for cn, lb in list(_vlan_label_map.items()):
                                    ex.submit(_one, cn, lb)
                        threading.Thread(target=_load_all_vlans_parallel, daemon=True).start()

                        # Анимация "Vlan . .. ..." пока грузится
                        nonlocal _vlan_anim_job
                        def _vlan_anim_tick():
                            nonlocal _vlan_anim_job
                            if not _vlan_label_map:
                                return
                            any_dots = False
                            for lbl in list(_vlan_label_map.values()):
                                try:
                                    txt = lbl.cget("text")
                                except tk.TclError:
                                    continue
                                if "Vlan ." in txt:
                                    any_dots = True
                                    if "Vlan ..." in txt:
                                        lbl.configure(text=txt.replace("Vlan ...", "Vlan ."))
                                    elif "Vlan .." in txt:
                                        lbl.configure(text=txt.replace("Vlan ..", "Vlan ..."))
                                    elif "Vlan ." in txt:
                                        lbl.configure(text=txt.replace("Vlan .", "Vlan .."))
                            if any_dots:
                                _vlan_anim_job = self.root.after(500, _vlan_anim_tick)
                            else:
                                _vlan_anim_job = None
                        if _vlan_label_map:
                            _vlan_anim_tick()

                    if not shown:
                        tk.Label(scroll_frame, text="(нет подходящих)", fg="gray",
                                 font=("Segoe UI", 8)).pack(anchor="w")

                # ── Выбор строки ──
                def _on_select_row(idx):
                    """Вызвать action-панель для строки idx."""
                    row = None
                    color = None
                    for c, i, r in colored_rows:
                        if i == idx:
                            row = r
                            color = c
                            break
                    if row is None:
                        _clear_actions()
                        return
                    _clear_actions()
                    status_text = {"yellow": "🟡 Нет адреса в US",
                                   "orange": "🟠 Не найден в биллинге",
                                   "red": "🔴 Расхождение"}.get(color, "")
                    tk.Label(_action_inner, text=f"Строка {idx+1}: {row.get('desc','')}  —  {status_text}",
                             font=("Segoe UI", 9, "bold"), anchor="w").pack(fill=tk.X)
                    btn_row = tk.Frame(_action_inner)
                    btn_row.pack(fill=tk.X, pady=4)
                    _rej = row.get("_raw_addr_rejected", [])
                    if _rej and len(_rej) > 1:
                        tk.Button(btn_row, text=f"🔍 {len(_rej)} кандидатов",
                                  command=lambda r=row, i=idx: _show_rejected_with_scroll(i, r),
                                  bg="#9C27B0", fg="white", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=2)
                    if color == "yellow":
                        tk.Button(btn_row, text="🔄 Перезапросить US",
                                  command=lambda i=idx, r=row: _live_retry_us(i, r),
                                  bg="#FF9800", fg="white", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=2)
                    tk.Button(btn_row, text="✏️ Ввести вручную",
                              command=lambda i=idx, r=row: _manual_edit(i, r),
                              bg="#1976D2", fg="white", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=2)

                # ── Кнопка "Продолжить" ──
                bottom_frame = tk.Frame(_top)
                bottom_frame.pack(fill=tk.X, padx=10, pady=(5, 10))
                tk.Button(bottom_frame, text="✅ Продолжить",
                          font=("Segoe UI", 11, "bold"),
                          command=lambda: _continue_dialog(_top),
                          bg="#1E376E", fg="white", padx=30).pack(side=tk.LEFT)

            except Exception as _exc:
                import traceback as _tb
                self.log.error(f"Ошибка при создании диалога цветных строк: {_exc}")
                self.log.debug(_tb.format_exc())
                if _top:
                    try: _top.destroy()
                    except: pass
                event.set()

        # Запуск диалога на главном потоке, ожидание на фоновом
        self.root.after(0, _show)
        event.wait()

    def _ask_sn(self, desc: str, default: str, done: int = 1, total: int = 1) -> str:
        """Спросить SN терминала. Автоподтверждение при вводе HWTC..."""
        if self._skip_all_sn:
            return "HWTC12345678"
        import threading as _thr, re as _re
        event = _thr.Event()
        result = [default]

        def _show():
            top = tk.Toplevel(self.root)
            top.title(f"Ввод SN  [{done}/{total}]")
            top.update_idletasks()
            w, h = 480, 170
            sw = top.winfo_screenwidth()
            sh = top.winfo_screenheight()
            top.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
            top.transient(self.root)
            top.grab_set()
            top.protocol("WM_DELETE_WINDOW", lambda: (top.destroy(), event.set()))

            tk.Label(top, text=f"[{done}/{total}] Отсканируйте терминал:",
                     font=("Segoe UI", 9), fg="gray").pack(pady=(8, 0))
            tk.Label(top, text=desc, font=("Segoe UI", 11, "bold")).pack()

            sn_var = tk.StringVar()
            entry = ttk.Entry(top, textvariable=sn_var, font=("Consolas", 14), width=30)
            entry.pack(pady=8)
            entry.focus_set()

            def _on_change(*_):
                val = sn_var.get().strip()
                if len(val) >= 10:
                    result[0] = val
                    top.destroy()
                    event.set()

            sn_var.trace_add("write", _on_change)

            # Кнопка пропуска всех SN
            btn_frame = tk.Frame(top)
            btn_frame.pack(pady=(0, 6))
            tk.Button(
                btn_frame,
                text="⏭ Пропустить (все SN)",
                font=("Segoe UI", 9),
                bg="#FF9800", fg="white",
                command=lambda: (
                    setattr(self, "_skip_all_sn", True),
                    setattr(self.orchestrator, "_skip_label_print", True),
                    result.__setitem__(0, "HWTC12345678"),
                    top.destroy(),
                    event.set()
                )
            ).pack()

        self.root.after(0, _show)
        event.wait()
        return result[0]

    # ── Старт миграции ──

    def _on_start(self):
        if self._running:
            messagebox.showwarning("Уже выполняется", "Миграция уже запущена")
            return

        # Валидация полей
        in_path = self._in_path.get().strip()
        if not in_path or not os.path.exists(in_path):
            messagebox.showerror("Ошибка", "Укажите существующий входной файл")
            return

        out_dir = self._out_dir.get().strip()
        if not out_dir:
            out_dir = os.path.join(os.path.dirname(in_path), "result_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
            self._out_dir.set(out_dir)

        # Откуда — читаем из самого CSV
        from_olt, from_chan = self._read_csv_olt_chan(in_path)
        if not from_olt or not from_chan:
            messagebox.showerror("Ошибка",
                "Не удалось прочитать OLT/ствол из CSV.\n"
                "Убедитесь, что файл содержит колонки OLT и Chan.")
            return

        to_olt = self._to_olt.get().strip()
        to_chan = self._to_chan.get().strip()
        bill_login = self._bill_login.get().strip()
        bill_pass = self._bill_pass.get().strip()

        # Создаём логгер
        log_dir = os.path.join(os.path.dirname(__file__), "logs")
        os.makedirs(log_dir, exist_ok=True)
        session = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.log = Logger(log_dir, session)
        self.log.on_log(self._on_log)
        self.log.section("ЗАПУСК ПРИЛОЖЕНИЯ")
        self.log.info(f"Входной файл: {in_path}")
        self.log.info(f"Папка вывода: {out_dir}")

        # Блокируем кнопку
        self._running = True
        self._start_btn.configure(state=tk.DISABLED, text="⏳ Выполняется...")
        self._log_text.delete(1.0, tk.END)

        # Запускаем в фоновом потоке
        thread = threading.Thread(
            target=self._run_migration,
            args=(in_path, out_dir, from_olt, from_chan, to_olt, to_chan, bill_login, bill_pass),
            daemon=True
        )
        thread.start()

    def _run_migration(self, in_path, out_dir, from_olt, from_chan, to_olt, to_chan, bill_login, bill_pass):
        """Запуск миграции в фоновом потоке."""
        try:
            self.orchestrator = MigrationOrchestrator(
                log=self.log,
                on_progress=self._on_progress,
                on_confirm_colors=self._confirm_colors,
                on_ask_sn=self._ask_sn,
            )
            success = self.orchestrator.start(
                input_path=in_path,
                output_dir=out_dir,
                from_olt=from_olt, from_chan=from_chan,
                to_olt_name=to_olt, to_chan=to_chan,
                billing_login=bill_login, billing_password=bill_pass,
            )

            if success:
                self.root.after(0, lambda: self._on_finish(
                    f"✅ Миграция завершена!\nРезультаты: {out_dir}"))
            else:
                self.root.after(0, lambda: self._on_finish(
                    "❌ Миграция прервана из-за ошибки.\n"
                    "Подробности в логе.", is_error=True))

        except Exception as e:
            err_msg = str(e)
            self.log.fatal(f"Критическая ошибка: {err_msg}")
            import traceback
            self.log.debug(traceback.format_exc())
            self.root.after(0, lambda m=err_msg: self._on_finish(
                f"❌ Критическая ошибка:\n{m}", is_error=True))

    def _on_finish(self, message: str, is_error: bool = False):
        """По окончании миграции."""
        self._running = False
        if self._eta_timer_id:
            try: self.root.after_cancel(self._eta_timer_id)
            except: pass
            self._eta_timer_id = None
        self._start_btn.configure(state=tk.NORMAL, text="🚀 СТАРТ МИГРАЦИИ")
        if not is_error:
            self._progress_var.set(100)
        messagebox.showinfo(
            "Результат" if not is_error else "Ошибка",
            message
        )

    # ── Завершение ──

    def run(self):
        """Запустить GUI."""
        base = os.path.dirname(os.path.abspath(__file__))
        examples_dir = os.path.join(base, "examples")
        if os.path.exists(examples_dir):
            for f in os.listdir(examples_dir):
                if f.endswith(".csv") or f.endswith(".xlsx"):
                    self._in_path.set(os.path.join(examples_dir, f))
                    break

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        if self._running:
            if not messagebox.askyesno("Подтверждение", "Миграция ещё выполняется. Закрыть?"):
                return
        if self.log:
            self.log.close()
        self.root.destroy()


# ── Точка входа ──

def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = MigrationApp()
    app.run()


if __name__ == "__main__":
    main()
