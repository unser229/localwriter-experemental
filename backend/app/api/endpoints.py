import shutil
import os
import uuid
import json
from fastapi import APIRouter, Request, Header, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from app.services.ollama_client import get_tags, stream_completion
from app.services.docling_parser import docling_service
from app.services.rag_engine import rag_engine

router = APIRouter()

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
    
    # --- RAG LOGIC (ИСПРАВЛЕНО) ---
    
    # 1. Очищаем промт от системных инструкций, чтобы искать только по сути
    search_query = user_prompt
    
    # Расширение отправляет промт в формате: SYSTEM: ... USER: ... CONTENT/CONTEXT: ...
    # Нам нужно то, что в конце
    if "CONTENT/CONTEXT:" in user_prompt:
        search_query = user_prompt.split("CONTENT/CONTEXT:")[-1]
    elif "USER INSTRUCTION:" in user_prompt:
        search_query = user_prompt.split("USER INSTRUCTION:")[-1]
    elif "USER:" in user_prompt:
        search_query = user_prompt.split("USER:")[-1]
    
    search_query = search_query.strip()

    # 2. Ищем, если запрос осмысленный
    if len(search_query) > 10:
        # Обрезаем запрос до 1000 символов для поиска, чтобы не перегружать эмбеддинги
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

            # Модифицируем промт (добавляем найденный текст в начало)
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
    file_ext = file.filename.split(".")[-1]
    unique_filename = f"{uuid.uuid4()}.{file_ext}"
    file_path = os.path.join(TEMP_DIR, unique_filename)
    
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        print(f"📥 Received: {file.filename} -> {unique_filename}")
        result = docling_service.process_file(file_path)
        
        if result["status"] == "success":
            rag_engine.add_document(
                filename=file.filename, 
                markdown_text=result["markdown"],
                metadata={
                    "stored_uuid": unique_filename, # Сохраняем связь с файлом
                    "original_name": file.filename
                }
            )
            return JSONResponse(content={"status": "indexed", "uuid": unique_filename})
        else:
            return JSONResponse(content=result, status_code=500)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

@router.get("/api/download_template/{filename}")
async def download_template(filename: str):
    file_path = os.path.join(TEMP_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path, filename=filename)
    return JSONResponse({"error": "File not found"}, status_code=404)