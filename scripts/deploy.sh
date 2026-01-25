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

echo "📂 Project Root: $PROJECT_ROOT"
echo "🛑 Закрываем LibreOffice..."
killall -9 soffice.bin soffice 2>/dev/null

echo "📦 Собираем новый пакет..."
mkdir -p "$BUILD_DIR"
rm "$BUILD_DIR/$EXT_FILE" 2>/dev/null

# Переходим в папку с исходниками расширения для правильной архивации
cd "$SOURCE_DIR" || exit
# Зипуем всё содержимое папки extension
zip -r -q "$BUILD_DIR/$EXT_FILE" *

echo "🧹 Удаляем старую версию..."
$LO_PATH/unopkg remove $EXT_ID --force >/dev/null 2>&1

echo "🚀 Устанавливаем расширение..."
$LO_PATH/unopkg add --force "$BUILD_DIR/$EXT_FILE"

if [ $? -eq 0 ]; then
    echo "✅ УСПЕШНО! Запускаем Writer..."
    nohup soffice --writer >/dev/null 2>&1 &
else
    echo "❌ ОШИБКА УСТАНОВКИ!"
fi

echo "📄 Открываем лог файл..."
if [ -f /tmp/localwriter.log ]; then
    code /tmp/localwriter.log
else
    echo "Лог-файл еще не создан."
fi