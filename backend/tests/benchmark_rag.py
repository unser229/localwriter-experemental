import sys
import os
import glob
import random
import time
import pandas as pd
from datetime import datetime
from typing import List
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- 1. НАСТРОЙКА ПУТЕЙ ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_ROOT = os.path.dirname(CURRENT_DIR)

if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

# --- 2. ИМПОРТЫ ---
try:
    from tqdm import tqdm  # Прогресс-бар
    from app.services.rag_engine import RagEngine
    from app.services.style_extractor import style_extractor
    import app.services.rag_engine
except ImportError as e:
    print(f"❌ Ошибка импорта: {e}")
    if "tqdm" in str(e):
        print("💡 Совет: выполните 'poetry add tqdm'")
    sys.exit(1)

# Настройка путей
TEST_DOCS_DIR = os.path.join(CURRENT_DIR, "test_docs")
TEST_DB_PATH = os.path.join(BACKEND_ROOT, "data", "test_vector_db")
REPORTS_DIR = os.path.join(CURRENT_DIR, "reports")

os.makedirs(TEST_DOCS_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

# Monkey Patching пути к БД
app.services.rag_engine.DB_PATH = TEST_DB_PATH
rag = RagEngine()

# Количество потоков (не ставьте слишком много, чтобы ChromaDB/SQLite не залочилась)
MAX_WORKERS = 4 

def _ingest_single_file(file_path):
    """Вспомогательная функция для потока индексации"""
    filename = os.path.basename(file_path)
    start_time = time.time()
    try:
        # Важно: rag instance общий, методы ChromaDB обычно потокобезопасны 
        # (но если база локальная sqlite, возможны локи при записи, поэтому workers=4)
        rag.add_document(file_path, filename)
        status = "Success"
    except Exception as e:
        status = f"Failed: {str(e)[:100]}" # Обрезаем длинные ошибки
    
    elapsed = time.time() - start_time
    return {"file": filename, "time_sec": round(elapsed, 2), "status": status}

def run_ingestion_benchmark():
    print(f"\n=== PHASE 1: INGESTION & PARSING (Parallel: {MAX_WORKERS} threads) ===")
    docx_files = glob.glob(os.path.join(TEST_DOCS_DIR, "*.docx"))
    
    if not docx_files:
        print(f"⚠️ Файлы не найдены в {TEST_DOCS_DIR}")
        return None

    # Очистка базы
    print("🧹 Cleaning DB...")
    try:
        rag.client.delete_collection("styled_templates_v3")
    except: pass
    rag.collection = rag.client.get_or_create_collection("styled_templates_v3", embedding_function=rag.emb_fn)

    stats = []
    
    # ПАРАЛЛЕЛЬНЫЙ ЗАПУСК
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Создаем задачи
        future_to_file = {executor.submit(_ingest_single_file, f): f for f in docx_files}
        
        # Прогресс бар
        with tqdm(total=len(docx_files), desc="Indexing", unit="doc") as pbar:
            for future in as_completed(future_to_file):
                result = future.result()
                stats.append(result)
                # Если ошибка, пишем в доп. инфо бара, иначе просто обновляем
                if "Failed" in result['status']:
                    pbar.write(f"❌ {result['file']}: {result['status']}")
                pbar.update(1)

    return pd.DataFrame(stats)

def _retrieve_single_file(filename):
    """Вспомогательная функция для потока поиска"""
    file_path = os.path.join(TEST_DOCS_DIR, filename)
    file_results = []
    
    try:
        # Парсинг делаем тут же (он CPU-bound, но в потоках тоже ускорится)
        chunks = style_extractor.parse_docx(file_path)
        if not chunks: return []
        
        valid_chunks = [c['text'] for c in chunks if len(c['text']) > 60]
        if not valid_chunks: return []
        
        # Берем 3 случайных запроса
        test_queries = random.sample(valid_chunks, min(len(valid_chunks), 3))
            
        for query in test_queries:
            search_query = query[:200] 
            start_t = time.time()
            
            # Поиск в RAG
            results = rag.search(search_query, n_results=3)
            search_time = time.time() - start_t
            
            found, retrieved_style = False, False
            top_match = "None"
            
            if results['metadatas'] and len(results['metadatas'][0]) > 0:
                top_match = results['metadatas'][0][0].get('source', 'Unknown')
                found = (top_match == filename)
                first_doc_content = results['documents'][0][0]
                retrieved_style = ("[S: " in first_doc_content)

            file_results.append({
                "source_doc": filename,
                "found_doc": top_match,
                "success": found,
                "style_ok": retrieved_style,
                "time_ms": round(search_time * 1000, 2),
                "query": search_query.replace("\n", " ")[:50] + "..."
            })
    except Exception as e:
        # Логируем ошибку, но не валим поток
        print(f"Error processing {filename}: {e}")
        
    return file_results

def run_retrieval_accuracy_test(df_ingestion):
    print(f"\n=== PHASE 2: RETRIEVAL ACCURACY (Parallel: {MAX_WORKERS} threads) ===")
    
    if df_ingestion is None or df_ingestion.empty:
        return pd.DataFrame()

    success_files = df_ingestion[df_ingestion['status'] == 'Success']['file'].tolist()
    all_results_log = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_file = {executor.submit(_retrieve_single_file, f): f for f in success_files}
        
        with tqdm(total=len(success_files), desc="Retrieving", unit="doc") as pbar:
            for future in as_completed(future_to_file):
                results = future.result()
                all_results_log.extend(results)
                pbar.update(1)

    return pd.DataFrame(all_results_log)

def run_formatting_coverage_test():
    print("\n=== PHASE 2.1: AUTOMATED FORMATTING COVERAGE (INTROSPECTION) ===")
    
    # 1. Интроспекция
    all_possible_tags = style_extractor.schema.get_all_formatting_keys()
    print(f"🔍 Introspection complete: System supports {len(all_possible_tags)} formatting attributes.")
    
    results = rag.collection.get(limit=100)
    all_metas = results['metadatas']
    
    if not all_metas:
        print("⚠️ База пуста.")
        return pd.DataFrame(), 0

    coverage_stats = {tag: 0 for tag in all_possible_tags}
    total_docs = len(all_metas)
    
    core_tags = ["S", "F", "P", "B", "I", "IND-F", "IND-L", "SB", "SA", "LS"]
    
    for meta in all_metas:
        rich_text = meta.get("rich_content", "")
        for tag in all_possible_tags:
            if f"[{tag}:" in rich_text:
                coverage_stats[tag] += 1

    rows = []
    active_tags_count = 0
    total_score_accum = 0
    
    sorted_tags = sorted(all_possible_tags, key=lambda x: (0 if x in core_tags else 1, x))

    for tag in sorted_tags:
        count = coverage_stats[tag]
        if tag in core_tags or count > 0:
            score = (count / total_docs) * 100
            
            status = "✅" if score > 90 else "⚠️" if score > 0 else "⚪"
            if tag in core_tags and score < 50: status = "❌"
            
            rows.append({
                "Tool": tag, 
                "Usage": f"{score:.1f}%", 
                "Status": status
            })
            
            if tag in core_tags:
                total_score_accum += score
                active_tags_count += 1
    
    df_coverage = pd.DataFrame(rows)
    avg_score = total_score_accum / active_tags_count if active_tags_count else 0
    
    print(df_coverage.to_string())
    print(f"\n🔥 CORE METRICS SCORE: {avg_score:.2f}%")
        
    return df_coverage, avg_score

def run_semantic_stress_test():
    print("\n=== PHASE 3: SEMANTIC STRESS TEST ===")
    manual_tests = [
        {"query": "Введение и цели работы", "expected": "Введение"},
        {"query": "Список литературы и источники", "expected": "Литература"},
        {"query": "Титульный лист министерство", "expected": "Министерство"},
    ]
    semantic_results = []
    
    for case in manual_tests:
        res = rag.search(case['query'], n_results=1)
        found_in = "Nothing"
        snippet = ""
        match = False

        if res['metadatas'] and len(res['metadatas'][0]) > 0:
            found_in = res['metadatas'][0][0].get('source')
            snippet = res['documents'][0][0][:120]
            
            s_lower = snippet.lower()
            f_lower = found_in.lower()
            e_lower = case['expected'].lower()
            
            if e_lower in s_lower or e_lower in f_lower:
                match = True
            
        semantic_results.append({
            "query": case['query'], 
            "found_in": found_in, 
            "match": "✅" if match else "❌",
            "snippet": snippet.replace("\n", " ")
        })
    return pd.DataFrame(semantic_results)

def save_report(df_ingest, df_recall, df_coverage, coverage_score, df_semantic):
    if df_ingest is None or df_recall.empty:
        print("❌ Недостаточно данных для генерации отчета.")
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(REPORTS_DIR, f"benchmark_{timestamp}.md")
    csv_path = os.path.join(REPORTS_DIR, f"raw_data_{timestamp}.csv")
    
    accuracy = df_recall['success'].mean() * 100
    integrity = df_recall['style_ok'].mean() * 100
    avg_time = df_recall['time_ms'].mean()

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# RAG Benchmark Report - {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write(f"## 📊 Summary Metrics\n")
        f.write(f"- **Total Accuracy (Recall):** {accuracy:.2f}%\n")
        f.write(f"- **Formatting Coverage Score:** {coverage_score:.2f}%\n")
        f.write(f"- **Style Data Integrity:** {integrity:.2f}%\n")
        f.write(f"- **Avg Retrieval Latency:** {avg_time:.2f} ms\n\n")
        
        f.write("## 🏗️ Phase 2.1: Automated Style Coverage (Introspection)\n")
        if not df_coverage.empty:
            f.write(df_coverage.to_markdown(index=False) + "\n\n")
        else:
            f.write("No data.\n\n")

        f.write("## 📥 Phase 1: Ingestion\n")
        f.write(df_ingest.to_markdown(index=False) + "\n\n")
        
        f.write("## 🔍 Phase 2: Retrieval Accuracy\n")
        f.write(df_recall[['source_doc', 'found_doc', 'success', 'style_ok', 'time_ms']].to_markdown(index=False) + "\n\n")
        
        f.write("## 🧠 Phase 3: Semantic Stress Test\n")
        f.write(df_semantic.to_markdown(index=False) + "\n")

    df_recall.to_csv(csv_path, index=False)
    print(f"\n✅ Отчет сохранен: {report_path}")
    print(f"✅ Сырые данные: {csv_path}")

if __name__ == "__main__":
    df_i = run_ingestion_benchmark()
    if df_i is not None:
        df_r = run_retrieval_accuracy_test(df_i)
        df_cov, cov_score = run_formatting_coverage_test()
        df_s = run_semantic_stress_test()
        
        save_report(df_i, df_r, df_cov, cov_score, df_s)
    else:
        print("❌ Тест прерван: файлы .docx не найдены.")