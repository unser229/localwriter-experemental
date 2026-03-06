"""
Определение безопасного контекстного окна — ЧИСТЫЙ PYTHON, без NPX.

Почему убрали npx llm-checker:
  - У большинства пользователей (юристы, клерки) нет Node.js
  - Утилита всё равно падала в DEFAULT_CTX фоллбэк
  - Зависимость Node.js в Python-проекте — архитектурный запах

Как работает теперь:
  1. GET /api/show → model_info → парсим *.context_length (объявленный ctx)
  2. psutil.virtual_memory().available → поправка на RAM сервера
  3. НЕТ внешних процессов. НЕТ npm. Только httpx + psutil (уже в зависимостях).

Про CPT (chars_per_token):
  - НЕ кэшируется по (model, lang) для системного промпта!
  - Системный промпт ВСЕГДА на английском/JSON → SYSTEM_CPT = 4.0 (константа)
  - Пользовательский текст — детектируем по кириллице → отдельный кэш
"""

import re
import psutil
import httpx

# --------------------------------------------------------------------------
# Константы
# --------------------------------------------------------------------------
DEFAULT_CTX = 4096

# CPT для системного промпта: мы пишем его сами — всегда английский + JSON
# Английский: ~4 символа на токен (BPE/SentencePiece)
SYSTEM_CPT: float = 4.0

# CPT фоллбэки для пользовательского текста
_DEFAULT_CPT_RU = 1.8   # кириллица дорогая для токенайзера
_DEFAULT_CPT_EN = 4.0
_DEFAULT_CPT_MX = 3.0   # смешанный

# Сколько памяти (GB) нужно модели вне KV-кэша (веса + overhead)
# Эвристика: у нас уже загружена модель → смотрим только на доступную память
_CTX_RAM_TABLE = [
    (16.0, 4096),  # < 16 GB -> режем до 4k (fallback для слабых машин)
    (32.0, 8192),  # < 32 GB -> режем до 8k
]

# Жесткий буфер безопасности для системного промпта и RAG-эталонов
SAFETY_BUFFER = 2048

# Кэши (singleton модуля)
_ctx_cache: dict[str, tuple[int, bool]] = {}         # model -> (safe_context_for_user, is_degraded)
_cpt_cache: dict[tuple, float] = {}                  # (model, lang) -> chars_per_token


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------

def _detect_lang(sample: str) -> str:
    """Heuristic: если > 25% кириллицы — русский."""
    if not sample:
        return "other"
    cyrillic = sum(1 for c in sample if '\u0400' <= c <= '\u04FF')
    if cyrillic / len(sample) > 0.25:
        return "ru"
    latin = sum(1 for c in sample if 'a' <= c.lower() <= 'z')
    if latin / len(sample) > 0.3:
        return "en"
    return "other"


def _default_user_cpt(lang: str) -> float:
    return {"ru": _DEFAULT_CPT_RU, "en": _DEFAULT_CPT_EN}.get(lang, _DEFAULT_CPT_MX)


def _ram_cap(declared_ctx: int) -> tuple[int, bool]:
    """Ограничивает контекст по доступной RAM и возвращает флаг деградации."""
    available_gb = psutil.virtual_memory().available / (1024 ** 3)
    is_degraded = False
    
    cap = declared_ctx
    for threshold_gb, max_ctx in _CTX_RAM_TABLE:
        if available_gb < threshold_gb:
            cap = min(declared_ctx, max_ctx)
            # Если мы порезали контекст из-за памяти — включаем флаг деградации
            if cap < declared_ctx:
                is_degraded = True
            break
            
    return cap, is_degraded


def _parse_context_from_show(data: dict) -> int:
    """
    Парсит объявленный context_length из ответа /api/show.
    Ollama кладёт его в model_info под ключом '<family>.context_length'.
    Если не нашёл — пробуем modelfile (PARAMETER num_ctx).
    """
    # 1. model_info: ищем ключ вида "*.context_length"
    for key, val in data.get("model_info", {}).items():
        if key.endswith(".context_length") and isinstance(val, (int, float)):
            return int(val)

    # 2. modelfile: PARAMETER num_ctx XXXX
    modelfile = data.get("modelfile", "")
    m = re.search(r"PARAMETER\s+num_ctx\s+(\d+)", modelfile)
    if m:
        return int(m.group(1))

    return DEFAULT_CTX


# --------------------------------------------------------------------------
# Публичный API
# --------------------------------------------------------------------------

async def get_safe_context(model_name: str, ollama_url: str) -> tuple[int, bool]:
    """
    Получает безопасный num_ctx для модели.
    Возвращает (user_token_budget, is_degraded)
    """
    if model_name in _ctx_cache:
        return _ctx_cache[model_name]

    declared_ctx = DEFAULT_CTX
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{ollama_url.rstrip('/')}/api/show",
                json={"name": model_name},
                timeout=10,
            )
        resp.raise_for_status()
        declared_ctx = _parse_context_from_show(resp.json())
        print(f"🔍 /api/show: {model_name} → declared context = {declared_ctx} tokens")
    except Exception as e:
        declared_ctx = DEFAULT_CTX
        print(f"⚠️  /api/show ошибка ({e}). Фоллбэк: {DEFAULT_CTX}")

    safe_ctx, is_degraded = _ram_cap(declared_ctx)
    
    user_budget = max(safe_ctx - SAFETY_BUFFER, 1024) # Не меньше 1к токенов

    if safe_ctx != declared_ctx:
        print(
            f"📉 RAM-ограничение: {declared_ctx} → {safe_ctx} tokens "
            f"(доступно {psutil.virtual_memory().available/(1024**3):.1f} GB RAM), Degraded={is_degraded}"
        )

    _ctx_cache[model_name] = (user_budget, is_degraded)
    return user_budget, is_degraded


async def get_chars_per_token(model_name: str, sample: str, ollama_url: str) -> float:
    """
    Определяет CPT пользовательского текста через /api/tokenize.
    Ключ кэша: (model, lang) — чтобы ru и en не смешивались.
    ВАЖНО: этот CPT только для пользовательского текста.
           Для system_message используй константу SYSTEM_CPT = 4.0.
    """
    lang = _detect_lang(sample)
    cache_key = (model_name, lang)

    if cache_key in _cpt_cache:
        return _cpt_cache[cache_key]

    try:
        probe = sample[:500] if sample else "sample text образец"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{ollama_url.rstrip('/')}/api/tokenize",
                json={"model": model_name, "content": probe},
                timeout=10,
            )
        resp.raise_for_status()
        tokens = resp.json().get("tokens", [])
        if tokens:
            cpt = len(probe) / len(tokens)
            print(
                f"📏 chars/token [{lang}] для '{model_name}': {cpt:.2f} "
                f"({len(probe)} симв → {len(tokens)} tok)"
            )
        else:
            cpt = _default_user_cpt(lang)
            print(f"⚠️  /api/tokenize пустой список → фоллбэк [{lang}]: {cpt}")

    except Exception as e:
        cpt = _default_user_cpt(lang)
        print(f"⚠️  /api/tokenize недоступен ({type(e).__name__}) → фоллбэк [{lang}]: {cpt}")

    _cpt_cache[cache_key] = cpt
    return cpt
