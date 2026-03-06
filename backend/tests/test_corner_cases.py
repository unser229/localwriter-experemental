import sys
import os
import queue
import threading
import time
import json

# Фиктивный urllib.request для перехвата вызовов
import urllib.request
from io import BytesIO

# Добавляем путь к клиенту
EXTENSION_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../extension"))
sys.path.insert(0, EXTENSION_DIR)

from client import call_apply_template_ndjson

def run_heartbeat_test():
    print("=== TEST 1: Heartbeat & JSONDecodeError ===")
    
    class DummyResponse:
        def __init__(self, data_lines):
            self.lines = data_lines
            self.headers = {"X-Degraded-Mode": "false", "X-Best-Template-ID": "test_123"}
            self.idx = 0
            
        def readline(self):
            if self.idx < len(self.lines):
                line = self.lines[self.idx]
                self.idx += 1
                time.sleep(0.1)  # Имитация задержки сети
                return line
            return b""
            
        def close(self): pass

    # Имитируем ответ сервера:
    # 1. Heartbeat
    # 2. Валидный JSON
    # 3. Мусор
    # 4. Heartbeat
    # 5. Еще один JSON
    mock_stream = [
        b" \n", # Heartbeat
        b'{"text": "Paragraph 1", "style_name": "Normal"}\n',
        b" \n", # Heartbeat
        b" \n", # Heartbeat
        b"{invalid json! \n", # Мусор
        b'{"text": "Paragraph 2", "style_name": "Heading 1"}\n'
    ]

    # Подменяем urlopen
    original_urlopen = urllib.request.urlopen
    
    def mock_urlopen(req, timeout=120):
        return DummyResponse(mock_stream)
        
    urllib.request.urlopen = mock_urlopen
    
    result_queue = queue.Queue()
    stop_event = threading.Event()
    
    try:
        is_degraded, rag_id = call_apply_template_ndjson(
            content="test",
            model="test",
            middleware_url="http://dummy",
            result_queue=result_queue,
            stop_event=stop_event
        )
        
        print(f"Initial Setup: is_degraded={is_degraded}, rag_id={rag_id}")
        
        results = []
        while True:
            item = result_queue.get(timeout=2.0)
            if "DONE" in item or "error" in item:
                if "error" in item: print("Error in queue:", item["error"])
                break
            results.append(item)
            
        print(f"Extracted JSON blocks: {len(results)}")
        for i, r in enumerate(results):
            print(f"  [{i}] {r}")
            
        assert len(results) == 2, "Должно быть ровно 2 валидных объекта"
        print("✅ HEARTBEAT TEST PASSED: Ошибок парсинга не возникло, поток не упал.\n")
        
    finally:
        urllib.request.urlopen = original_urlopen


if __name__ == "__main__":
    run_heartbeat_test()
