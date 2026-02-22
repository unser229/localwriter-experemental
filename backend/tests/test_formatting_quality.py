"""
Контест качества форматирования: ВСЕ модели × ВСЕ файлы.

Автоматически:
  - Находит все модели с Ollama сервера
  - Находит все .docx файлы в test_docs/ и в RAG (data/temp/)
  - Прогоняет каждую пару (модель, файл) через /v1/completions
  - Сравнивает LLM ответ с ground truth (стили из docx)
  - Генерирует leaderboard и детальный отчёт

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

# --- Настройка путей ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_ROOT = os.path.dirname(CURRENT_DIR)
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

try:
    from app.services.style_extractor import style_extractor
except ImportError as e:
    print(f"❌ Ошибка импорта: {e}")
    sys.exit(1)

TEST_DOCS_DIR = os.path.join(CURRENT_DIR, "test_docs")
DATA_TEMP_DIR = os.path.join(BACKEND_ROOT, "data", "temp")
REPORTS_DIR = os.path.join(CURRENT_DIR, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)


# ============================================================================
# АВТО-ОБНАРУЖЕНИЕ
# ============================================================================

def discover_models(server_url: str, ollama_url: str) -> list[str]:
    """Получает список ВСЕХ доступных моделей с Ollama через middleware."""
    url = f"{server_url.rstrip('/')}/api/tags"
    headers = {"X-Target-Ollama-Url": ollama_url}
    try:
        req = urllib.request.Request(url, headers=headers)
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

def extract_ground_truth(docx_path: str) -> list[dict]:
    """
    Парсит docx через style_extractor.
    Все атрибуты извлекаются ДИНАМИЧЕСКИ из тегов [KEY: VALUE].
    """
    chunks = style_extractor.parse_docx(docx_path)
    records = []

    for chunk in chunks:
        text = chunk.get("text", "").strip()
        if not text or text == "<IMAGE_PLACEHOLDER>":
            continue

        meta = chunk.get("metadata", {})
        style_desc = chunk.get("style_desc", "")

        record: dict[str, Any] = {
            "text": text,
            "style_name": meta.get("style_name", "Normal"),
            "is_header": meta.get("is_header", False),
            "section_type": meta.get("section_type", "body"),
        }

        # Динамическое извлечение ВСЕХ тегов
        for m in re.finditer(r'\[([^:]+):\s*([^\]]+)\]', style_desc):
            record[f"tag_{m.group(1).strip()}"] = m.group(2).strip()

        records.append(record)

    return records


def extract_plain_text(docx_path: str, max_chars: int) -> str:
    """Извлекает plain text из docx, обрезая при необходимости."""
    chunks = style_extractor.parse_docx(docx_path)
    lines = [c["text"].strip() for c in chunks
             if c.get("text", "").strip() and c["text"] != "<IMAGE_PLACEHOLDER>"]
    text = "\n\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... [TRUNCATED]"
    return text


# ============================================================================
# LLM ВЫЗОВ
# ============================================================================

def call_llm(
    text: str,
    model: str,
    server_url: str,
    ollama_url: str,
    timeout: int = 300,
) -> tuple[list[dict] | None, float]:
    """
    Отправляет текст на /v1/completions.
    Возвращает (parsed_list | None, elapsed_seconds).
    """
    url = f"{server_url.rstrip('/')}/v1/completions"
    payload = {
        "model": model,
        "prompt": f"=== USER CONTENT (CONTENT SOURCE) ===\n{text}",
        "stream": False,
        "format": "json",
        "options": {"num_ctx": 8192},
    }
    headers = {
        "Content-Type": "application/json",
        "X-Target-Ollama-Url": ollama_url,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )

    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return None, time.time() - start

    elapsed = time.time() - start
    raw = data.get("response", "")

    # Парсинг JSON
    parsed = _try_parse_json_list(raw)
    return parsed, elapsed


def _try_parse_json_list(raw: str) -> list[dict] | None:
    """Пытается извлечь JSON list/dict из сырого текста."""
    if not raw:
        return None

    # 1. Прямой парсинг
    try:
        p = json.loads(raw)
        if isinstance(p, list):
            return p
        if isinstance(p, dict):
            return [p]
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. Поиск массива
    s, e = raw.find('['), raw.rfind(']')
    if s != -1 and e > s:
        try:
            p = json.loads(raw[s:e + 1])
            if isinstance(p, list):
                return p
        except (json.JSONDecodeError, ValueError):
            pass

    # 3. Поиск объекта
    s, e = raw.find('{'), raw.rfind('}')
    if s != -1 and e > s:
        try:
            p = json.loads(raw[s:e + 1])
            if isinstance(p, dict):
                return [p]
        except (json.JSONDecodeError, ValueError):
            pass

    return None


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
    """Нормализует значение для сравнения."""
    if val is None:
        return ""
    s = str(val).strip().lower()
    if s in ("true", "1", "yes"):
        return "true"
    if s in ("false", "0", "no"):
        return "false"
    s = re.sub(r'\s*\(\d+\)\s*$', '', s)  # "CENTER (1)" → "center"
    # section_type ↔ type нормализация
    _type_map = {"header": "header", "body": "paragraph", "paragraph": "paragraph"}
    if s in _type_map:
        s = _type_map[s]
    return s


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
    llm_records: list[dict],
) -> dict:
    """
    Полная оценка: matching текстов + динамические метрики.
    Возвращает словарь с метриками.
    """
    key_map = build_key_map(gt_records, llm_records)

    # Matching
    matched = []
    used = set()
    for gt in gt_records:
        best_score, best_idx = 0.0, -1
        for i, llm in enumerate(llm_records):
            if i in used:
                continue
            sc = fuzzy_match(gt.get("text", ""), llm.get("text", ""))
            if sc > best_score:
                best_score, best_idx = sc, i
        if best_idx >= 0 and best_score > 0.3:
            used.add(best_idx)
            matched.append({"gt": gt, "llm": llm_records[best_idx], "score": best_score})

    # Метрики покрытия
    result: dict[str, Any] = {
        "gt_count": len(gt_records),
        "llm_count": len(llm_records),
        "matched_count": len(matched),
        "text_coverage_pct": round(len(matched) / len(gt_records) * 100, 1) if gt_records else 0,
        "avg_text_similarity": round(
            sum(p["score"] for p in matched) / len(matched) * 100, 1
        ) if matched else 0,
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
                        "text": pair["gt"].get("text", "")[:40],
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
    max_chars: int,
    workers: int = 4,
) -> dict[str, dict]:
    """
    Параллельно извлекает ground truth и текст из ВСЕХ файлов.
    Возвращает {path: {"gt": [...], "text": "...", "error": None}} 
    """
    cache: dict[str, dict] = {}
    lock = threading.Lock()
    
    def _process_file(path: str) -> tuple[str, dict]:
        try:
            gt = extract_ground_truth(path)
            text = extract_plain_text(path, max_chars)
            return path, {"gt": gt, "text": text, "error": None}
        except Exception as e:
            return path, {"gt": [], "text": "", "error": str(e)}
    
    print(f"\n⚡ Предвычисление GT для {len(files)} файлов ({workers} потоков)...")
    start = time.time()
    
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_process_file, f): f for f in files}
        done = 0
        for future in as_completed(futures):
            path, result = future.result()
            with lock:
                cache[path] = result
                done += 1
            fname = os.path.basename(path)
            if result["error"]:
                print(f"  ❌ [{done}/{len(files)}] {fname}: {result['error']}")
            else:
                print(f"  ✅ [{done}/{len(files)}] {fname}: {len(result['gt'])} элементов")
    
    elapsed = time.time() - start
    ok_count = sum(1 for v in cache.values() if not v["error"])
    print(f"  ⏱️  GT готов за {elapsed:.1f}s ({ok_count}/{len(files)} файлов ОК)")
    
    return cache


def _run_single(
    model: str,
    file_path: str,
    gt_cache: dict[str, dict],
    server_url: str,
    ollama_url: str,
    timeout: int,
) -> dict:
    """
    Один прогон: модель × файл.
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
    
    # Вызов LLM (это основной bottleneck)
    llm_records, elapsed = call_llm(text, model, server_url, ollama_url, timeout)
    
    if llm_records is None:
        return _error_result(model, fname, "No JSON", elapsed)
    
    # Оценка
    metrics = evaluate(gt, llm_records)
    metrics["model"] = model
    metrics["file"] = fname
    metrics["elapsed_sec"] = round(elapsed, 1)
    metrics["status"] = "OK"
    
    return metrics


def run_contest(
    models: list[str],
    files: list[str],
    server_url: str,
    ollama_url: str,
    max_chars: int,
    timeout: int,
    workers: int = 4,
) -> list[dict]:
    """
    Прогоняет все комбинации (модель × файл).
    GT предвычисляется параллельно, LLM вызовы — СТРОГО ПОСЛЕДОВАТЕЛЬНО:
    заканчиваем один файл, переходим к следующему.
    """
    # Фаза 1: параллельное предвычисление GT (CPU-bound, безопасно)
    gt_cache = precompute_all_gt(files, max_chars, workers=workers)
    
    # Фаза 2: ПОСЛЕДОВАТЕЛЬНЫЕ LLM вызовы
    # Порядок: для каждого файла прогоняем все модели, потом следующий файл
    total_runs = len(models) * len(files)
    results = []
    start_time = time.time()
    done = 0
    
    print(f"\n🚀 Запуск контеста: {total_runs} прогонов (последовательно)")
    
    for file_idx, file_path in enumerate(files, 1):
        fname = os.path.basename(file_path)
        print(f"\n{'='*60}")
        print(f"📄 [{file_idx}/{len(files)}] {fname}")
        print(f"{'='*60}")
        
        for model in models:
            done += 1
            
            result = _run_single(
                model, file_path, gt_cache,
                server_url, ollama_url, timeout
            )
            results.append(result)
            
            # Прогресс + ETA
            elapsed_total = time.time() - start_time
            avg_per_run = elapsed_total / done
            eta = avg_per_run * (total_runs - done)
            
            status = result.get("status", "?")
            if status == "OK":
                cov = result.get('text_coverage_pct', 0)
                score = result.get('overall_score', 0)
                elems = result.get('llm_count', 0)
                print(
                    f"  ✅ [{done}/{total_runs}] 🤖 {model} | "
                    f"Cov={cov}% Score={score}% Elems={elems} | "
                    f"ETA: {eta/60:.1f}min"
                )
            else:
                print(
                    f"  ❌ [{done}/{total_runs}] 🤖 {model} | "
                    f"{status} | ETA: {eta/60:.1f}min"
                )
    
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
        "attributes": {},
        "key_map": {},
    }


# ============================================================================
# ОТЧЁТ: Leaderboard + детали
# ============================================================================

def save_contest_report(results: list[dict], output_dir: str) -> str:
    """Генерирует Markdown leaderboard и JSON дамп."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = os.path.join(output_dir, f"contest_{ts}.md")
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

    # --- Markdown ---
    lines = [
        f"# 🏆 Formatting Contest Report",
        f"**Дата:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**Моделей:** {len(model_scores)} | **Файлов:** {len(set(r['file'] for r in results))} | "
        f"**Прогонов:** {len(results)}",
        "",
        "## Leaderboard",
        "",
        "| # | Модель | Файлов ✅/❌ | Avg Coverage | Avg Score | Avg Elements | Avg Time |",
        "|---|---|---|---|---|---|---|",
    ]

    for i, lb in enumerate(leaderboard, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        lines.append(
            f"| {medal} | `{lb['model']}` | {lb['files_ok']}/{lb['files_failed']} | "
            f"{lb['avg_coverage']:.1f}% | {lb['avg_overall']:.1f}% | "
            f"{lb['avg_elements']:.0f} | {lb['avg_time_sec']:.1f}s |"
        )

    # --- Детали по каждому прогону ---
    lines.extend(["", "## Детали по прогонам", ""])

    for r in results:
        status_icon = "✅" if r["status"] == "OK" else "❌"
        lines.append(f"### {status_icon} `{r['model']}` × `{r['file']}`")

        if r["status"] != "OK":
            lines.append(f"**Статус:** {r['status']}")
            lines.append("")
            continue

        lines.append(
            f"Coverage: {r['text_coverage_pct']}% | "
            f"Score: {r['overall_score']}% | "
            f"Elements: {r['llm_count']}/{r['gt_count']} | "
            f"Time: {r['elapsed_sec']}s"
        )

        if r.get("attributes"):
            lines.append("")
            lines.append("| Атрибут | Accuracy | Correct/Total |")
            lines.append("|---|---|---|")
            for attr, info in sorted(r["attributes"].items(), key=lambda x: x[1]["accuracy"]):
                icon = "✅" if info["accuracy"] >= 80 else "⚠️" if info["accuracy"] >= 50 else "❌"
                lines.append(
                    f"| {icon} `{attr}` | {info['accuracy']:.1f}% | {info['correct']}/{info['total']} |"
                )

            # Примеры ошибок (компактно)
            has_examples = any(info["examples"] for info in r["attributes"].values())
            if has_examples:
                lines.append("")
                lines.append("<details><summary>Примеры ошибок</summary>")
                lines.append("")
                for attr, info in r["attributes"].items():
                    for ex in info["examples"]:
                        lines.append(f"- **{attr}**: `{ex['text']}...` — ожидалось `{ex['expected']}`, получено `{ex['got']}`")
                lines.append("")
                lines.append("</details>")

        lines.append("")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # --- JSON (без examples для компактности) ---
    json_data = {
        "timestamp": datetime.now().isoformat(),
        "leaderboard": leaderboard,
        "runs": [{k: v for k, v in r.items() if k != "key_map"} for r in results],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)

    print(f"\n📊 Leaderboard: {md_path}")
    print(f"📦 JSON: {json_path}")

    # Консольный leaderboard
    print(f"\n{'='*60}")
    print("🏆 LEADERBOARD")
    print(f"{'='*60}")
    for i, lb in enumerate(leaderboard, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"  {i}."
        print(f"  {medal} {lb['model']:30s} Coverage={lb['avg_coverage']:5.1f}%  "
              f"Score={lb['avg_overall']:5.1f}%  Elements={lb['avg_elements']:5.0f}")

    return md_path


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Formatting Quality Contest")
    parser.add_argument("--server", "-s", default="http://localhost:8323",
                        help="URL middleware сервера")
    parser.add_argument("--ollama", "-o", default="http://192.168.0.107:11434",
                        help="URL Ollama сервера")
    parser.add_argument("--model", "-m", default=None,
                        help="Конкретная модель (по умолчанию — все)")
    parser.add_argument("--file", "-f", default=None,
                        help="Конкретный .docx файл (по умолчанию — все из test_docs/)")
    parser.add_argument("--timeout", "-t", type=int, default=300,
                        help="Таймаут на один LLM вызов (секунды)")
    parser.add_argument("--max-chars", type=int, default=12000,
                        help="Максимальная длина текста для LLM")
    parser.add_argument("--max-files", type=int, default=0,
                        help="Лимит файлов (0 = без лимита)")
    parser.add_argument("--workers", "-w", type=int, default=4,
                        help="Количество параллельных потоков")
    args = parser.parse_args()

    print("🏁 FORMATTING QUALITY CONTEST")
    print(f"   Server: {args.server}")
    print(f"   Ollama: {args.ollama}")

    # --- Модели ---
    if args.model:
        models = [args.model]
    else:
        models = discover_models(args.server, args.ollama)
        if not models:
            print("❌ Моделей не найдено")
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
    print(f"   Max chars: {args.max_chars} | Timeout: {args.timeout}s")

    # --- Контест ---
    results = run_contest(
        models=models,
        files=files,
        server_url=args.server,
        ollama_url=args.ollama,
        max_chars=args.max_chars,
        timeout=args.timeout,
        workers=args.workers,
    )

    # --- Отчёт ---
    if results:
        save_contest_report(results, REPORTS_DIR)
    else:
        print("❌ Нет результатов")


if __name__ == "__main__":
    main()
