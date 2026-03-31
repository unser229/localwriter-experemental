import os
import asyncio
from contextlib import asynccontextmanager
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

try:
    from app.api.endpoints import router as api_router
except ImportError:
    from app.endpoints import router as api_router

# Импорт сервисов запуска
from app.services.calibration import calibrate_ollama
from app.services.startup_sync import sync_documents_on_startup


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Жизненный цикл приложения (современная замена @app.on_event("startup")).
    Оба сервиса запускаются как фоновые задачи — сервер готов принимать
    запросы немедленно, не дожидаясь завершения синхронизации и калибровки.
    """
    asyncio.create_task(calibrate_ollama())
    asyncio.create_task(sync_documents_on_startup())
    yield
    # shutdown hook (при необходимости — добавить логику здесь)


app = FastAPI(title="LocalWriter Backend", lifespan=lifespan)

# CORS setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)

if __name__ == "__main__":
    from app.config import app_config
    uvicorn.run("app.main:app", host=app_config.server.host, port=app_config.server.port, reload=True)