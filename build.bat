@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

:: ── Читаем версию из version.txt ──
set /p VERSION=<version.txt
if "%VERSION%"=="" (
    echo [ERROR] version.txt not found or empty!
    pause
    exit /b 1
)
echo ===== Сборка Миграция_OLT v%VERSION% =====
echo.

:: ── Определяем имя EXE ──
set EXE_NAME=Миграция_OLT_v%VERSION%

:: ── Сборка PyInstaller ──
pyinstaller --onefile --windowed --uac-admin ^
    --name "%EXE_NAME%" ^
    --add-data "version.txt;." ^
    --add-data "config.toml;." ^
    --add-data "icon.ico;." ^
    --add-data "snmp;snmp" ^
    --hidden-import paramiko ^
    --hidden-import tkinter ^
    --hidden-import tkinter.font ^
    --hidden-import openpyxl ^
    --hidden-import openpyxl.styles ^
    --hidden-import openpyxl.utils ^
    --hidden-import docx ^
    --hidden-import requests ^
    --hidden-import win32print ^
    --hidden-import win32ui ^
    --hidden-import win32con ^
    --hidden-import ctypes ^
    --hidden-import urllib.request ^
    --hidden-import urllib.error ^
    --hidden-import urllib.parse ^
    --hidden-import json ^
    --hidden-import ssl ^
    --hidden-import threading ^
    --hidden-import queue ^
    --hidden-import concurrent.futures ^
    --hidden-import difflib ^
    --hidden-import subprocess ^
    --hidden-import tempfile ^
    --hidden-import http.cookiejar ^
    --hidden-import socket ^
    --icon=icon.ico ^
    migration_app.py

if %errorlevel% equ 0 (
    echo.
    echo ===== OK: dist\%EXE_NAME%.exe (v%VERSION%) =====
    echo Размер:
    dir "dist\%EXE_NAME%.exe"
) else (
    echo.
    echo ===== FAILED =====
)
pause
