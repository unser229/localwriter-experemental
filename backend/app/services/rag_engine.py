import os
import threading
import chromadb
from chromadb.utils import embedding_functions
from app.services.style_extractor import style_extractor
from app.config import settings  # <--- ВАЖНО: Добавлен этот импорт

DB_PATH = os.path.join(os.getcwd(), "data", "vector_db")

class RagEngine:
    _instance = None
    _lock = threading.Lock() # Глобальный мьютекс для записи

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(RagEngine, cls).__new__(cls)
            cls._instance.initialized = False
        return cls._instance

    def __init__(self):
        if self.initialized: return
        
        self.client = chromadb.PersistentClient(path=DB_PATH)
        self.emb_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="paraphrase-multilingual-MiniLM-L12-v2"
        )
        self.collection = self.client.get_or_create_collection(
            name="styled_templates_v3", 
            embedding_function=self.emb_fn
        )
        self.initialized = True

    def add_document(self, file_path: str, original_filename: str):
        """
        Thread-safe ingestion.
        """
        parsed_chunks = style_extractor.parse_docx(file_path)
        
        documents = []
        metadatas = []
        ids = []

        for chunk in parsed_chunks:
            # Для поиска используем текст.
            search_text = chunk['text']
            if chunk['metadata']['is_header']:
                search_text = f"{search_text} {search_text} {search_text}"

            documents.append(search_text)
            
            meta = chunk['metadata']
            meta["source"] = original_filename
            meta["rich_content"] = f"{chunk['style_desc']}\nCONTENT: {chunk['text']}"
            
            metadatas.append(meta)
            ids.append(f"{original_filename}_{chunk['metadata']['source_idx']}")

        if documents:
            # КРИТИЧЕСКАЯ СЕКЦИЯ: Запись в БД
            with self._lock:
                try:
                    self.collection.add(
                        documents=documents,
                        metadatas=metadatas,
                        ids=ids
                    )
                except Exception as e:
                    print(f"⚠️ DB Write Error ({original_filename}): {e}")

    def search(self, query_text: str, n_results: int = 5):
        """Простой поиск ближайших фрагментов"""
        results = self.collection.query(
            query_texts=[query_text],
            n_results=n_results
        )
        
        final_docs = []
        if results['metadatas']:
            for meta_list in results['metadatas']:
                final_docs.append([m.get('rich_content', '') for m in meta_list])
        
        results['documents'] = final_docs
        return results

    def search_style_reference(self, query_text: str):
        """
        Стратегия "Style Palette" с оптимизацией под железо.
        """
        # Шаг 1: Находим лучший документ-кандидат
        results = self.collection.query(query_texts=[query_text], n_results=1)
        if not results['metadatas'] or not results['metadatas'][0]:
            return None
            
        best_filename = results['metadatas'][0][0]['source']
        
        # Шаг 2: Достаем контекст.
        # ОПТИМИЗАЦИЯ: Лимит берем из профиля железа (30 для мощных, 10 для слабых)
        limit_blocks = 30 if not settings.is_low_power else 10
        
        all_blocks = self.collection.get(
            where={"source": best_filename},
            limit=limit_blocks 
        )
        
        # Шаг 3: Фильтруем, чтобы не дублировать одинаковые стили
        unique_styles = set()
        formatted_context = [f"REFERENCE DOCUMENT: {best_filename}\n"]
        
        # Приоритет отдаем заголовкам
        sorted_indices = sorted(
            range(len(all_blocks['documents'])), 
            key=lambda i: 0 if all_blocks['metadatas'][i].get('is_header') else 1
        )

        count = 0
        # ОПТИМИЗАЦИЯ: Жесткий лимит количества примеров (10 или 3)
        max_examples = settings.RAG_CHUNK_LIMIT

        for i in sorted_indices:
            if count >= max_examples: break 
            
            meta = all_blocks['metadatas'][i]
            rich_content = meta.get('rich_content', '')
            style_sig = meta.get('style_name', 'Normal')
            
            # Простая эвристика уникальности
            sig = f"{style_sig}_{rich_content[:20]}"
            
            if sig not in unique_styles:
                unique_styles.add(sig)
                formatted_context.append(rich_content)
                count += 1
        
        return {
            "full_context": "\n\n".join(formatted_context),
            "source_id": best_filename
        }

rag_engine = RagEngine()