import os
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# --- FIX IMPORT ---
# Пытаемся импортировать из app.api.endpoints (где он лежал раньше)
# Если не выйдет, пробуем из app.endpoints
try:
    from app.api.endpoints import router as api_router
except ImportError:
    from app.endpoints import router as api_router

# Импорт калибровки
from app.services.calibration import calibrate_ollama

app = FastAPI(title="LocalWriter Backend")

# CORS setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)

# --- STARTUP EVENT ---
@app.on_event("startup")
async def startup_event():
    # Запускаем калибровку при старте
    await calibrate_ollama()

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8323, reload=True)