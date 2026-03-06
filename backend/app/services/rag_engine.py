import os
import re
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
        Группирует мелкие параграфы в чанки по ~500 токенов (900 символов),
        чтобы RAG возвращал осмысленные куски текста фиксированного размера.
        """
        parsed_chunks = style_extractor.parse_docx(file_path)
        
        documents = []
        metadatas = []
        ids = []

        # Настраиваемый целевой размер чанка в символах (500 токенов * 1.8 = 900)
        MAX_CHUNK_CHARS = 900 
        
        current_chunk_text = []
        current_rich_content = []
        current_chars = 0
        chunk_idx = 0
        
        # Сохраняем метаданные первого абзаца в чанке как "главные"
        current_meta = None

        def flush_chunk():
            nonlocal chunk_idx, current_chunk_text, current_rich_content, current_chars, current_meta
            if not current_chunk_text:
                return
                
            search_text = "\n".join(current_chunk_text)
            if current_meta and current_meta.get('is_header'):
                search_text = f"{search_text} {search_text}"

            documents.append(search_text)
            
            # Собираем метаданные
            meta = current_meta.copy() if current_meta else {}
            meta["source"] = original_filename
            meta["rich_content"] = "\n\n".join(current_rich_content)
            
            metadatas.append(meta)
            ids.append(f"{original_filename}_chunk_{chunk_idx}")
            
            # Reset
            chunk_idx += 1
            current_chunk_text = []
            current_rich_content = []
            current_chars = 0
            current_meta = None

        for p_data in parsed_chunks:
            text_len = len(p_data['text'])
            
            # Если добавление этого абзаца превысит лимит (и чанк уже не пустой) -> сбрасываем чанк
            if current_chars + text_len > MAX_CHUNK_CHARS and current_chars > 0:
                flush_chunk()
                
            if not current_meta:
                current_meta = p_data['metadata']
                
            current_chunk_text.append(p_data['text'])
            current_rich_content.append(f"{p_data['style_desc']}\nCONTENT: {p_data['text']}")
            current_chars += text_len
            
        # Сбрасываем остаток
        flush_chunk()

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
        Стратегия "Style Palette" с жесткой фильтрацией по токенам (чанковой гильотиной).
        """
        # Находим ЛУЧШИЕ чанки (K=3)
        # 3 чанка * 500 токенов = 1500 токенов максимум (идеально ложится в лимит)
        K = min(getattr(settings, 'RAG_CHUNK_LIMIT', 3), 5)
        MAX_DIST = getattr(settings, 'RAG_MAX_DISTANCE', 1.5) # Порог отсечения мусора
        
        results = self.collection.query(query_texts=[query_text], n_results=K)
        if not results['metadatas'] or not results['metadatas'][0]:
            return None
            
        valid_metas = []
        distances = results.get('distances', [[0]*K])[0]
        
        for idx, dist in enumerate(distances):
            if dist <= MAX_DIST:
                valid_metas.append(results['metadatas'][0][idx])
            else:
                print(f"🔸 RAG Chunk Rejected: Distance {dist:.2f} > Threshold {MAX_DIST}")
                
        if not valid_metas:
            print("🔸 RAG: No style reference passed distance threshold.")
            return None
            
        best_filename = valid_metas[0]['source']
        
        unique_styles = set()
        formatted_context = [f"REFERENCE DOCUMENT: {best_filename}\n"]
        style_map = {}
        
        # Обрабатываем найденные валидные чанки
        for meta in valid_metas:
            rich_content_block = meta.get('rich_content', '')
            
            # Чанк состоит из нескольких абзацев, разделенных "\n\n"
            for absatz_rich_content in rich_content_block.split("\n\n"):
                if not absatz_rich_content.strip(): continue
                    
                style_sig = meta.get('style_name', 'Normal')
                section_type = meta.get('section_type', 'paragraph')
                
                sig = f"{style_sig}_{absatz_rich_content[:20]}"
                
                if sig not in unique_styles:
                    unique_styles.add(sig)
                    formatted_context.append(absatz_rich_content)
                    
                    # Извлекаем свойства для жесткой логики Semantic Mapper
                    parts = absatz_rich_content.split("\nCONTENT:")
                    style_desc = parts[0]
                    
                    parsed_style = {"style_name": style_sig, "type": section_type}
                    
                    f_match = re.search(r'\[F:\s*([^]]+)\]', style_desc)
                    if f_match: parsed_style["font_family"] = f_match.group(1).strip()
                    
                    p_match = re.search(r'\[P:\s*([^]]+)\]', style_desc)
                    if p_match: 
                        try: parsed_style["font_size"] = float(p_match.group(1))
                        except: pass
                    
                    b_match = re.search(r'\[B:\s*([^]]+)\]', style_desc)
                    if b_match: parsed_style["bold"] = (b_match.group(1).lower() == 'true')
                    
                    a_match = re.search(r'\[A:\s*([^]]+)\]', style_desc)
                    if a_match: 
                        align_str = a_match.group(1).lower()
                        if "center" in align_str or "1" in align_str: parsed_style["align"] = "center"
                        elif "right" in align_str or "2" in align_str: parsed_style["align"] = "right"
                        elif "justify" in align_str or "3" in align_str: parsed_style["align"] = "justify"
                        else: parsed_style["align"] = "left"
                    else: parsed_style["align"] = "left"
                    
                    style_map[style_sig] = parsed_style
        
        return {
            "full_context": "\n\n".join(formatted_context),
            "source_id": best_filename,
            "style_map": style_map
        }

    def search_batch_fast_track(
        self,
        texts: list[str],
        fast_track_distance: float = 0.20,
    ) -> dict[int, str]:
        """
        Батчевый Vector Fast Track: один запрос к ChromaDB для всего батча.
        Защита от N+1: вместо 15 отдельных запросов — один запрос с 15 текстами.

        Args:
            texts: Список текстов параграфов из батча (порядок = индексы 0..N-1)
            fast_track_distance: Порог уверенности. Если distance <= этого порога,
                                 стиль назначается без LLM.

        Returns:
            {batch_idx: style_name} — только для параграфов с высокой уверенностью.
        """
        if not texts:
            return {}

        try:
            results = self.collection.query(
                query_texts=texts,
                n_results=1,  # для каждого текста — только лучший кандидат
            )
        except Exception as e:
            print(f"⚠️ RAG batch fast track error: {e}")
            return {}

        fast_track_hits: dict[int, str] = {}

        distances_matrix = results.get("distances", [])
        metadatas_matrix = results.get("metadatas", [])

        for batch_idx, (dist_list, meta_list) in enumerate(
            zip(distances_matrix, metadatas_matrix)
        ):
            if not dist_list or not meta_list:
                continue
            dist = dist_list[0]
            meta = meta_list[0]
            if dist <= fast_track_distance:
                style_name = meta.get("style_name") or meta.get("tag_S", "Normal")
                fast_track_hits[batch_idx] = style_name
                print(f"  ⚡ Vector FastTrack[{batch_idx}]: dist={dist:.3f} → '{style_name}'")

        return fast_track_hits


rag_engine = RagEngine()