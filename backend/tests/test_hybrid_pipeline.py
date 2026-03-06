import asyncio
import json
import httpx
from unittest.mock import patch, MagicMock

import sys
import os

# Добавляем путь, чтобы импортировать app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api.endpoints import proxy_completions
from app.services.rag_engine import rag_engine

class MockRequest:
    def __init__(self, json_data):
        self._json = json_data

    async def json(self):
        return self._json

    async def is_disconnected(self):
        return False

async def main():
    print("🚀 Starting Hybrid Pipeline Merge Test...")

    # Генерируем тестовый батч из 15 параграфов
    paragraphs = []
    
    # 3 параграфа капсом (Эвристики)
    paragraphs.append({"id": 1, "text": "КРАТКИЙ ЗАГОЛОВОК 1"})
    paragraphs.append({"id": 2, "text": "MAIN TITLE ALL CAPS"})
    paragraphs.append({"id": 3, "text": "ВВЕДЕНИЕ"})
    
    # 3 параграфа гарантированно (RAG Fast Track)
    paragraphs.append({"id": 4, "text": "Vector match 1"})
    paragraphs.append({"id": 5, "text": "Vector match 2"})
    paragraphs.append({"id": 6, "text": "Vector match 3"})
    
    # 9 обычных параграфов (LLM)
    for i in range(7, 16):
        paragraphs.append({"id": i, "text": f"Обычный текст параграфа номер {i}"})

    prompt_data = {
        "prompt": "=== USER CONTENT (CONTENT SOURCE) ===\n" + json.dumps(paragraphs),
        "model": "lfm2:latest"
    }
    req = MockRequest(prompt_data)

    # Мокаем RAG engine
    original_search_style = rag_engine.search_style_reference
    original_fast_track = rag_engine.search_batch_fast_track
    
    rag_engine.search_style_reference = MagicMock(return_value={
        "source_id": "test_uuid",
        "style_map": {
            "Heading 1": {"type": "paragraph"},
            "Normal": {"type": "paragraph"}
        }
    })
    
    # Fast track должен вернуть стили для 0-го, 1-го, 2-го элементов из батча (индексы внутри remaining_for_vector)
    # В remaining_for_vector будут параграфы с ID от 4 до 15. Значит, индексы 0, 1, 2 соответствуют ID 4, 5, 6.
    rag_engine.search_batch_fast_track = MagicMock(return_value={
        0: "Heading 1",
        1: "Heading 1",
        2: "Normal"
    })

    # Мокаем LLM вызов
    # LLM по ошибке вернет только 5 ID из 9 запрошенных
    llm_dummy_response = "{\"7\": \"Normal\", \"8\": \"Normal\", \"9\": \"Normal\", \"10\": \"Normal\", \"11\": \"Normal\"}"
    
    class MockResponse:
        def raise_for_status(self): pass
        async def aiter_lines(self):
            yield json.dumps({"message": {"content": llm_dummy_response}})
            yield ""
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class MockAsyncClient:
        def stream(self, *args, **kwargs):
            class StreamContext:
                async def __aenter__(self): return MockResponse()
                async def __aexit__(self, exc_type, exc_val, exc_tb): pass
            return StreamContext()
        async def __aenter__(self): return self
        async def __aexit__(self, exc_type, exc_val, exc_tb): pass

    with patch("httpx.AsyncClient", new=MockAsyncClient):
        # Вызываем конвейер
        response = await proxy_completions(req)
        
        # Получаем стрим
        collected_ids = set()
        async for chunk in response.body_iterator:
            chunk = chunk.strip()
            if not chunk: continue
            try:
                data = json.loads(chunk)
                if "id" in data:
                    collected_ids.add(data["id"])
            except:
                pass

    print(f"✅ Найдено ID в стриме сборщика: {len(collected_ids)}")
    print(f"🆔 Collected IDs: {sorted(list(collected_ids))}")

    missing = set(range(1, 16)) - collected_ids
    if missing:
        print(f"❌ ОШИБКА: Потеряны ID: {missing}")
        exit(1)
    else:
        print("🎉 Все 15 ID успешно вернулись!")
        exit(0)

if __name__ == "__main__":
    asyncio.run(main())
