import sys
import os
# Добавляем текущую папку в путь, чтобы видеть app.services
sys.path.append(os.getcwd())

from app.services.rag_engine import rag_engine

# 1. Посмотрим, сколько всего документов (чанков) в базе
count = rag_engine.collection.count()
print(f"📚 Total chunks in DB: {count}")

# 2. Выведем первые 3 документа, чтобы убедиться, что текст на месте
if count > 0:
    print("\n🔍 Preview of stored data:")
    peek = rag_engine.collection.peek(limit=3)
    
    for i in range(len(peek['ids'])):
        print(f"\n--- Chunk {peek['ids'][i]} ---")
        print(f"Source: {peek['metadatas'][i].get('source')}")
        # Выводим первые 200 символов текста
        text_preview = peek['documents'][i][:200].replace('\n', ' ')
        print(f"Text: {text_preview}...")
else:
    print("Database is empty!")