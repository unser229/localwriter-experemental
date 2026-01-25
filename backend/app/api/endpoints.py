import shutil
import os
import uuid
from fastapi import APIRouter, Request, Header, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse
from app.services.ollama_client import get_tags, stream_completion
from app.services.docling_parser import docling_service  # Импортируем наш новый сервис

router = APIRouter()

# --- TEMP FOLDER CONFIG ---
TEMP_DIR = os.path.join(os.getcwd(), "data", "temp")
os.makedirs(TEMP_DIR, exist_ok=True)

# ... (Тут остались старые методы proxy_tags и proxy_completions) ...
# ... (Оставь их как есть) ...

@router.get("/api/tags")
async def proxy_tags(x_target_ollama_url: str = Header(None, alias="X-Target-Ollama-Url")):
    if not x_target_ollama_url: x_target_ollama_url = "http://localhost:11434"
    data = await get_tags(x_target_ollama_url)
    return JSONResponse(content=data)

@router.post("/v1/completions")
async def proxy_completions(request: Request, x_target_ollama_url: str = Header(None, alias="X-Target-Ollama-Url")):
    if not x_target_ollama_url: x_target_ollama_url = "http://localhost:11434"
    data = await request.json()
    print(f"Processing request for model: {data.get('model')} -> {x_target_ollama_url}")
    return StreamingResponse(stream_completion(x_target_ollama_url, data), media_type="text/event-stream")

# === НОВЫЙ МЕТОД ДЛЯ ЗАГРУЗКИ ДОКУМЕНТОВ ===
@router.post("/api/ingest")
async def ingest_document(file: UploadFile = File(...)):
    """
    Принимает PDF/DOCX, сохраняет, парсит через Docling и возвращает структуру.
    В будущем здесь будет сохранение в Vector DB.
    """
    # Генерируем уникальное имя, чтобы файлы не перезатерлись
    file_ext = file.filename.split(".")[-1]
    filename = f"{uuid.uuid4()}.{file_ext}"
    file_path = os.path.join(TEMP_DIR, filename)
    
    try:
        # 1. Сохраняем файл на диск
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        print(f"File saved to {file_path}. Processing...")
        
        # 2. Запускаем Docling (это может занять время, при первом запуске скачает модели)
        # В реальном продакшене это лучше делать через BackgroundTasks, но пока делаем синхронно для простоты
        result = docling_service.process_file(file_path)
        
        # 3. (Тут будет код для RAG / Vector DB)
        
        return JSONResponse(content={
            "filename": file.filename,
            "processed_path": file_path,
            "result": result
        })
        
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
    finally:
        # Опционально: удалять файл сразу или оставлять для дебага
        # os.remove(file_path) 
        pass