import time
import httpx
from app.config import settings

async def calibrate_ollama(ollama_url: str = "http://localhost:11434", model: str = ""):
    """
    Отправляет короткий запрос, чтобы прогреть модель и замерить скорость.
    """
    print("⏳ Calibrating Inference Speed...")
    
    # 1. Если модель не задана, пытаемся узнать список тегов, берем первый попавшийся
    # (В реальном проекте лучше брать ту, что в конфиге, но здесь упростим)
    target_model = model or "gemma:2b" 
    
    # Пытаемся получить список моделей, если имя не передали
    if not model:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{ollama_url}/api/tags", timeout=2.0)
                data = resp.json()
                if data.get("models"):
                    target_model = data["models"][0]["name"]
        except:
            print("⚠️ Calibration skipped: Could not connect to Ollama.")
            return

    # 2. Тестовый промпт
    payload = {
        "model": target_model,
        "prompt": "Write one short sentence about sky.",
        "stream": False,
        "options": {"num_ctx": 2048} # Маленький контекст для теста
    }

    try:
        start_time = time.perf_counter()
        async with httpx.AsyncClient() as client:
            # Таймаут 30 сек на "прогрев" (загрузку в память)
            resp = await client.post(f"{ollama_url}/api/generate", json=payload, timeout=30.0)
            
        end_time = time.perf_counter()
        
        if resp.status_code == 200:
            data = resp.json()
            # Ollama возвращает точные метрики времени
            # eval_count - количество токенов ответа
            # eval_duration - время генерации в наносекундах
            
            eval_count = data.get("eval_count", 0)
            eval_duration_ns = data.get("eval_duration", 0)
            
            # Если метрики есть - используем их (это чистая скорость GPU/CPU)
            if eval_count > 0 and eval_duration_ns > 0:
                tps = eval_count / (eval_duration_ns / 1e9)
            else:
                # Иначе считаем по "грязному" времени (включая сеть и препроцессинг)
                total_time = end_time - start_time
                # Примерно считаем, что ответ был токенов 10-15
                tps = 10.0 / total_time 

            # 3. ОБНОВЛЯЕМ НАСТРОЙКИ
            settings.update_from_benchmark(tps)
            
        else:
            print(f"⚠️ Calibration failed: Status {resp.status_code}")

    except Exception as e:
        print(f"⚠️ Calibration failed (Ollama might be down): {e}")