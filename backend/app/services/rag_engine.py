import os
import re
import chromadb
from chromadb.utils import embedding_functions

# Настройки БД
DB_PATH = os.path.join(os.getcwd(), "data", "vector_db")

class RagEngine:
    def __init__(self):
        print(f"📂 Initializing Vector DB at: {DB_PATH}")
        # Инициализируем клиент
        self.client = chromadb.PersistentClient(path=DB_PATH)
        
        # Функция для векторизации текста
        self.emb_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        
        # Получаем или создаем коллекцию
        self.collection = self.client.get_or_create_collection(
            name="templates",
            embedding_function=self.emb_fn
        )

    def add_document(self, filename: str, markdown_text: str, metadata: dict):
        print(f"🚀 RAG Engine: Processing {filename} ({len(markdown_text)} chars)...")
        
        chunks = []
        ids = []
        metadatas = []
        
        # Разбиваем по заголовкам (#, ##, ###)
        sections = re.split(r'\n#{1,3}\s', markdown_text)
        
        if len(sections) > 1:
            print(f"   🔹 Found {len(sections)} semantic sections via headers.")
            for i, section in enumerate(sections):
                if len(section.strip()) < 50: continue
                
                content = section.strip()
                chunks.append(content)
                # Уникальный ID чанка
                ids.append(f"{filename}_sec_{i}")
                
                meta = metadata.copy()
                meta["source"] = filename
                meta["type"] = "section"
                metadatas.append(meta)
        else:
            # Fallback: если заголовков нет, рубим по 1000 символов
            print("   🔸 No headers found. Using fixed-size chunking.")
            chunk_size = 1000
            for i in range(0, len(markdown_text), chunk_size):
                content = markdown_text[i : i + chunk_size]
                if len(content) < 50: continue
                
                chunks.append(content)
                ids.append(f"{filename}_chunk_{i}")
                
                meta = metadata.copy()
                meta["source"] = filename
                meta["type"] = "chunk"
                metadatas.append(meta)

        if chunks:
            try:
                self.collection.add(
                    documents=chunks,
                    metadatas=metadatas,
                    ids=ids
                )
                print(f"   💾 Saved {len(chunks)} chunks to DB.")
            except Exception as e:
                print(f"   ❌ ChromaDB Error: {e}")
        else:
            print("   ⚠️ Warning: No valid text chunks created.")

    def search(self, query_text: str, n_results: int = 3):
        print(f"🔍 Searching DB for: '{query_text[:50]}...'")
        results = self.collection.query(
            query_texts=[query_text],
            n_results=n_results
        )
        return results

# Создаем экземпляр класса
rag_engine = RagEngine()