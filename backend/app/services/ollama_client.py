import httpx
import json
from typing import AsyncGenerator

# Больше никакого хардкода OLLAMA_URL здесь!

async def get_tags(base_url: str):
    """Получить список моделей с указанного адреса"""
    # Защита от дублирования слешей
    clean_url = base_url.rstrip('/')
    
    async with httpx.AsyncClient() as client:
        try:
            print(f"Proxying tags request to: {clean_url}/api/tags")
            resp = await client.get(f"{clean_url}/api/tags", timeout=5.0)
            return resp.json()
        except Exception as e:
            print(f"Ollama connection error ({clean_url}): {e}")
            return {"models": []}

async def stream_completion(base_url: str, data: dict) -> AsyncGenerator[bytes, None]:
    """
    Проксирует запрос генерации на указанный base_url
    """
    clean_url = base_url.rstrip('/')
    target_endpoint = f"{clean_url}/v1/completions"
    
    print(f"Proxying completion to: {target_endpoint}")
    
    async with httpx.AsyncClient() as client:
        try:
            async with client.stream("POST", target_endpoint, json=data, timeout=60.0) as response:
                async for chunk in response.aiter_bytes():
                    yield chunk
        except Exception as e:
            # Возвращаем ошибку в поток, чтобы клиент увидел её
            yield json.dumps({"error": str(e)}).encode()