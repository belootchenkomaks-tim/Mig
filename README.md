# Mig — Миграция абонентов OLT (Eltex LTE-8ST → C-Data)

Перенос GPON-абонентов со старых OLT Eltex на C-Data с сохранением VLAN, договоров, адресов и телефонов.

## Возможности

- **10-шаговый конвейер**: CSV → Eltex VLAN → Userside адрес → Billing VLAN → новые VLAN → C-Data слоты → команды → Excel
- **Параллельная обработка**: 3 потока для Eltex SSH + Userside HTTP + Billing API
- **Цветная подсветка** в Excel: жёлтый (нет адреса US), оранж (OLT=? + billing найден), красный (расхождение VLAN)
- **Автообновление** через GitHub Releases (кнопка 🔄)
- **GUI** на tkinter с прогресс-баром и логом

## Быстрый старт

```cmd
cd NewScript
pip install -r requirements.txt
python migration_app.py
```

## Сборка EXE

```cmd
build.bat
```

См. `BUILD_GUIDE.md` для подробностей.
