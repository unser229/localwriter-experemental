"""
Тест качества форматирования: ВСЕ модели × ВСЕ файлы.

Вызовы к backend делаются через extension/client.py — тот же код, что выполняет
расширение LibreOffice при работе пользователя.

Процесс:
  1. [INGEST] загрузка всех docx в RAG-индекс (через /api/ingest)
  2. [GT]     параллельное извлечение ground truth (через /api/extract_ground_truth)
  3. [LLM]   последовательные вызовы /v1/completions для каждой пары (модель, файл)
  4. [EVAL]  сравнение ответа LLM с GT + проверка UNO-совместимости

Запуск:
  poetry run python tests/test_formatting_quality.py
  poetry run python tests/test_formatting_quality.py --server http://localhost:8323 --ollama http://192.168.0.107:11434
  poetry run python tests/test_formatting_quality.py --file one_specific.docx   # только один файл
  poetry run python tests/test_formatting_quality.py --model gemma3:12b         # только одна модель
"""

import sys
import os
import re
import json
import glob
import time
import argparse
import urllib.request
import urllib.error
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from difflib import SequenceMatcher
from collections import defaultdict
from typing import Any

try:
    from tqdm import tqdm
except ImportError:
    # Заглушка, если tqdm не установлен
    class tqdm:  # type: ignore
        def __init__(self, iterable=None, **kwargs):
            self._it = iter(iterable) if iterable is not None else iter([])
            total = kwargs.get("total", "?")
            desc = kwargs.get("desc", "")
            print(f"{desc} (всего: {total})")
        def __iter__(self): return self
        def __next__(self): return next(self._it)
        def update(self, n=1): pass
        def set_postfix_str(self, s): pass
        def set_postfix(self, **kw): pass
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

# --- Настройка путей ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_ROOT = os.path.dirname(CURRENT_DIR)
EXTENSION_DIR = os.path.join(os.path.dirname(BACKEND_ROOT), "extension")

# Добавляем extension/ в sys.path для импорта client.py
if EXTENSION_DIR not in sys.path:
    sys.path.insert(0, EXTENSION_DIR)
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

# Импорт клиента расширения — тот же код, что выполняет LibreOffice
from client import (
    call_apply_template_ndjson,
    call_ingest,
    call_extract_ground_truth,
    validate_uno_fields,
)

TEST_DOCS_DIR = os.path.join(CURRENT_DIR, "test_docs")
DATA_TEMP_DIR = os.path.join(BACKEND_ROOT, "data", "temp")
REPORTS_DIR = os.path.join(CURRENT_DIR, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)


# ============================================================================
# АВТО-ОБНАРУЖЕНИЕ
# ============================================================================

def discover_models(server_url: str) -> list[str]:
    """Получает список ВСЕХ доступных моделей через middleware."""
    url = f"{server_url.rstrip('/')}/api/tags"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "LocalWriter-Test"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            models = [m["name"] for m in data.get("models", [])]
            return models
    except Exception as e:
        print(f"❌ Не удалось получить модели: {e}")
        return []


def discover_docx_files(extra_dirs: list[str] | None = None) -> list[str]:
    """Находит все .docx файлы в известных директориях."""
    search_dirs = [TEST_DOCS_DIR]
    if extra_dirs:
        search_dirs.extend(extra_dirs)

    found = []
    seen_names = set()
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for path in glob.glob(os.path.join(d, "*.docx")):
            name = os.path.basename(path)
            if name not in seen_names and not name.startswith(".~lock"):
                seen_names.add(name)
                found.append(path)

    return sorted(found)


# ============================================================================
# GROUND TRUTH: динамическое извлечение ВСЕХ атрибутов из docx
# ============================================================================

# Убраны функции локального извлечения, теперь используем API бэкенда.


# ============================================================================
# ФАЗА 1: загрузка всех docx в RAG (ingest)
# ============================================================================

def ingest_documents(
    files: list[str],
    server_url: str,
    workers: int = 4,
) -> dict[str, str]:
    """
    Загружает все docx через POST /api/ingest (точно как делает расширение при первом запуске).
    Возвращает {path: uuid | error_message}.
    """
    results: dict[str, str] = {}
    lock = threading.Lock()

    def _ingest_one(path: str) -> tuple[str, str]:
        # Используем функцию из extension/client.py — тот же код, что выполняет расширение
        resp = call_ingest(path, server_url)
        if 'error' in resp:
            return path, f"ERROR: {resp['error']}"
        return path, resp.get('uuid', 'unknown')

    print(f"\n⎡ [INGEST] Индексирование {len(files)} файлов в RAG (в 1 поток — защита от блокировок SQLite)...")
    start = time.time()

    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = {pool.submit(_ingest_one, f): f for f in files}
        pbar = tqdm(total=len(files), desc="📂 Ingest", unit="файл", ncols=80)
        for future in as_completed(futures):
            path, uuid_or_err = future.result()
            with lock:
                results[path] = uuid_or_err
            fname = os.path.basename(path)
            ok = not uuid_or_err.startswith("ERROR")
            pbar.set_postfix_str(f"{'OK' if ok else 'ERR'} {fname}")
            pbar.update(1)
        pbar.close()

    elapsed = time.time() - start
    ok_count = sum(1 for v in results.values() if not v.startswith("ERROR"))
    print(f"  ⏱️  Ingest за {elapsed:.1f}s ({ok_count}/{len(files)} OK)")
    return results


# ============================================================================
# ФАЗА 2: извлечение ground truth через API
# ============================================================================

# ============================================================================
# СРАВНЕНИЕ: полностью динамическое
# ============================================================================

# Известные маппинги LLM ключей → GT ключей
_KNOWN_MAPPINGS = {
    "style_name": "style_name",
    "font_family": "tag_F",
    "font_size": "tag_P",
    "bold": "tag_B",
    "align": "tag_A",
    "type": "section_type",
}


def _normalize(val: Any) -> str:
    """
    Нормализует значение для сравнения.
    Покрывает случаи:
    - align: "CENTER (1)" / "center" / "1" -> "center"
    - font_size: "11" -> "11.0"
    - bool: "True"/"1" -> "true"
    - section_type: "body" -> "paragraph"
    """
    if val is None:
        return ""
    s = str(val).strip().lower()

    # Алиасы альянсования (docx число → слово)
    _ALIGN_MAP: dict[str, str] = {
        "0": "left", "left": "left",
        "1": "center", "center": "center",
        "2": "right", "right": "right",
        "3": "justify", "justify": "justify", "justified": "justify",
    }

    # Bool нормализация (до align, чтобы "true"/"false" не ушли в align-мап)
    if s in ("true", "yes"):
        return "true"
    if s in ("false", "no"):
        return "false"

    # Убираем суффикс "(N)" или "_N" для align: "CENTER (1)" -> "center"
    s_stripped = re.sub(r'[\s_]*\(\s*\d+\s*\)\s*$', '', s).strip()

    # Проверяем align-маппинг (ПОСЛЕ strip суффикса)
    if s_stripped in _ALIGN_MAP:
        return _ALIGN_MAP[s_stripped]
    if s in _ALIGN_MAP:
        return _ALIGN_MAP[s]

    # font_size: целое число -> флоат ("11" -> "11.0")
    try:
        f = float(s_stripped)
        if f == int(f):  # 11.0 == 11
            return f"{f:.1f}"
        return str(f)
    except ValueError:
        pass

    # section_type нормализация
    _type_map = {"header": "header", "body": "paragraph", "paragraph": "paragraph"}
    if s_stripped in _type_map:
        return _type_map[s_stripped]

    return s_stripped if s_stripped else s


def build_key_map(gt: list[dict], llm: list[dict]) -> dict[str, str]:
    """Автоматически строит маппинг LLM keys → GT keys."""
    gt_keys = {k for r in gt for k in r.keys()} - {"text"}
    llm_keys = {k for r in llm for k in r.keys()} - {"text"}

    mapping = {}
    # Известные
    for lk, gk in _KNOWN_MAPPINGS.items():
        if lk in llm_keys and gk in gt_keys:
            mapping[lk] = gk
    # Автоматические
    for lk in llm_keys:
        if lk in mapping:
            continue
        if lk in gt_keys:
            mapping[lk] = lk
        elif f"tag_{lk}" in gt_keys:
            mapping[lk] = f"tag_{lk}"
    return mapping


def fuzzy_match(gt_text: str, llm_text: str) -> float:
    """Fuzzy matching двух текстов (0..1)."""
    if not gt_text or not llm_text:
        return 0.0
    a = re.sub(r'\s+', ' ', gt_text.strip().lower())[:200]
    b = re.sub(r'\s+', ' ', llm_text.strip().lower())[:200]
    return SequenceMatcher(None, a, b).ratio()


def evaluate(
    gt_records: list[dict],
    llm_records: list[dict], # Это список словарей {"id": N, "style_name": "..."} полученных из NDJSON
) -> dict:
    """
    Полная оценка: строгое совпадение по ID.
    Template-mode и fuzzy_match удалены по правилу Zero-Mock.
    """
    # Преобразуем список словарей LLM в удобный маппинг по ID
    llm_map = {}
    for record in llm_records:
        pid = record.get("id")
        if pid is not None:
            llm_map[int(pid)] = record

    # Определяем key_map динамически как раньше
    # (Хотя теперь LLM возвращает в основном только style_name, оставим для универсальности)
    key_map = build_key_map(gt_records, [r for r in llm_records if isinstance(r, dict)])

    matched = []
    # GT records идут в строгом порядке, их индексы = ID (0..N-1)
    for i, gt in enumerate(gt_records):
        if i in llm_map:
            matched.append({"gt": gt, "llm": llm_map[i]})

    # Метрики покрытия
    result: dict[str, Any] = {
        "gt_count": len(gt_records),
        "llm_count": len(llm_records),
        "matched_count": len(matched),
        "text_coverage_pct": round(len(matched) / len(gt_records) * 100, 1) if gt_records else 0,
        "avg_text_similarity": 100.0 if matched else 0.0, # Текст больше не сверяем, он 100% совпадает по ID
        "key_map": key_map,
        "attributes": {},
    }

    # Динамические метрики по каждому атрибуту
    for llm_key, gt_key in key_map.items():
        total, correct, examples = 0, 0, []
        for pair in matched:
            gt_val = pair["gt"].get(gt_key)
            if gt_val is None:
                continue
            llm_val = pair["llm"].get(llm_key)
            total += 1
            if _normalize(gt_val) == _normalize(llm_val):
                correct += 1
            else:
                if len(examples) < 3:
                    examples.append({
                        "text": str(pair["gt"].get("text", ""))[:40],
                        "expected": str(gt_val),
                        "got": str(llm_val),
                    })
        acc = round(correct / total * 100, 1) if total else 0
        result["attributes"][llm_key] = {
            "accuracy": acc,
            "correct": correct,
            "total": total,
            "examples": examples,
        }

    # Overall (среднее по всем атрибутам)
    accs = [v["accuracy"] for v in result["attributes"].values() if v["total"] > 0]
    result["overall_score"] = round(sum(accs) / len(accs), 1) if accs else 0

    return result




def precompute_all_gt(
    files: list[str],
    server_url: str,
    workers: int = 4,
) -> dict[str, dict]:
    """
    Параллельно извлекает ground truth и текст из ВСЕХ файлов через API бэкенда.
    Сервер сам делает батчинг — текст передаётся целиком, как расширение в LibreOffice.
    Возвращает {path: {"gt": [...], "text": "...", "error": None}}
    """
    cache: dict[str, dict] = {}
    lock = threading.Lock()

    def _process_file(path: str) -> tuple[str, dict]:
        # Используем функцию из extension/client.py — без httpx, без усечения
        data = call_extract_ground_truth(path, server_url)
        if 'error' in data:
            return path, {"gt": [], "text": "", "error": data['error']}
        return path, {"gt": data.get("ground_truth", []),
                      "text": data.get("plain_text", ""),
                      "error": None}

    print(f"\n⚡ [GT] Парсинг GT для {len(files)} файлов ({workers} потоков)...")
    start = time.time()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_process_file, f): f for f in files}
        pbar = tqdm(total=len(files), desc="📄 GT-парсинг", unit="файл", ncols=80)
        for future in as_completed(futures):
            path, result = future.result()
            with lock:
                cache[path] = result
            fname = os.path.basename(path)
            if result["error"]:
                pbar.set_postfix_str(f"❌ {fname}: {result['error'][:30]}")
            else:
                pbar.set_postfix_str(f"✅ {fname} ({len(result['gt'])} эл.)")
            pbar.update(1)
        pbar.close()

    elapsed = time.time() - start
    ok_count = sum(1 for v in cache.values() if not v["error"])
    print(f"  ⏱️  GT готов за {elapsed:.1f}s ({ok_count}/{len(files)} файлов ОК)")

    return cache



def _run_single(
    model: str,
    file_path: str,
    gt_cache: dict[str, dict],
    server_url: str,
    timeout: int,
) -> dict:
    """
    Один прогон: модель × файл.
    Вызывает extension/client.call_apply_template() — точно как расширение LibreOffice.
    GT берётся из кэша (уже предвычислен).
    """
    fname = os.path.basename(file_path)
    cached = gt_cache.get(file_path, {})

    if cached.get("error"):
        return _error_result(model, fname, f"GT error: {cached['error']}")

    gt = cached.get("gt", [])
    text = cached.get("text", "")

    if not gt:
        return _error_result(model, fname, "Empty GT")

    # В новой архитектуре мы передаем список текстов параграфов, чтобы ID совпадали 1к1 с GT
    paragraphs_texts = [r.get("text", "") for r in gt]

    import queue
    import threading
    
    result_queue = queue.Queue()
    stop_event = threading.Event()
    
    start_time = time.time()

    # Вызов /v1/completions через NDJSON-бачтер (гибридный клиент)
    try:
        is_degraded, rag_template_id = call_apply_template_ndjson(
            content=paragraphs_texts,
            model=model,
            middleware_url=server_url,
            result_queue=result_queue,
            stop_event=stop_event,
            timeout_per_line=timeout,
        )
    except Exception as e:
        return _error_result(model, fname, f"API Exception: {e}", time.time() - start_time)

    llm_records = []
    has_error = False
    error_msg = ""
    
    while True:
        try:
            # Даем таймаут на 1 батч
            item = result_queue.get(timeout=60)
            if "DONE" in item:
                break
            if "error" in item:
                has_error = True
                error_msg = item["error"]
                break
            
            llm_records.append(item)
            
        except queue.Empty:
            has_error = True
            error_msg = "Timeout waiting for backend queue"
            stop_event.set()
            break

    elapsed = time.time() - start_time

    if has_error:
        return _error_result(model, fname, f"Stream Error: {error_msg}", elapsed)

    if not llm_records:
        return _error_result(model, fname, "No JSON generated", elapsed)

    # Оценка качества форматирования
    metrics = evaluate(gt, llm_records)
    metrics["model"] = model
    metrics["file"] = fname
    metrics["elapsed_sec"] = round(elapsed, 1)
    metrics["status"] = "OK"
    metrics["rag_found"] = bool(rag_template_id)
    metrics["rag_template_id"] = rag_template_id or ""

    # Проверка UNO-совместимости (поля, которые ожидает uno_formatter.apply_structure)
    uno_info = validate_uno_fields(llm_records)
    metrics["uno_compat_pct"] = uno_info["compat_pct"]
    metrics["uno_with_style_pct"] = round(
        uno_info["with_style"] / uno_info["total"] * 100, 1
    ) if uno_info["total"] else 0.0

    return metrics



def _append_model_section(
    report_path: str,
    model_results: list[dict],
    model_name: str,
) -> None:
    """
    Дописывает в Markdown-отчёт секцию с результатами для одной модели.
    """
    lines = [f"## 🤖 Модель: `{model_name}`", ""]
    lines.append("| Файл | Status | RAG | Coverage | Score | UNO% | Time |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in model_results:
        icon = "✅" if r["status"] == "OK" else "❌"
        rag_icon = "✅" if r.get("rag_found") else "✖️"
        lines.append(
            f"| `{r['file']}` | {icon} {r['status']} | {rag_icon} | "
            f"{r.get('text_coverage_pct', 0):.1f}% | "
            f"{r.get('overall_score', 0):.1f}% | "
            f"{r.get('uno_compat_pct', 0):.0f}% | "
            f"{r.get('elapsed_sec', 0):.1f}s |"
        )
    lines.append("")
    with open(report_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")



def run_contest(
    models: list[str],
    files: list[str],
    server_url: str,
    timeout: int,
    workers: int = 4,
    report_path: str | None = None,
) -> list[dict]:
    """
    Прогон контеста в 3 фазы:
      1. [INGEST] загрузка всех docx в RAG-индекс (1 поток — защита от блокировок SQLite ChromaDB)
      2. [GT]     параллельное извлечение ground truth (/api/extract_ground_truth)
      3. [LLM]   последовательные вызовы /v1/completions
    Все HTTP-вызовы через extension/client.py.
    """
    # Фаза 1: INGEST (наполняем RAG-индекс)
    ingest_documents(files, server_url, workers=workers)

    # Фаза 2: параллельное извлечение GT
    gt_cache = precompute_all_gt(files, server_url, workers=workers)

    # Фаза 3: ПОСЛЕДОВАТЕЛЬНЫЕ LLM вызовы
    total_runs = len(models) * len(files)
    results: list[dict] = []
    start_time = time.time()

    print(f"\n🚀 [LLM] Запуск контеста: {total_runs} прогонов (последовательно)")
    if report_path:
        print(f"📝 Real-time отчёт: {report_path}")

    pbar = tqdm(
        total=total_runs,
        desc="🏁 Контест",
        unit="прогон",
        ncols=90,
        colour="green",
    )

    for model in models:
        pbar.set_postfix_str(f"🤖 {model}")
        model_results: list[dict] = []

        for file_path in files:
            fname = os.path.basename(file_path)
            pbar.set_postfix_str(f"📄 {fname[:25]} × {model}")

            result = _run_single(
                model, file_path, gt_cache,
                server_url, timeout,
            )
            results.append(result)
            model_results.append(result)
            pbar.update(1)

            status = result.get("status", "?")
            if status == "OK":
                cov = result.get('text_coverage_pct', 0)
                score = result.get('overall_score', 0)
                uno = result.get('uno_compat_pct', 0)
                rag = "✅" if result.get('rag_found') else "✖️"
                tqdm.write(
                    f"  ✅ {model} × {fname} | "
                    f"RAG={rag} Cov={cov:.1f}% Score={score:.1f}% UNO={uno:.0f}%"
                )
            else:
                tqdm.write(f"  ❌ {model} × {fname} | {status}")

        if report_path:
            _append_model_section(report_path, model_results, model)
            tqdm.write(f"  📝 Записано в отчёт: {model}")

    pbar.close()
    total_elapsed = time.time() - start_time
    print(f"\n⏱️  Контест завершён за {total_elapsed/60:.1f} минут")

    return results



def _error_result(model: str, fname: str, error: str, elapsed: float = 0) -> dict:
    return {
        "model": model,
        "file": fname,
        "status": f"FAIL: {error}",
        "gt_count": 0,
        "llm_count": 0,
        "matched_count": 0,
        "text_coverage_pct": 0,
        "avg_text_similarity": 0,
        "overall_score": 0,
        "elapsed_sec": round(elapsed, 1),
        "rag_found": False,
        "rag_template_id": "",
        "uno_compat_pct": 0.0,
        "uno_with_style_pct": 0.0,
        "attributes": {},
        "key_map": {},
    }


# ============================================================================
# ОТЧЁТ: Leaderboard + детали
# ============================================================================

# ============================================================================
# ИНФОГРАФИКА (ASCII bar charts)
# ============================================================================

def _bar(value: float, max_val: float = 100.0, width: int = 20, fill: str = "█", empty: str = "░") -> str:
    """Рисует ASCII прогресс-бар для значения 0..max_val."""
    if max_val <= 0:
        max_val = 100.0
    filled = int(round(value / max_val * width))
    filled = max(0, min(width, filled))
    return fill * filled + empty * (width - filled)


def _infographic_summary(leaderboard: list[dict], results: list[dict]) -> list[str]:
    """
    Строит текстовую инфографику:
      - Bar chart Coverage и Score для каждой модели
      - Топ-3 модели
      - Статистика по файлам
    """
    lines: list[str] = []
    lines.append("")
    lines.append("## 📊 Инфографика")
    lines.append("")

    # --- Bar chart моделей ---
    lines.append("### Сравнение моделей (Coverage / Score)")
    lines.append("")
    lines.append("```")
    lines.append(f"{'Модель':<32} {'Coverage':>10}  {'Score':>8}")
    lines.append("-" * 70)
    for lb in leaderboard:
        name = lb["model"][:31]
        cov = lb["avg_coverage"]
        score = lb["avg_overall"]
        bar_cov = _bar(cov, width=15)
        bar_score = _bar(score, width=10)
        lines.append(
            f"{name:<32} {bar_cov} {cov:5.1f}%  {bar_score} {score:5.1f}%"
        )
    lines.append("```")
    lines.append("")

    # --- Топ-3 ---
    lines.append("### 🏆 Топ-3 модели")
    lines.append("")
    medals = ["🥇", "🥈", "🥉"]
    for i, lb in enumerate(leaderboard[:3]):
        lines.append(
            f"{medals[i]} **{lb['model']}** — "
            f"Coverage: `{lb['avg_coverage']:.1f}%`, "
            f"Score: `{lb['avg_overall']:.1f}%`, "
            f"Файлов ОК: `{lb['files_ok']}/{lb['files_tested']}`"
        )
    lines.append("")

    # --- Статистика по файлам ---
    files_in_results = sorted(set(r["file"] for r in results))
    lines.append("### 📄 Статистика по файлам")
    lines.append("")
    lines.append("```")
    lines.append(f"{'Файл':<35} {'OK':>4} {'FAIL':>5} {'Avg Score':>10}")
    lines.append("-" * 60)
    for fname in files_in_results:
        file_runs = [r for r in results if r["file"] == fname]
        ok_runs = [r for r in file_runs if r["status"] == "OK"]
        fail_count = len(file_runs) - len(ok_runs)
        avg_score = (
            sum(r["overall_score"] for r in ok_runs) / len(ok_runs)
            if ok_runs else 0
        )
        bar = _bar(avg_score, width=12)
        lines.append(
            f"{fname[:34]:<35} {len(ok_runs):>4} {fail_count:>5}  {bar} {avg_score:5.1f}%"
        )
    lines.append("```")
    lines.append("")

    # --- Общая статистика ---
    total = len(results)
    ok_total = sum(1 for r in results if r["status"] == "OK")
    fail_total = total - ok_total
    lines.append("### ℹ️ Общая статистика")
    lines.append("")
    lines.append(f"- **Всего прогонов:** {total}")
    lines.append(f"- **Успешно:** {ok_total} ({ok_total/total*100:.1f}%)")
    lines.append(f"- **Ошибок:** {fail_total} ({fail_total/total*100:.1f}%)")
    if ok_total:
        avg_cov_all = sum(r["text_coverage_pct"] for r in results if r["status"] == "OK") / ok_total
        avg_score_all = sum(r["overall_score"] for r in results if r["status"] == "OK") / ok_total
        lines.append(f"- **Средняя Coverage:** {avg_cov_all:.1f}%")
        lines.append(f"- **Средний Score:** {avg_score_all:.1f}%")
    lines.append("")

    return lines


def save_contest_report(
    results: list[dict],
    output_dir: str,
    realtime_path: str | None = None,
) -> str:
    """
    Генерирует итоговый Markdown leaderboard, инфографику и JSON дамп.
    Если realtime_path указан — добавляет сводку поверх уже записанного файла.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = realtime_path or os.path.join(output_dir, f"contest_{ts}.md")
    json_path = os.path.join(output_dir, f"contest_{ts}.json")

    # --- Leaderboard ---
    # Группировка по модели: среднее по всем файлам
    model_scores: dict[str, list] = defaultdict(list)
    for r in results:
        model_scores[r["model"]].append(r)

    leaderboard = []
    for model, runs in model_scores.items():
        ok_runs = [r for r in runs if r["status"] == "OK"]
        fail_count = len(runs) - len(ok_runs)
        avg_coverage = (
            sum(r["text_coverage_pct"] for r in ok_runs) / len(ok_runs)
            if ok_runs else 0
        )
        avg_overall = (
            sum(r["overall_score"] for r in ok_runs) / len(ok_runs)
            if ok_runs else 0
        )
        avg_elements = (
            sum(r["llm_count"] for r in ok_runs) / len(ok_runs)
            if ok_runs else 0
        )
        avg_time = (
            sum(r["elapsed_sec"] for r in ok_runs) / len(ok_runs)
            if ok_runs else 0
        )

        leaderboard.append({
            "model": model,
            "files_tested": len(runs),
            "files_ok": len(ok_runs),
            "files_failed": fail_count,
            "avg_coverage": round(avg_coverage, 1),
            "avg_overall": round(avg_overall, 1),
            "avg_elements": round(avg_elements, 1),
            "avg_time_sec": round(avg_time, 1),
        })

    # Сортировка: лучшие сверху (coverage * overall)
    leaderboard.sort(key=lambda x: (x["avg_coverage"] * x["avg_overall"]), reverse=True)

    # --- Шапка Markdown ---
    header_lines = [
        f"# 🏆 Formatting Contest Report",
        f"**Дата:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**Моделей:** {len(model_scores)} | "
        f"**Файлов:** {len(set(r['file'] for r in results))} | "
        f"**Прогонов:** {len(results)}",
        "",
        "## Leaderboard",
        "",
        "| # | Модель | Файлов ✅/❌ | Avg Coverage | Avg Score | Avg Elements | Avg Time |",
        "|---|---|---|---|---|---|---|",
    ]

    for i, lb in enumerate(leaderboard, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        header_lines.append(
            f"| {medal} | `{lb['model']}` | {lb['files_ok']}/{lb['files_failed']} | "
            f"{lb['avg_coverage']:.1f}% | {lb['avg_overall']:.1f}% | "
            f"{lb['avg_elements']:.0f} | {lb['avg_time_sec']:.1f}s |"
        )

    # --- Инфографика (сводка) ---
    infographic_lines = _infographic_summary(leaderboard, results)

    # --- Разделитель перед детальными прогонами ---
    detail_lines = ["", "## Детали по прогонам", ""]

    for r in results:
        status_icon = "✅" if r["status"] == "OK" else "❌"
        detail_lines.append(f"### {status_icon} `{r['model']}` × `{r['file']}`")

        if r["status"] != "OK":
            detail_lines.append(f"**Статус:** {r['status']}")
            detail_lines.append("")
            continue

        detail_lines.append(
            f"Coverage: {r['text_coverage_pct']}% | "
            f"Score: {r['overall_score']}% | "
            f"Elements: {r['llm_count']}/{r['gt_count']} | "
            f"Time: {r['elapsed_sec']}s"
        )

        if r.get("attributes"):
            detail_lines.append("")
            detail_lines.append("| Атрибут | Accuracy | Correct/Total |")
            detail_lines.append("|---|---|---|")
            for attr, info in sorted(r["attributes"].items(), key=lambda x: x[1]["accuracy"]):
                icon = "✅" if info["accuracy"] >= 80 else "⚠️" if info["accuracy"] >= 50 else "❌"
                detail_lines.append(
                    f"| {icon} `{attr}` | {info['accuracy']:.1f}% | {info['correct']}/{info['total']} |"
                )

            # Примеры ошибок (компактно)
            has_examples = any(info["examples"] for info in r["attributes"].values())
            if has_examples:
                detail_lines.append("")
                detail_lines.append("<details><summary>Примеры ошибок</summary>")
                detail_lines.append("")
                for attr, info in r["attributes"].items():
                    for ex in info["examples"]:
                        text_snippet = ex.get('text', '')
                        prefix = f"`{text_snippet}...` — " if text_snippet else ""
                        detail_lines.append(
                            f"- **{attr}**: {prefix}ожидалось `{ex['expected']}`, получено `{ex['got']}`"
                        )
                detail_lines.append("")
                detail_lines.append("</details>")

        detail_lines.append("")

    # --- Запись финального Markdown ---
    # Если realtime_path задан — перезаписываем (добавляем шапку + инфографику перед накопленными секциями)
    if realtime_path and os.path.exists(realtime_path):
        # Читаем уже накопленный real-time контент (секции файлов)
        with open(realtime_path, "r", encoding="utf-8") as f:
            realtime_content = f.read()
        # Итоговый документ: шапка → инфографика → real-time секции → детали
        full_content = (
            "\n".join(header_lines + infographic_lines)
            + "\n## Прогоны по файлам (real-time)\n\n"
            + realtime_content
            + "\n"
            + "\n".join(detail_lines)
        )
    else:
        full_content = "\n".join(header_lines + infographic_lines + detail_lines)

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(full_content)

    # --- JSON (без examples для компактности) ---
    json_data = {
        "timestamp": datetime.now().isoformat(),
        "leaderboard": leaderboard,
        "runs": [
            {k: v for k, v in r.items() if k not in ("key_map", "llm_records", "gt_records")}
            for r in results
        ],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)

    print(f"\n📊 Отчёт: {md_path}")
    print(f"📦 JSON:  {json_path}")

    # --- Консольный leaderboard с инфографикой ---
    W = 70
    print(f"\n{'='*W}")
    print("🏆 LEADERBOARD")
    print(f"{'='*W}")
    print(f"  {'Модель':<30} {'Coverage':>10}  {'Score':>8}")
    print(f"  {'-'*60}")
    for i, lb in enumerate(leaderboard, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"  {i}."
        bar_cov = _bar(lb['avg_coverage'], width=12)
        bar_score = _bar(lb['avg_overall'], width=8)
        print(
            f"  {medal} {lb['model']:<28} "
            f"{bar_cov} {lb['avg_coverage']:5.1f}%  "
            f"{bar_score} {lb['avg_overall']:5.1f}%"
        )
    print(f"{'='*W}")

    return md_path


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Formatting Quality Contest")
    parser.add_argument("--server", "-s", default="http://localhost:8323",
                        help="URL middleware сервера")
    parser.add_argument("--model", "-m", default=None,
                        help="Конкретная модель (по умолчанию — все)")
    parser.add_argument("--file", "-f", default=None,
                        help="Конкретный .docx файл (по умолчанию — все из test_docs/)")
    parser.add_argument("--timeout", "-t", type=int, default=300,
                        help="Таймаут на один LLM вызов (секунды)")
    parser.add_argument("--max-files", type=int, default=0,
                        help="Лимит файлов (0 = без лимита)")
    parser.add_argument("--workers", "-w", type=int, default=4,
                        help="Количество параллельных потоков (ingest/GT)")
    args = parser.parse_args()

    print("🏁 FORMATTING QUALITY CONTEST")
    print(f"   Server: {args.server}")

    config_path = os.path.join(os.path.dirname(__file__), "test_config.json")
    excluded_models = ["translategemma:12b", "translategemma:latest"]
    try:
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                excluded_models = cfg.get("excluded_models", excluded_models)
                args.server = cfg.get("server_url", args.server)
                print(f"   🔧 Config loaded (excluded: {len(excluded_models)})")
    except Exception:
        pass

    # --- Модели ---
    if args.model:
        models = [args.model]
    else:
        models = discover_models(args.server)
        if excluded_models:
            models = [m for m in models if not any(ex in m for ex in excluded_models)]
        if not models:
            print("❌ Моделей не найдено (или все исключены)")
            sys.exit(1)

    print(f"\n🤖 Модели ({len(models)}):")
    for m in models:
        print(f"   - {m}")

    # --- Файлы ---
    if args.file:
        if os.path.exists(args.file):
            files = [args.file]
        else:
            # Ищем в test_docs
            candidate = os.path.join(TEST_DOCS_DIR, args.file)
            if os.path.exists(candidate):
                files = [candidate]
            else:
                print(f"❌ Файл не найден: {args.file}")
                sys.exit(1)
    else:
        files = discover_docx_files([DATA_TEMP_DIR])
        if not files:
            print(f"❌ Файлы .docx не найдены в {TEST_DOCS_DIR}")
            sys.exit(1)

    if args.max_files > 0:
        files = files[:args.max_files]

    print(f"\n📄 Файлы ({len(files)}):")
    for f in files:
        print(f"   - {os.path.basename(f)}")

    print(f"\n📐 Всего прогонов: {len(models)} × {len(files)} = {len(models) * len(files)}")
    print(f"   Timeout: {args.timeout}s | Workers: {args.workers}")
    print(f"   Extension client: {EXTENSION_DIR}/client.py")

    # --- Путь для real-time записи ---
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    realtime_report_path = os.path.join(REPORTS_DIR, f"contest_{ts}.md")
    # Инициализируем файл (пустой, будем дописывать по ходу)
    with open(realtime_report_path, "w", encoding="utf-8") as f:
        f.write(f"<!-- Отчёт создан: {datetime.now().isoformat()} — будет дополняться -->\n\n")

    # --- Контест ---
    results = run_contest(
        models=models,
        files=files,
        server_url=args.server,
        timeout=args.timeout,
        workers=args.workers,
        report_path=realtime_report_path,
    )

    # --- Итоговый отчёт (перезапишет файл, добавив шапку + инфографику) ---
    if results:
        save_contest_report(results, REPORTS_DIR, realtime_path=realtime_report_path)
    else:
        print("❌ Нет результатов — выходим с ошибкой")
        sys.exit(1)

    # --- Объективная проверка качества (защита от False Positive) ---
    # Считаем только прогоны со статусом OK (не FAIL / Connection refused)
    ok_results = [r for r in results if r.get("status") == "OK"]
    fail_results = [r for r in results if r.get("status") != "OK"]

    if fail_results:
        print(f"\n⚠️  Провальных прогонов: {len(fail_results)}/{len(results)}")
        for r in fail_results[:5]:  # показываем первые 5
            print(f"   ❌ {r['model']} × {r['file']} — {r['status']}")

    # Если вообще нет успешных прогонов — критичная ошибка (сервер недоступен, GT подал 0 файлов и т.д.)
    if not ok_results:
        print(
            "\n❌ КРИТИЧНО: Все прогоны завершились неуспешно (FAIL). "
            "Убедитесь, что сервер запущен и доступен."
        )
        sys.exit(1)

    # Если средний Score по OK-прогонам == 0% — LLM не вернула ничего осмысленного
    avg_score = sum(r.get("overall_score", 0) for r in ok_results) / len(ok_results)
    avg_coverage = sum(r.get("text_coverage_pct", 0) for r in ok_results) / len(ok_results)

    print(f"\n📊 Итог: avg_score={avg_score:.1f}% | avg_coverage={avg_coverage:.1f}% "
          f"| ok={len(ok_results)} | fail={len(fail_results)}")

    if avg_score == 0.0 and avg_coverage == 0.0:
        print(
            "\n❌ КРИТИЧНО: Средний Score=0% и Coverage=0% по всем успешным прогонам. "
            "LLM не вернула осмысленного ответа."
        )
        sys.exit(1)

    print("\n✅ E2E тест пройден объективно.")


if __name__ == "__main__":
    main()
