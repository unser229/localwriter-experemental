import shutil
import os
import uuid
import json
from typing import List, Optional  # <--- ДОБАВЛЕН ЭТОТ ИМПОРТ
from fastapi import APIRouter, Request, Header, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from app.services.ollama_client import get_tags, stream_completion
from app.services.docling_parser import docling_service
from app.services.rag_engine import rag_engine
from pydantic import BaseModel
import subprocess



router = APIRouter()

# --- DTO классы (модели данных) ---
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
    user_prompt = data.get('prompt', '')
    
    # Переменная для ID шаблона (имя файла на диске)
    best_template_uuid = None
    
    # --- RAG LOGIC ---
    
    # 1. Очищаем промт от системных инструкций
    search_query = user_prompt
    
    if "CONTENT/CONTEXT:" in user_prompt:
        search_query = user_prompt.split("CONTENT/CONTEXT:")[-1]
    elif "USER INSTRUCTION:" in user_prompt:
        search_query = user_prompt.split("USER INSTRUCTION:")[-1]
    elif "USER:" in user_prompt:
        search_query = user_prompt.split("USER:")[-1]
    
    search_query = search_query.strip()

    # 2. Ищем, если запрос осмысленный
    if len(search_query) > 10:
        print(f"🔎 RAG Search Query: '{search_query[:50]}...'")
        search_results = rag_engine.search(search_query[:1000], n_results=1)
        
        if search_results['documents'] and len(search_results['documents'][0]) > 0:
            found_context = "\n\n".join(search_results['documents'][0])
            
            # Пытаемся достать UUID файла из метаданных
            try:
                metadatas = search_results['metadatas'][0]
                if metadatas and 'stored_uuid' in metadatas[0]:
                    best_template_uuid = metadatas[0]['stored_uuid']
                    print(f"✅ RAG: Best template match -> {best_template_uuid}")
            except Exception as e:
                print(f"Error extracting metadata: {e}")

            # Модифицируем промт
            augmented_prompt = (
                "SYSTEM: Используй этот контекст стиля и содержания из базы знаний.\n"
                "CONTEXT:\n"
                f"{found_context}\n"
                "END CONTEXT\n\n"
                f"{user_prompt}"
            )
            data['prompt'] = augmented_prompt
        else:
            print("🔸 RAG: No relevant documents found.")

    print(f"Processing request -> {x_target_ollama_url}")
    
    # Добавляем заголовок с ID шаблона в ответ
    response_headers = {}
    if best_template_uuid:
        response_headers["X-Best-Template-ID"] = best_template_uuid

    return StreamingResponse(
        stream_completion(x_target_ollama_url, data), 
        media_type="text/event-stream",
        headers=response_headers 
    )

@router.post("/api/ingest")
async def ingest_document(file: UploadFile = File(...)):
    file_ext = file.filename.split(".")[-1].lower()
    unique_filename = f"{uuid.uuid4()}.{file_ext}"
    file_path = os.path.join(TEMP_DIR, unique_filename)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # КОНВЕРТАЦИЯ В DOCX (если это не docx)
    processing_path = file_path
    if file_ext != "docx":
        print(f"🔄 Converting {file_ext} to docx using LibreOffice...")
        try:
            # Используем headless libreoffice для конвертации
            # soffice --headless --convert-to docx filename.pdf --outdir /tmp/...
            subprocess.run([
                "soffice", "--headless", "--convert-to", "docx", 
                file_path, "--outdir", TEMP_DIR
            ], check=True)
            
            processing_path = file_path.replace(f".{file_ext}", ".docx")
            unique_filename = unique_filename.replace(f".{file_ext}", ".docx")
        except Exception as e:
            return JSONResponse({"error": f"Conversion failed: {e}"}, status_code=500)

    # Теперь скармливаем DOCX нашему новому RAG
    try:
        rag_engine.add_document(processing_path, unique_filename)
        return JSONResponse({"status": "indexed", "uuid": unique_filename})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@router.get("/api/download_template/{filename}")
async def download_template(filename: str):
    file_path = os.path.join(TEMP_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path, filename=filename)
    return JSONResponse({"error": "File not found"}, status_code=404)

@router.post("/api/retrieve_context")
def retrieve_context(request: ContextRequest):
    try:
        # Используем новый метод для получения полной картины стиля
        data = rag_engine.search_style_reference(request.text)
        if data:
            return {
                "context": data["full_context"],
                "source_id": data["source_id"]
            }
        return {"context": "No reference found.", "source_id": None}
    except Exception as e:
        return {"context": f"Error: {e}", "source_id": None}