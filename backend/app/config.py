import os
import psutil

class HardwareProfile:
    def __init__(self):
        vm = psutil.virtual_memory()
        self.available_ram_gb = vm.available / (1024 ** 3)
        self.physical_cores = psutil.cpu_count(logical=False) or 2

        # URL локальной Ollama — бэкенд всегда работает с ней напрямую
        self.OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

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