import shutil
import os
import uuid
import json
import re
import urllib.parse
import httpx 
from typing import List, Optional
from fastapi import APIRouter, Request, Header, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from app.services.ollama_client import get_tags, stream_completion
from app.services.rag_engine import rag_engine
from pydantic import BaseModel
import subprocess
from app.config import settings

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
    """Нормализует JSON к списку: dict -> [dict], list -> list."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        # Модель вернула один объект — оборачиваем в список
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


def construct_deep_style_prompt(context_snippets: str) -> str:
    return (
        "You are a Document Layout Engine (Deep Style v2).\n"
        "Your goal involves two inputs:\n"
        "1. REFERENCE (Style Source): Fragments of a document with technical tags.\n"
        "2. USER CONTENT: Raw text that needs formatting.\n\n"
        "### UNDERSTANDING THE TAGS\n"
        "- [S: Name]: The Style Name.\n"
        "- [F: Name]: Font Family.\n"
        "- [P: 12.0]: Font Size.\n"
        "- [B: True]: Bold.\n"
        "- [A: CENTER]: Alignment.\n\n"
        "### TASK\n"
        "Map the USER CONTENT to the visual structure of the REFERENCE.\n"
        "CRITICAL: Output ONLY a raw JSON array. No markdown, no explanations, no thinking.\n"
        "Start your response with '[' and end with ']'.\n\n"
        "### OUTPUT FORMAT (JSON ONLY)\n"
        "[{\"type\": \"header\", \"text\": \"...\", \"style_name\": \"...\", "
        "\"font_family\": \"...\", \"font_size\": 12.0, \"bold\": true, \"align\": \"left\"}]\n"
    )

def construct_generic_prompt() -> str:
    return (
        "You are a formatting assistant. Convert the user text into a JSON structure.\n"
        "Return ONLY valid JSON."
    )

@router.get("/api/tags")
async def proxy_tags(x_target_ollama_url: str = Header(None, alias="X-Target-Ollama-Url")):
    if not x_target_ollama_url: x_target_ollama_url = "http://localhost:11434"
    data = await get_tags(x_target_ollama_url)
    return JSONResponse(content=data)

@router.post("/v1/completions")
async def proxy_completions(
    request: Request, 
    x_target_ollama_url: str = Header(None, alias="X-Target-Ollama-Url")
):
    if not x_target_ollama_url: x_target_ollama_url = "http://localhost:11434"
    
    data = await request.json()
    raw_prompt = data.get('prompt', '')
    best_template_uuid = None
    
    # --- RAG LOGIC ---
    marker = "=== USER CONTENT (CONTENT SOURCE) ==="
    if marker in raw_prompt:
        search_query = raw_prompt.split(marker)[-1]
    else:
        search_query = raw_prompt

    # ОПТИМИЗАЦИЯ: Обрезка
    if len(search_query) > settings.MAX_INPUT_CHARS:
        print(f"✂️ Truncating input from {len(search_query)} to {settings.MAX_INPUT_CHARS} chars (Limit Policy)")
        search_query = search_query[:settings.MAX_INPUT_CHARS] + "... [TRUNCATED]"

    clean_query = re.sub(r'[^\w\sа-яА-Яa-zA-Z0-9]', ' ', search_query).strip()

    # Формируем messages для /api/chat (разделение system/user ролей)
    system_message = SYSTEM_PROMPT_JSON
    user_message = search_query  # Fallback

    if len(clean_query) > 5:
        print(f"🔎 Deep Style Search: '{clean_query[:50]}...'")
        style_data = rag_engine.search_style_reference(clean_query)
        
        if style_data:
            found_context = style_data["full_context"]
            best_template_uuid = style_data["source_id"]
            print(f"✅ RAG: Found Template -> {best_template_uuid}")
            # Системная инструкция = промпт + REFERENCE
            system_message = (
                f"{construct_deep_style_prompt(found_context)}\n\n"
                f"=== REFERENCE DATA ===\n{found_context}"
            )
            # Пользовательское сообщение = только контент
            user_message = search_query
        else:
            print("🔸 RAG: No style reference found.")
            system_message = construct_generic_prompt()
            user_message = search_query
            
    # Формируем payload для /api/chat
    chat_payload = {
        'model': data.get('model', ''),
        'messages': [
            {'role': 'system', 'content': system_message},
            {'role': 'user', 'content': user_message}
        ],
        'format': 'json',
        'stream': False,
        'options': {
            'num_ctx': settings.OLLAMA_CTX,
            'temperature': 0.1
        }
    }

    response_headers = {}
    if best_template_uuid:
        safe_header_value = urllib.parse.quote(best_template_uuid)
        response_headers["X-Best-Template-ID"] = safe_header_value

    clean_url = x_target_ollama_url.rstrip('/')
    target_endpoint = f"{clean_url}/api/chat"
    print(f"Proxying request to -> {target_endpoint}")

    # --- BLOCKING REQUEST (Apply Template) ---
    if not data.get('stream'):
        async with httpx.AsyncClient() as client:
            try:
                # ДИНАМИЧЕСКИЙ ТАЙМАУТ
                full_prompt_len = len(system_message) + len(user_message)
                dynamic_timeout = settings.estimate_timeout(full_prompt_len)
                
                print(f"⏳ Dynamic Timeout: {dynamic_timeout:.1f}s (Prompt: {full_prompt_len} chars, TPS: {settings.current_tps:.1f})")
                
                resp = await client.post(target_endpoint, json=chat_payload, timeout=dynamic_timeout)
                resp.raise_for_status()
                ollama_response = resp.json()
                
                # /api/chat возвращает ответ в message.content, а не в response
                raw_llm_text = ""
                if "message" in ollama_response:
                    raw_llm_text = ollama_response["message"].get("content", "")
                elif "response" in ollama_response:
                    raw_llm_text = ollama_response.get("response", "")
                
                parsed_json = extract_json_from_llm_response(raw_llm_text)
                
                if parsed_json is not None:
                    # Приводим ответ к формату, который ожидает клиент (поле "response")
                    ollama_response["response"] = json.dumps(parsed_json, ensure_ascii=False)
                    print(f"✅ JSON extracted successfully ({len(parsed_json)} elements)")
                else:
                    print(f"⚠️ JSON EXTRACTION FAILED. Raw (first 500 chars): {raw_llm_text[:500]}")
                    # Оставляем сырой ответ — клиент попробует сам
                
                return JSONResponse(content=ollama_response, headers=response_headers)
            except Exception as e:
                # ВАЖНО: Выводим реальную ошибку в консоль
                print(f"❌ Ollama Error: {type(e).__name__}: {e}")
                return JSONResponse({"error": f"{type(e).__name__}: {str(e)}"}, status_code=500)

    # --- STREAMING ---
    else:
        return StreamingResponse(
            stream_completion(x_target_ollama_url, data), 
            media_type="text/event-stream",
            headers=response_headers 
        )

# ... (остальные методы ingest/retrieve те же) ...
@router.post("/api/ingest")
async def ingest_document(file: UploadFile = File(...)):
    file_ext = file.filename.split(".")[-1].lower()
    unique_filename = f"{uuid.uuid4()}.{file_ext}"
    file_path = os.path.join(TEMP_DIR, unique_filename)
    with open(file_path, "wb") as buffer: shutil.copyfileobj(file.file, buffer)
    processing_path = file_path
    if file_ext != "docx":
        try:
            subprocess.run(["soffice", "--headless", "--convert-to", "docx", file_path, "--outdir", TEMP_DIR], check=True)
            processing_path = file_path.replace(f".{file_ext}", ".docx")
            unique_filename = unique_filename.replace(f".{file_ext}", ".docx")
        except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)
    try:
        rag_engine.add_document(processing_path, unique_filename)
        return JSONResponse({"status": "indexed", "uuid": unique_filename})
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

@router.post("/api/retrieve_context")
def retrieve_context(request: ContextRequest):
    try:
        data = rag_engine.search_style_reference(request.text)
        if data: return {"context": data["full_context"], "source_id": data["source_id"]}
        return {"context": "No reference found.", "source_id": None}
    except Exception as e: return {"context": f"Error: {e}", "source_id": None}