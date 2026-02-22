#!/bin/bash

# Настройки
EXT_ID="org.extension.sample"
EXT_FILE="localwriter.oxt"
# Определяем путь к папке extension относительно скрипта
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SOURCE_DIR="$PROJECT_ROOT/extension"
BUILD_DIR="$PROJECT_ROOT/build"
LO_PATH="/usr/lib/libreoffice/program"
# Путь к lock-файлу (обычно здесь в Linux)
LOCK_FILE="$HOME/.config/libreoffice/4/.lock"

# FIX: Запуск unopkg изнутри активированного Python virtualenv вызывает std::bad_alloc
# Поэтому очищаем переменные окружения, связанные с Python, перед запуском инструментов LibreOffice
unset VIRTUAL_ENV
unset PYTHONHOME
unset PYTHONPATH

# Важно также убрать виртуальное окружение из PATH, иначе LibreOffice всё равно найдет python из poetry
export PATH=$(echo "$PATH" | tr ':' '\n' | grep -v "/\.cache/pypoetry/virtualenvs/" | paste -sd ':' -)

echo "📂 Project Root: $PROJECT_ROOT"
echo "🛑 Закрываем LibreOffice..."
killall -9 soffice.bin soffice 2>/dev/null
# Даем секунду системе на освобождение ресурсов
sleep 1

# --- FIX: УДАЛЕНИЕ LOCK ФАЙЛА ---
if [ -f "$LOCK_FILE" ]; then
    echo "🔓 Удаляем зависший lock-файл..."
    rm -f "$LOCK_FILE"
fi
# --------------------------------

echo "📦 Собираем новый пакет..."
mkdir -p "$BUILD_DIR"
rm "$BUILD_DIR/$EXT_FILE" 2>/dev/null

# Переходим в папку с исходниками расширения для правильной архивации
cd "$SOURCE_DIR" || exit
# Зипуем всё содержимое папки extension
zip -r -q "$BUILD_DIR/$EXT_FILE" *

echo "🧹 Удаляем старую версию..."
# unopkg тоже может ругаться на lock, поэтому удаляем его до unopkg
$LO_PATH/unopkg remove $EXT_ID --force >/dev/null 2>&1

echo "🚀 Устанавливаем расширение..."
$LO_PATH/unopkg add --force --suppress-license "$BUILD_DIR/$EXT_FILE"

if [ $? -eq 0 ]; then
    echo "✅ УСПЕШНО! Запускаем Writer..."
    nohup soffice --writer >/dev/null 2>&1 &
else
    echo "❌ ОШИБКА УСТАНОВКИ!"
    # Если ошибка, попробуем вывести, что сказал unopkg (убрав перенаправление в null выше для отладки, если понадобится)
fi

echo "📄 Открываем лог файл..."
if [ -f /tmp/localwriter.log ]; then
    # Если code не установлен, можно заменить на xdg-open или cat
    if command -v code &> /dev/null; then
        code /tmp/localwriter.log
    else
        cat /tmp/localwriter.log
    fi
else
    echo "Лог-файл еще не создан."
fi