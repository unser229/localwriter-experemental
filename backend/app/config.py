import os
import psutil
import yaml
import shutil
from pathlib import Path
from pydantic import BaseModel

BACKEND_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BACKEND_DIR / "config.yaml"
EXAMPLE_CONFIG_PATH = BACKEND_DIR / "config.example.yaml"

class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8323

class OllamaConfig(BaseModel):
    base_url: str = "http://localhost:11434"

class TestConfig(BaseModel):
    excluded_models: list[str] = ["translategemma:12b", "translategemma:latest"]

class AppConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    ollama: OllamaConfig = OllamaConfig()
    test: TestConfig = TestConfig()

def load_app_config() -> AppConfig:
    if not CONFIG_PATH.exists():
        if EXAMPLE_CONFIG_PATH.exists():
            shutil.copy(EXAMPLE_CONFIG_PATH, CONFIG_PATH)
            print(f"📄 Создан базовый config.yaml из примера.")
        else:
            print("⚠️ Отсутствует config.example.yaml, будут использованы значения по умолчанию.")
    
    yaml_data = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f) or {}

    # Переопределяем параметры через переменные окружения, если они есть
    if "OLLAMA_BASE_URL" in os.environ:
        if "ollama" not in yaml_data:
            yaml_data["ollama"] = {}
        yaml_data["ollama"]["base_url"] = os.environ["OLLAMA_BASE_URL"]
    
    if "OLLAMA_URL" in os.environ:
        if "ollama" not in yaml_data:
            yaml_data["ollama"] = {}
        yaml_data["ollama"]["base_url"] = os.environ["OLLAMA_URL"]

    return AppConfig(**yaml_data)

app_config = load_app_config()

class HardwareProfile:
    def __init__(self):
        vm = psutil.virtual_memory()
        self.available_ram_gb = vm.available / (1024 ** 3)
        self.physical_cores = psutil.cpu_count(logical=False) or 2

        # Используем значение из загруженного и провалидированного AppConfig
        self.OLLAMA_BASE_URL = app_config.ollama.base_url
        self.SERVER_HOST = app_config.server.host
        self.SERVER_PORT = app_config.server.port

        # Начальная эвристика
        self.is_low_power = self.available_ram_gb < 8.0 or self.physical_cores < 6
        self.current_tps = 10.0 # Дефолтное значение (безопасное) до калибровки
        self._apply_settings()

    def update_from_benchmark(self, tokens_per_second: float):
        self.current_tps = tokens_per_second
        print(f"📊 BENCHMARK RESULT: {tokens_per_second:.2f} tokens/sec")
        
        if tokens_per_second < 15.0:
            print("🐢 LLM is responding slowly. Switching to LOW POWER mode.")
            self.is_low_power = True
        else:
            print("🚀 LLM is fast. Keeping/Switching to HIGH POWER mode.")
            self.is_low_power = False
            
        self._apply_settings()

    def _apply_settings(self):
        if self.is_low_power:
            self.OLLAMA_CTX = 4096
            self.RAG_CHUNK_LIMIT = 3
            self.MAX_INPUT_CHARS = 3500
        else:
            self.OLLAMA_CTX = 8192
            self.RAG_CHUNK_LIMIT = 10
            self.MAX_INPUT_CHARS = 12000

    def estimate_timeout(self, input_char_len: int) -> float:
        """
        Считает, сколько времени нужно модели, чтобы переварить текст.
        Эвристика: 1 токен ≈ 3-4 символа (для русского + код + json).
        """
        # Оценка количества входных токенов
        input_tokens = input_char_len / 3.0

        # Оценка выходных токенов: ~1 JSON-объект на каждые 200 символов входа,
        # каждый объект ~100 токенов. Минимум 512, максимум 8192.
        estimated_output_objects = max(1, input_char_len // 200)
        expected_output_tokens = min(8192, max(512, estimated_output_objects * 100))

        total_workload = input_tokens + expected_output_tokens

        # Время = Объем / Скорость
        # Если TPS не измерен (0), берем 5.0 как safe-mode
        speed = self.current_tps if self.current_tps > 0 else 5.0

        estimated_seconds = total_workload / speed

        # Добавляем 30% буфера + 15 секунд на сеть/лаги
        final_timeout = (estimated_seconds * 1.3) + 15.0

        # Не меньше 90 секунд
        return max(90.0, final_timeout)

settings = HardwareProfile()