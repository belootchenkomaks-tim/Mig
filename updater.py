#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
updater.py — Автообновление через GitHub Releases.

Механизм:
  1. Читает APP_VERSION из version.txt (упакован в EXE или рядом)
  2. GET /repos/{GITHUB_REPO}/releases/latest — JSON с tag_name + assets[]
  3. Сравнивает версии через _is_newer_version()
  4. Если новее — диалог "Скачать?"
  5. Скачивает .exe из assets во временную папку
  6. Создаёт batch-скрипт: ждёт 3 сек → копирует новый exe → запускает его
  7. Закрывает текущее приложение
"""

import os
import sys
import json
import ssl
import re
import tempfile
import subprocess
import urllib.request
import urllib.error
from typing import Optional

# ── РЕПОЗИТОРИЙ ──
# Заменить на свой после создания репозитория:
# https://github.com/ВАШ_АККАУНТ/ВАШ_РЕПО
GITHUB_REPO = "belootchenkomaks-tim/Mig"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def _detect_paths():
    """
    Определить base_dir (ресурсы внутри EXE) и exe_dir (куда сохранять обновления).
    При запуске как .py — оба равны папке скрипта.
    """
    if getattr(sys, 'frozen', False):
        base_dir = sys._MEIPASS        # папка с ресурсами внутри EXE
        exe_dir = os.path.dirname(sys.executable)  # папка, где лежит .exe
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        exe_dir = base_dir
    return base_dir, exe_dir


_BASE_DIR, _EXE_DIR = _detect_paths()


def read_version() -> str:
    """
    Прочитать версию из version.txt.
    Ищет в base_dir (ресурсы EXE) и exe_dir (рядом с EXE).
    Возвращает "1.0" если файл не найден.
    """
    for d in (_BASE_DIR, _EXE_DIR):
        p = os.path.join(d, "version.txt")
        try:
            with open(p, "r", encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
        except Exception:
            pass
    return "1.0"


APP_VERSION = read_version()


def _is_newer_version(latest: str, current: str) -> bool:
    """
    Сравнить две версии вида 'X.Y' или 'X.Y.Z'.
    >>> _is_newer_version('2.6', '2.51')   # 2.6 > 2.51 → True
    >>> _is_newer_version('1.9', '2.0')    # 1.9 < 2.0 → False
    Спец-логика: X.YY где YY > 30 — это X.Y.Z (2.51 = 2.5.1)
    """
    try:
        def _parse(v: str):
            parts = [int(x) for x in v.split(".")]
            # 2.51 → [2, 5, 1]
            if len(parts) == 2 and parts[1] > 30:
                return [parts[0], parts[1] // 10, parts[1] % 10]
            return parts

        lh = _parse(latest)
        ch = _parse(current)

        max_len = max(len(lh), len(ch))
        lh += [0] * (max_len - len(lh))
        ch += [0] * (max_len - len(ch))
        return lh > ch
    except (ValueError, IndexError):
        return False


def check_for_updates(on_status=None) -> Optional[dict]:
    """
    Проверить GitHub Releases на наличие новой версии.

    on_status — callback(status_string) для отображения в GUI
    Возвращает dict {tag, download_url, body} если обновление есть, иначе None.
    """
    if on_status:
        on_status("⏳ Проверка обновлений...")

    try:
        req = urllib.request.Request(
            GITHUB_API_URL,
            headers={"User-Agent": f"migration-olt/{APP_VERSION}"},
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            data = json.loads(resp.read().decode())

        latest_tag = data.get("tag_name", "").lstrip("v")

        # Ищем .exe в assets
        download_url = None
        assets = data.get("assets", [])
        for asset in assets:
            name = asset.get("name", "")
            if name.endswith(".exe"):
                download_url = asset.get("browser_download_url")
                break
        if not download_url and assets:
            download_url = assets[0].get("browser_download_url")

        body = data.get("body", "")

        if _is_newer_version(latest_tag, APP_VERSION):
            return {
                "tag": latest_tag,
                "download_url": download_url,
                "body": body[:1000],
                "html_url": data.get("html_url", ""),
            }

        return None  # актуальная версия

    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(
                f"Релиз не найден на GitHub.\n"
                f"Проверьте GITHUB_REPO (сейчас: {GITHUB_REPO}).\n"
                f"Создайте первый релиз вручную."
            )
        raise RuntimeError(f"HTTP {e.code}: {e.reason}")
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"Нет доступа к GitHub:\n{e}")


def download_and_install(url: str, new_version: str,
                         on_progress=None) -> str:
    """
    Скачать новый .exe и подготовить замену через batch-скрипт.

    on_progress — callback(percent_or_status)
    Возвращает путь к batch-скрипту (уже создан).
    """
    if on_progress:
        on_progress("⏳ Скачивание...")

    # ── Скачиваем во временную папку ──
    temp_dir = tempfile.gettempdir()
    new_exe_temp = os.path.join(temp_dir, f"Migration_v{new_version}.exe")

    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "migration-olt-updater"})
    with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        with open(new_exe_temp, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if total and on_progress:
                    pct = downloaded * 100 // total
                    on_progress(f"⏳ Скачивание... {pct}%")

    if not getattr(sys, 'frozen', False):
        # Режим разработчика — не перезаписываем
        return "DEV_MODE"

    # ── Определяем имя и путь нового exe ──
    current_exe = sys.executable
    exe_dir_path = os.path.dirname(current_exe)
    new_exe_name = f"Миграция_OLT_v{new_version}.exe"
    new_exe_final = os.path.join(exe_dir_path, new_exe_name)

    # ── Создаём batch-скрипт подмены ──
    # Ждёт 3 сек (пока закроется текущее приложение),
    # копирует новый exe, запускает его и самоудаляется.
    batch_path = os.path.join(temp_dir, "migration_update.bat")
    bat_content = f"""@echo off
chcp 65001 >nul 2>&1
echo Ожидание завершения программы...
timeout /t 3 /nobreak >nul
copy /y "{new_exe_temp}" "{new_exe_final}" >nul
if errorlevel 1 (
    echo ОШИБКА: не удалось скопировать файл.
    echo Возможно, нужны права администратора.
    pause
    exit /b 1
)
echo Запуск новой версии...
start "" "{new_exe_final}"
del "%~f0"
"""
    with open(batch_path, "w", encoding="utf-8") as f:
        f.write(bat_content)

    if on_progress:
        on_progress("✅ Скачано. Готово к перезапуску.")

    return batch_path


def apply_update(batch_path: str, root_close_cb=None):
    """
    Запустить batch-скрипт обновления и закрыть приложение.

    batch_path — путь к .bat (из download_and_install)
    root_close_cb — функция закрытия GUI (root.destroy)
    """
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    subprocess.Popen(
        ["cmd.exe", "/c", batch_path],
        startupinfo=startupinfo,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if root_close_cb:
        root_close_cb()
