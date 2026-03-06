#!/bin/bash
# Полный интеграционный пайплайн LocalWriter.
# Порядок: uvicorn запуск → ожидание готовности → тесты → сервер остается живым.
# При ЛЮБОЙ ошибке: сервер убивается и скрипт завершается с exit code 1.

set -e

# --- Цвета ---
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# --- Конфиг ---
PORT=8323
SERVER_URL="http://localhost:${PORT}"
UVICORN_LOG="uvicorn_test.log"
UVICORN_PID_FILE="/tmp/localwriter_uvicorn.pid"

echo -e "${BLUE}====================================================${NC}"
echo -e "${BLUE}  🚀 LocalWriter: Master Integration Test Suite 🚀 ${NC}"
echo -e "${BLUE}====================================================${NC}\n"

cd "$(dirname "$0")"

# ─────────────────────────────────────────────────────────────────────────────
# Утилиты
# ─────────────────────────────────────────────────────────────────────────────

cleanup_server() {
    # Убиваем uvicorn при выходе по ошибке
    if [ -f "$UVICORN_PID_FILE" ]; then
        local pid
        pid=$(cat "$UVICORN_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo -e "\n${RED}🛑 Остановка uvicorn (PID=$pid) из-за ошибки...${NC}"
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
        rm -f "$UVICORN_PID_FILE"
    fi
}

# Вызываем cleanup только при ошибке (ERR trap срабатывает при set -e)
trap 'cleanup_server; echo -e "${RED}❌ ПАЙПЛАЙН ПРЕРВАН. Проверь логи выше.${NC}"; exit 1' ERR

run_test_step() {
    local label=$1
    local cmd=$2

    echo -e "${YELLOW}▶ Запуск этапа: $label...${NC}"
    if eval "$cmd"; then
        echo -e "${GREEN}✅ УСПЕХ: $label${NC}\n"
    else
        # ERR trap сам вызовет cleanup и выйдет
        false
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# ШАГ 0: Стартуем uvicorn в фоне
# ─────────────────────────────────────────────────────────────────────────────

echo -e "${CYAN}>>> Шаг 0: Запуск FastAPI бэкенда <<<${NC}"

# Убиваем старый процесс, если остался от предыдущего запуска
if [ -f "$UVICORN_PID_FILE" ]; then
    old_pid=$(cat "$UVICORN_PID_FILE")
    if kill -0 "$old_pid" 2>/dev/null; then
        echo -e "   Останавливаем старый uvicorn (PID=$old_pid)..."
        kill "$old_pid" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$UVICORN_PID_FILE"
fi

echo -e "   Запускаем uvicorn на порту ${PORT}..."
poetry run uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --workers 4 \
    > "$UVICORN_LOG" 2>&1 &
UVICORN_PID=$!
echo "$UVICORN_PID" > "$UVICORN_PID_FILE"
echo -e "   PID=$UVICORN_PID | Лог: $UVICORN_LOG"

# Ожидаем готовности сервера (до 30 сек)
echo -n "   Ожидаем /api/tags..."
READY=0
for i in $(seq 1 30); do
    if curl -s --fail "${SERVER_URL}/api/tags" > /dev/null 2>&1; then
        READY=1
        break
    fi
    echo -n "."
    sleep 1
done
echo ""

if [ "$READY" -eq 0 ]; then
    echo -e "${RED}❌ Сервер не стартовал за 30 секунд! Последние строки лога:${NC}"
    tail -20 "$UVICORN_LOG"
    false  # Запустит ERR trap
fi

echo -e "${GREEN}✅ Сервер жив: ${SERVER_URL}${NC}\n"

# ─────────────────────────────────────────────────────────────────────────────
# ШАГ 0.1: Прогрев RAG модели (SentenceTransformer lazy-init)
# Первый вызов инициализирует ~200MB модель. Без этого параллельные тесты
# могут упасть с KeyboardInterrupt при конкурентном импорте transformers.
# ─────────────────────────────────────────────────────────────────────────────

echo -e "${CYAN}>>> Шаг 0.1: Прогрев RAG SentenceTransformer модели <<<${NC}"
echo -n "   Инициализация паттерна эмбеддингов..."
# Используем Python-скрипт для однопоточного прогрева
poetry run python - <<'PYEOF'
import sys, os
sys.path.insert(0, os.getcwd())
try:
    from app.services.rag_engine import RagEngine
    _rag = RagEngine()
    # Выполняем тестовый поиск, чтобы точно загрузить модель
    _rag.collection.query(query_texts=["warmup"], n_results=1)
    print(" готово!")
except Exception as e:
    print(f" ошибка: {e}")
    sys.exit(1)
PYEOF

echo -e "${GREEN}✅ RAG модель прогрета и готова к параллельным тестам${NC}\n"



echo -e "${YELLOW}>>> Шаг 1–2: Базовые и быстрые тесты парсеров <<<${NC}"
run_test_step "XML DOCX Styles Parser" \
    "poetry run python tests/xml_docx_styles.py"
run_test_step "Corner Cases (Heartbeat & Ghost Connects)" \
    "poetry run python tests/test_corner_cases.py"

# ─────────────────────────────────────────────────────────────────────────────
# ШАГ 3–4: Тесты среднего уровня (RAG + Workflow)
# ─────────────────────────────────────────────────────────────────────────────

echo -e "${YELLOW}>>> Шаг 3–4: Комбинированные тесты (Middle) <<<${NC}"
run_test_step "RAG Benchmark" \
    "poetry run python tests/benchmark_rag.py"
run_test_step "Workflow Validator" \
    "poetry run python tests/workflow_validator.py"

# ─────────────────────────────────────────────────────────────────────────────
# ШАГ 5: E2E тест через боевой client.py (production pipeline)
# ─────────────────────────────────────────────────────────────────────────────

echo -e "${YELLOW}>>> Шаг 5: Тяжелый E2E Тест (FastAPI + Ollama + client.py) <<<${NC}"
echo -e "   Сервер: ${SERVER_URL} | Лимит: 5 файлов"
run_test_step "Formatting Quality E2E Test (max 5 docs)" \
    "poetry run python tests/test_formatting_quality.py \
        --server ${SERVER_URL} \
        --max-files 5"

# ─────────────────────────────────────────────────────────────────────────────
# ФИНАЛ: всё зелёное — снимаем ERR trap и оставляем сервер
# ─────────────────────────────────────────────────────────────────────────────

# Убираем ERR trap — сервер теперь должен жить
trap - ERR

echo -e "${GREEN}====================================================${NC}"
echo -e "${GREEN} 🎉 ВСЕ ТЕСТЫ ОБЪЕКТИВНО ПРОЙДЕНЫ! 🎉              ${NC}"
echo -e "${GREEN}====================================================${NC}"
echo -e "${CYAN}✅ Бэкенд Uvicorn работает на ${SERVER_URL} (PID=$UVICORN_PID)${NC}"
echo -e "${CYAN}   Лог сервера: $(pwd)/${UVICORN_LOG}${NC}"
echo -e "${CYAN}   Среда готова для ручного тестирования в LibreOffice!${NC}\n"
