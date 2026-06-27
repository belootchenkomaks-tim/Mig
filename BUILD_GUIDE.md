# Сборка и публикация EXE (Миграция OLT)

## 1. Обновить версию

Файл `version.txt` содержит номер версии. Изменить вручную:

```
echo 1.1 > version.txt
```

Версия отображается в заголовке окна: `Миграция абонентов OLT v1.0`.

---

## 2. Установить зависимости

```cmd
pip install pyinstaller paramiko openpyxl python-docx requests pywin32
```

> `pywin32` нужен для печати этикеток (win32print, win32ui, win32con).

---

## 3. Собрать .exe

### Способ A — через build.bat (рекомендуется)

```cmd
cd NewScript
build.bat
```

Делает:
- Читает версию из `version.txt`
- Запускает PyInstaller с параметрами

Готовый `.exe` → `dist\Миграция_OLT_v1.0.exe`

### Способ B — вручную

```cmd
pyinstaller --onefile --windowed --uac-admin ^
    --name "Миграция_OLT_v1.0" ^
    --add-data "version.txt;." ^
    --add-data "config.toml;." ^
    --add-data "icon.ico;." ^
    --add-data "snmp;snmp" ^
    --add-data "*.py;." ^
    --hidden-import paramiko ^
    --hidden-import tkinter ^
    --hidden-import openpyxl ^
    --hidden-import docx ^
    --hidden-import requests ^
    --hidden-import win32print ^
    --hidden-import win32ui ^
    --hidden-import win32con ^
    --icon=icon.ico ^
    migration_app.py
```

---

## 4. Создать релиз на GitHub

### Шаг 4.1: Создать репозиторий

1. Зайти на https://github.com/new
2. Название: например `migration-olt` (публичный)
3. Залить код:
```cmd
git init
git add .
git commit -m "Initial: migration OLT v1.0"
git remote add origin https://github.com/ВАШ_АККАУНТ/migration-olt.git
git push -u origin main
```

### Шаг 4.2: Создать Personal Access Token

https://github.com/settings/tokens → **Generate new token (classic)**
- Галочка `repo`
- Скопировать токен

### Шаг 4.3: Создать тег и релиз

```cmd
set TOKEN=ваш_токен
set VERSION=v1.0
```

```cmd
curl -s -X POST ^
  -H "Authorization: token %TOKEN%" ^
  -H "Accept: application/vnd.github.v3+json" ^
  https://api.github.com/repos/ВАШ_АККАУНТ/migration-olt/releases ^
  -d "{\"tag_name\":\"%VERSION%\",\"name\":\"%VERSION%\",\"body\":\"## %VERSION%\\n\\n- Что нового...\"}"
```

В ответе будет JSON с `id` релиза (запомнить).

### Шаг 4.4: Загрузить .exe

```cmd
curl -s -X POST ^
  -H "Authorization: token %TOKEN%" ^
  -H "Content-Type: application/octet-stream" ^
  "https://uploads.github.com/repos/ВАШ_АККАУНТ/migration-olt/releases/ID_REЛИЗА/assets?name=Migratsiya_OLT_v1.0.exe" ^
  --data-binary @dist/Миграция_OLT_v1.0.exe
```

### Шаг 4.5: Отозвать токен

https://github.com/settings/tokens → удалить использованный токен.

---

## 5. Обновить GITHUB_REPO в коде

После создания репозитория — отредактировать `updater.py`:

```python
GITHUB_REPO = "ВАШ_АККАУНТ/migration-olt"
```

И пересобрать .exe.

---

## 6. Проверить автообновление

1. Убедиться, что релиз создан и содержит .exe в Assets
2. Запустить старую версию `.exe`
3. Нажать кнопку **🔄** в правом верхнем углу
4. Приложение должно найти новую версию, скачать и перезапуститься

---

## Структура проекта (ключевые файлы)

```
NewScript/
├── migration_app.py        # 🏃 Главный GUI (точка входа)
├── updater.py              # 🔄 GitHub-автообновление
├── orchestrator.py         # 🔧 Центральный конвейер (10 шагов)
├── billing_client.py       # 💳 LANBilling API клиент
├── us_client.py            # 🌐 Userside клиент
├── eltex_client.py         # 🔧 Eltex SSH клиент
├── cdata_client.py         # 🔧 C-Data SSH+SNMP клиент
├── command_builder.py      # 📋 Генерация команд C-Data
├── excel_utils.py          # 📊 Чтение/запись Excel
├── addr_utils.py           # 📍 Нормализация адресов
├── log_utils.py            # 📝 Логирование
├── config_loader.py        # ⚙️ Чтение config.toml
├── label_printer.py        # 🖨 Печать этикеток
│
├── build.bat               # 🏗 Скрипт сборки PyInstaller
├── BUILD_GUIDE.md          # 📖 Эта инструкция
├── version.txt             # 🔢 Номер версии
├── config.toml             # ⚙️ Настройки (НЕ КОММИТИТЬ!)
├── config.toml.example     # ⚙️ Шаблон без паролей
├── icon.ico                # 🖼 Иконка .exe
├── .gitignore              # 🙈 Исключённые файлы
│
├── snmp/                   # 🔌 Portable Net-SNMP утилиты (для C-Data)
│
├── dist/                   # 📁 Сюда собирается .exe (не коммитить)
└── build/                  # 📁 Временные файлы PyInstaller (не коммитить)
```

---

## Примечания

### Администраторские права
Флаг `--uac-admin` в build.bat требует запуска `.exe` от имени администратора. Это нужно для SNMP-запросов к C-Data (сырые сокеты). Если права не нужны — уберите флаг.

### Работа с config.toml
- `config.toml` содержит пароли — **НЕ КОММИТИТЬ** в Git
- После сборки .exe нужно класть `config.toml` рядом с .exe
- Приложение сначала ищет `config.toml` во встроенных ресурсах, потом рядом с .exe
- Пример конфига без паролей: `config.toml.example`

### Ограничения
- Сборка ТОЛЬКО на Windows (из-за `win32ui`, `win32print`, `win32con`)
- Python 3.8+ (PyInstaller поддерживает до 3.12)
- SNMP-утилиты — portable Net-SNMP для Windows (x86_64)
