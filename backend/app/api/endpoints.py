import shutil
import os
import uuid
import json
import re
import urllib.parse
import httpx
import time
from typing import List, Optional
from fastapi import APIRouter, Request, Header, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from app.services.ollama_client import get_tags, stream_completion
from app.services.rag_engine import rag_engine
from app.services.llm_checker import get_safe_context, get_chars_per_token, SYSTEM_CPT
from pydantic import BaseModel
import subprocess
import asyncio
from app.config import settings
from filelock import FileLock
import json_repair

# Файловый мьютекс для /api/ingest: работает при uvicorn --workers N (несколько процессов).
# asyncio.Lock() работает только внутри одного процесса — при нескольких воркерах не защищает.
_INGEST_LOCK_PATH = os.path.join(os.getcwd(), "data", ".ingest.lock")
os.makedirs(os.path.dirname(_INGEST_LOCK_PATH), exist_ok=True)

router = APIRouter()

# ... (DTO классы те же) ...
class ChatRequest(BaseModel):
    model: str
    messages: List[dict]
    stream: bool = False

class CompletionRequest(BaseModel):
    model: str
    prompt: str
    max_tokens: Optional[int] = 100
    stream: bool = False
    format: Optional[str] = None
    options: Optional[dict] = None

class ContextRequest(BaseModel):
    text: str

TEMP_DIR = os.path.join(os.getcwd(), "data", "temp")
os.makedirs(TEMP_DIR, exist_ok=True)

# ... (функции construct_prompt те же) ...
# Системный промпт для Ollama — жёсткое ограничение на JSON-only вывод
SYSTEM_PROMPT_JSON = (
    "You are a JSON-only API. You MUST respond with a valid JSON array. "
    "Never include explanations, reasoning, thinking, or commentary. "
    "Start your response with '[' and end with ']'. "
    "Do NOT use markdown code blocks."
)


def _normalize_to_list(parsed):
    """Нормализует JSON к списку: dict -> [dict], list -> list.
    
    Обрабатывает паттерны reasoning-моделей (deepseek-r1, qwen):
    {"results": [...]} -> [...]
    {"data": [...]}    -> [...]
    Если в dict несколько ключей и хоть один — список объектов, берём его.
    """
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        # Паттерн: {"results": [...], ...} — берём первый список-значение
        for v in parsed.values():
            if isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict):
                return v
        # Один объект без списка — оборачиваем
        return [parsed]
    return None


def extract_json_from_llm_response(raw_text: str):
    """Извлекает JSON из сырого ответа LLM, даже если модель нагенерила мусор вокруг."""
    if not raw_text:
        return None

    # 1. Попробовать напрямую
    try:
        parsed = json.loads(raw_text)
        result = _normalize_to_list(parsed)
        if result is not None:
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. Найти JSON массив в тексте (жадный поиск от первого [ до последнего ])
    start = raw_text.find('[')
    end = raw_text.rfind(']')
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(raw_text[start:end + 1])
            result = _normalize_to_list(parsed)
            if result is not None:
                return result
        except (json.JSONDecodeError, ValueError):
            pass

    # 3. Найти JSON объект в тексте (от первого { до последнего })
    start = raw_text.find('{')
    end = raw_text.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(raw_text[start:end + 1])
            result = _normalize_to_list(parsed)
            if result is not None:
                return result
        except (json.JSONDecodeError, ValueError):
            pass

    # 4. Найти JSON в markdown блоке
    match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', raw_text)
    if match:
        try:
            parsed = json.loads(match.group(1))
            result = _normalize_to_list(parsed)
            if result is not None:
                return result
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def _find_style_by_keyword(style_map: dict, keywords: list[str]) -> Optional[str]:
    """Ищет первый стиль из style_map, чьё имя содержит одно из keywords."""
    if not style_map: return None
    for s_name in style_map.keys():
        s_lower = s_name.lower()
        if any(kw.lower() in s_lower for kw in keywords):
            return s_name
    return None

def apply_heuristics(paragraphs: list[dict], style_map: dict) -> dict[int, str]:
    """
    Шаг A: Применяет простые правила (эвристики) для назначения стилей.
    Ищет динамические стили из RAG (без хардкода).
    Возвращает {id: style_name}.
    """
    results = {}
    
    # Ищем подходящие стили из документа
    heading_style = _find_style_by_keyword(style_map, ["heading", "заголовок", "title", "глава"])
    list_num_style = _find_style_by_keyword(style_map, ["list number", "список", "нумеров"])
    list_bul_style = _find_style_by_keyword(style_map, ["list bullet", "маркиров", "bullet"])
    
    for p in paragraphs:
        pid = p.get("id")
        text = str(p.get("text", "")).strip()
        if not text or pid is None:
            continue
            
        # 1. Заголовок (Короткий + ALL CAPS)
        if heading_style and len(text) <= 80 and text.isupper():
            results[pid] = heading_style
            print(f"  🧠 Heuristic: Заголовок -> [ID: {pid}]")
            continue
            
        # 2. Нумерованный список
        if list_num_style and re.match(r'^\d+[\.\)]\s+', text):
            results[pid] = list_num_style
            print(f"  🧠 Heuristic: Список (Num) -> [ID: {pid}]")
            continue
            
        # 3. Маркированный список
        if list_bul_style and re.match(r'^[-•\*]\s+', text):
            results[pid] = list_bul_style
            print(f"  🧠 Heuristic: Список (Bul) -> [ID: {pid}]")
            continue
            
    return results


@router.get("/api/tags")
async def proxy_tags():
    """Проксирует запрос к Ollama /api/tags для проверки соединения и получения списка моделей клиентом."""
    ollama_url = settings.OLLAMA_BASE_URL.rstrip('/')
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{ollama_url}/api/tags", timeout=5.0)
            resp.raise_for_status()
            return JSONResponse(resp.json())
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/v1/completions")
async def proxy_completions(request: Request):
    """
    Гибридный конвейер: Client Batching + Heuristics + Vector Fast Track + LLM.
    Принимает prompt в формате JSON-массива параграфов: [{"id": 1, "text": "..."}]
    """
    ollama_url = settings.OLLAMA_BASE_URL
    data = await request.json()
    raw_prompt = data.get('prompt', '')
    model_name = data.get('model', '')
    
    # 0. Извлекаем массив параграфов из промпта
    paragraphs = []
    marker = "=== USER CONTENT (CONTENT SOURCE) ==="
    if marker in raw_prompt:
        json_content = raw_prompt.split(marker)[-1].strip()
        try:
            # Клиент отправляет JSON: [{"id": N, "text": "..."}]
            paragraphs = json.loads(json_content)
        except json.JSONDecodeError:
            pass
            
    if not isinstance(paragraphs, list):
        print("⚠️ Warning: proxy_completions did not receive a JSON array of paragraphs.")
        paragraphs = []

    # --- RAG SEARCH (по первому абзацу батча, чтобы найти шаблон документа) ---
    search_query = paragraphs[0]["text"] if paragraphs else raw_prompt
    clean_query = re.sub(r'[^\w\sа-яА-Яa-zA-Z0-9]', ' ', search_query).strip()

    style_map = {}
    best_template_uuid = None
    system_message = ""
    
    if len(clean_query) > 5:
        style_data = rag_engine.search_style_reference(clean_query)
        if style_data:
            best_template_uuid = style_data["source_id"]
            style_map = style_data.get("style_map", {})
            print(f"✅ RAG Template -> {best_template_uuid} (Styles: {len(style_map)})")
            
            # Подготовка жесткого system-промпта
            available_styles = list(style_map.keys())
            styles_json = json.dumps(available_styles, ensure_ascii=False)
            system_message = (
                "YOU ARE A JSON-ONLY STYLE CLASSIFIER.\n"
                "DO NOT SUMMARIZE. DO NOT ADD TEXT. DO NOT REASON.\n"
                f"Available exact style names: {styles_json}\n\n"
                "Return exactly ONE JSON dict where keys are IDs (strings) and values are style names.\n"
                "Example format: {\"1\": \"Normal\", \"2\": \"Heading 1\"}\n"
            )

    response_headers = {}
    if best_template_uuid:
        response_headers["X-Best-Template-ID"] = urllib.parse.quote(best_template_uuid)

    safe_context_budget, is_degraded = await get_safe_context(model_name, ollama_url)
    if is_degraded: response_headers["X-Degraded-Mode"] = "true"

    # =========================================================================
    # THE HYBRID PIPELINE
    # =========================================================================
    final_merged_results: dict[int, str] = {}
    
    # Шаг A: Эвристики
    if paragraphs and style_map:
        heuristic_hits = apply_heuristics(paragraphs, style_map)
        final_merged_results.update(heuristic_hits)
    
    # Фильтруем оставшиеся для Шага B
    remaining_for_vector = [p for p in paragraphs if p["id"] not in final_merged_results]
    
    # Шаг B: Vector Fast Track (Batch)
    if remaining_for_vector and style_map:
        texts_to_search = [p["text"] for p in remaining_for_vector]
        # Дистанция 0.20 — очень высокая уверенность
        vector_hits = rag_engine.search_batch_fast_track(texts_to_search, fast_track_distance=0.20)
        
        for batch_idx, style_name in vector_hits.items():
            original_p = remaining_for_vector[batch_idx]
            # Проверять наличие стиля в style_map для стабильности? (опционально)
            final_merged_results[original_p["id"]] = style_name
            
    # Фильтруем оставшиеся для Шага C (LLM)
    remaining_for_llm = [p for p in paragraphs if p["id"] not in final_merged_results]
    
    # =========================================================================
    # THE MERGE & STREAM
    # =========================================================================
    async def streaming_generator():
        success_count = 0
        last_heartbeat = time.time()
        
        # Немедленный Heartbeat, чтобы клиент (urllib) не отвалился по таймауту 30с
        yield " \n"
        
        # 1. Стримим готовые результаты из A (Heuristics) и B (Vector)
        for pid, style in final_merged_results.items():
            yield f"{json.dumps({'id': pid, 'style_name': style}, ensure_ascii=False)}\n"
            success_count += 1
            
        # 2. Если все обработано — завершаем поток
        if not remaining_for_llm:
            print(f"⚡ Batch completely resolved by FastTrack (A+B)! Yielded {success_count} items.")
            yield "\n"
            return
            
        # 3. Шаг C: Идем в LLM только с самыми сложными параграфами
        print(f"🤖 Calling LLM for {len(remaining_for_llm)} objects...", flush=True)
        
        # Формируем промпт из параграфов формата [ID] Text
        llm_prompt = "\n".join([f"[{p['id']}] {p['text']}" for p in remaining_for_llm])
        
        chat_payload = {
            'model': model_name,
            'messages': [
                {'role': 'system', 'content': system_message},
                {'role': 'user', 'content': llm_prompt}
            ],
            'format': {
                "type": "object",
                "additionalProperties": {"type": "string"}
            },
            'stream': True,
            'options': {
                'num_ctx': safe_context_budget, 
                'temperature': 0.1
            }
        }
        
        clean_url = ollama_url.rstrip('/')
        target_endpoint = f"{clean_url}/api/chat"
        buffer_text = ""
        
        async with httpx.AsyncClient() as client:
            try:
                async with client.stream("POST", target_endpoint, json=chat_payload, timeout=600.0) as resp:
                    resp.raise_for_status()
                    
                    async for chunk in resp.aiter_lines():
                        if await request.is_disconnected(): break
                        if not chunk: continue
                        
                        chunk_data = json.loads(chunk)
                        chunk_text = chunk_data.get("message", {}).get("content", "")
                        if chunk_text:
                            buffer_text += chunk_text
                            
                        # Heartbeat
                        now = time.time()
                        if now - last_heartbeat > 5.0:
                            yield " \n"
                            last_heartbeat = now
                            
            except Exception as e:
                print(f"❌ LLM Stream Error: {e}")
                yield f"{{\"error\": \"{str(e)}\"}}\n"
                return

        # После завершения стрима Ollama, чиним и парсим накопленный буфер
        llm_handled_ids = set()
        if buffer_text.strip():
            try:
                # json_repair вылечит обрывы (незакрытые скобки/кавычки)
                parsed_dict = json_repair.loads(buffer_text)
                
                if isinstance(parsed_dict, dict):
                    print(f"✅ LLM buffer repaired & parsed. Items: {len(parsed_dict)}", flush=True)
                    for k, llm_style_name in parsed_dict.items():
                        if not str(k).isdigit(): continue
                        pid = int(k)
                        
                        # --- ОБЪЕДИНЕНИЕ С ДАННЫМИ RAG (DNA стиля) ---
                        # Берем параметры стиля из RAG-карты (шрифт, размер, жирность)
                        rag_style_info = style_map.get(llm_style_name, {})
                        
                        # Собираем финальный объект для клиента
                        enriched_item = {
                            "id": pid,
                            "style_name": llm_style_name,
                            "font_family": rag_style_info.get("font_family"),
                            "font_size": rag_style_info.get("font_size"),
                            "bold": rag_style_info.get("bold", False),
                            "align": rag_style_info.get("align", "left")
                        }
                        
                        yield f"{json.dumps(enriched_item, ensure_ascii=False)}\n"
                        llm_handled_ids.add(pid)
                        success_count += 1
                else:
                    print(f"⚠️ LLM returned non-dict JSON: {type(parsed_dict)}")
            except Exception as e:
                print(f"❌ JSON Repair failed for buffer: {e}")
                
        # 4. Fallback (The Catch-All). Если LLM забыла вернуть стили для части ID,
        #    возвращаем для них "Normal", чтобы LibreOffice не "потерял" эти параграфы.
        missing_ids = [p["id"] for p in remaining_for_llm if p["id"] not in llm_handled_ids]
        if missing_ids:
            print(f"⚠️ LLM lost {len(missing_ids)} IDs! Applying 'Normal' fallback.")
            for pid in missing_ids:
                yield f"{json.dumps({'id': pid, 'style_name': 'Normal'}, ensure_ascii=False)}\n"
                success_count += 1
                
        print(f"🏁 Hybrid Stream Finished. Total pushed: {success_count}.")
        yield "\n"

    response_headers["Content-Type"] = "application/x-ndjson"
    return StreamingResponse(streaming_generator(), headers=response_headers)




# ... (остальные методы ingest/retrieve те же) ...
@router.post("/api/ingest")
async def ingest_document(file: UploadFile = File(...)):
    """
    Загружает .docx в RAG-индекс через ChromaDB/SQLite.
    FileLock (файловый мьютекс): работает при uvicorn --workers N.
    asyncio.Lock() не защищает от concurrent записи при нескольких воркерах.
    """
    # FileLock: кросс-процессный, выполняем в отдельном потоке чтобы не блокировать event loop
    def _do_ingest(file_path: str, file_ext: str, unique_filename: str):
        processing_path = file_path
        if file_ext != "docx":
            subprocess.run(
                ["soffice", "--headless", "--convert-to", "docx", file_path, "--outdir", TEMP_DIR],
                check=True
            )
            processing_path = file_path.replace(f".{file_ext}", ".docx")
            unique_filename = unique_filename.replace(f".{file_ext}", ".docx")
        with FileLock(_INGEST_LOCK_PATH, timeout=120):
            rag_engine.add_document(processing_path, unique_filename)
        return unique_filename

    file_ext = file.filename.split(".")[-1].lower()
    unique_filename = f"{uuid.uuid4()}.{file_ext}"
    file_path = os.path.join(TEMP_DIR, unique_filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    try:
        result_uuid = await asyncio.to_thread(_do_ingest, file_path, file_ext, unique_filename)
        return JSONResponse({"status": "indexed", "uuid": result_uuid})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/retrieve_context")
def retrieve_context(request: ContextRequest):
    try:
        data = rag_engine.search_style_reference(request.text)
        if data: return {"context": data["full_context"], "source_id": data["source_id"]}
        return {"context": "No reference found.", "source_id": None}
    except Exception as e: return {"context": f"Error: {e}", "source_id": None}

@router.post("/api/extract_ground_truth")
async def extract_ground_truth_api(file: UploadFile = File(...)):
    """API для извлечения Ground Truth напрямую из бэкенда (без дублирования логики в тесте)."""
    file_ext = file.filename.split(".")[-1].lower()
    unique_filename = f"test_gt_{uuid.uuid4()}.{file_ext}"
    file_path = os.path.join(TEMP_DIR, unique_filename)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    processing_path = file_path
    if file_ext != "docx":
        try:
            subprocess.run(["soffice", "--headless", "--convert-to", "docx", file_path, "--outdir", TEMP_DIR], check=True)
            processing_path = file_path.replace(f".{file_ext}", ".docx")
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
            
    try:
        from app.services.style_extractor import style_extractor
        chunks = style_extractor.parse_docx(processing_path)
        
        # Формируем структуру ответа, аналогичную extract_ground_truth в тесте
        records = []
        for chunk in chunks:
            text = chunk.get("text", "").strip()
            if not text or text == "<IMAGE_PLACEHOLDER>":
                continue

            meta = chunk.get("metadata", {})
            style_desc = chunk.get("style_desc", "")

            record = {
                "text": text,
                "style_name": meta.get("style_name", "Normal"),
                "is_header": meta.get("is_header", False),
                "section_type": meta.get("section_type", "body"),
            }

            for m in re.finditer(r'\[([^:]+):\s*([^\]]+)\]', style_desc):
                record[f"tag_{m.group(1).strip()}"] = m.group(2).strip()

            records.append(record)
            
        plain_text = "\n\n".join([r["text"] for r in records if "text" in r])
        
        return JSONResponse({"ground_truth": records, "plain_text": plain_text})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)