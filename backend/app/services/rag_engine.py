import os
import chromadb
from chromadb.utils import embedding_functions
from app.services.style_extractor import style_extractor

DB_PATH = os.path.join(os.getcwd(), "data", "vector_db")

class RagEngine:
    def __init__(self):
        self.client = chromadb.PersistentClient(path=DB_PATH)
        # Оставляем эту модель, но в будущем рекомендую 'intfloat/multilingual-e5-small'
        self.emb_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="paraphrase-multilingual-MiniLM-L12-v2"
        )
        self.collection = self.client.get_or_create_collection(
            name="styled_templates_v3", 
            embedding_function=self.emb_fn
        )

    def add_document(self, file_path: str, original_filename: str):
        parsed_chunks = style_extractor.parse_docx(file_path)
        
        documents = []
        metadatas = []
        ids = []

        for chunk in parsed_chunks:
            search_text = f"{chunk['semantic_tag']}{chunk['text']}"
            # ЧИСТЫЙ ТЕКСТ ДЛЯ ПОИСКА (без префиксов "Документ:")
            # Если это заголовок, мы его дублируем, чтобы усилить вес
            text = chunk['text']
            if chunk['metadata']['is_header']:
                text = f"{text} {text} {text}" # Triple boost для заголовков

            documents.append(text)
            
            # Сохраняем ВСЁ форматирование в метаданные
            meta = chunk['metadata']
            meta["source"] = original_filename
            meta["rich_content"] = f"{chunk['style_desc']}\nCONTENT: {chunk['text']}"
            
            metadatas.append(meta)
            ids.append(f"{original_filename}_{chunk['metadata']['source_idx']}")

        if documents:
            self.collection.add(
                documents=documents,
                metadatas=metadatas,
                ids=ids
            )

    def search(self, query_text: str, n_results: int = 5):
        # Поиск по чистому тексту
        results = self.collection.query(
            query_texts=[query_text],
            n_results=n_results
        )
        
        # Подменяем возвращаемые документы на rich_content с тегами стилей
        final_docs = []
        if results['metadatas']:
            for meta_list in results['metadatas']:
                final_docs.append([m['rich_content'] for m in meta_list])
        
        results['documents'] = final_docs
        return results
    def search_style_reference(self, query_text: str):
        """
        Специальный поиск для Apply Template.
        Находит лучший документ и возвращает разные типы блоков из него,
        чтобы LLM видела, как в этом документе оформляются заголовки, таблицы и текст.
        """
        # 1. Сначала находим, какой файл нам больше всего подходит
        results = self.collection.query(query_texts=[query_text], n_results=1)
        if not results['metadatas'] or not results['metadatas'][0]:
            return None
            
        best_filename = results['metadatas'][0][0]['source']
        
        # 2. Делаем второй запрос: достаем из этого же файла разные типы стилей
        # Достаем 10 разных блоков из этого же файла
        all_blocks = self.collection.get(
            where={"source": best_filename},
            limit=20
        )
        
        # Группируем по стилям, чтобы показать LLM разнообразие
        # (Например: один заголовок, одна таблица, один список, один текст)
        unique_styles = {}
        formatted_context = [f"REFERENCE DOCUMENT: {best_filename}\n"]
        
        for doc_content, meta in zip(all_blocks['documents'], all_blocks['metadatas']):
            s_name = meta['style_name']
            if s_name not in unique_styles:
                unique_styles[s_name] = doc_content
                formatted_context.append(doc_content)
        
        return {
            "full_context": "\n\n".join(formatted_context),
            "source_id": best_filename
        }

rag_engine = RagEngine()