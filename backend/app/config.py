import os
import psutil

class HardwareProfile:
    def __init__(self):
        vm = psutil.virtual_memory()
        self.available_ram_gb = vm.available / (1024 ** 3)
        self.physical_cores = psutil.cpu_count(logical=False) or 2
        
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
        
        # Оценка выходных токенов (JSON структуры обычно не гигантские, но берем с запасом)
        expected_output_tokens = 2048 
        
        total_workload = input_tokens + expected_output_tokens
        
        # Время = Объем / Скорость
        # Если TPS не измерен (0), берем 5.0 как safe-mode
        speed = self.current_tps if self.current_tps > 0 else 5.0
        
        estimated_seconds = total_workload / speed
        
        # Добавляем 20% буфера + 10 секунд на сеть/лаги
        final_timeout = (estimated_seconds * 1.2) + 10.0
        
        # Не меньше 60 секунд
        return max(60.0, final_timeout)

settings = HardwareProfile()