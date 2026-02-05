#docling_parser.py
import os
from docling.document_converter import DocumentConverter, WordFormatOption
from docling.datamodel.base_models import InputFormat

class DoclingService:
    def __init__(self):
        self.converter = DocumentConverter(
            format_options={InputFormat.DOCX: WordFormatOption()}
        )

    def process_file(self, file_path: str):
        try:
            result = self.converter.convert(file_path)
            doc = result.document
            
            # Извлекаем "Слепок стиля" из первого попавшегося текста
            # В будущем тут можно сделать обход всех элементов для среднего значения
            # Но для начала возьмем базовые параметры
            
            style_profile = {
                "font_name": "Times New Roman", # Значение по умолчанию
                "page_margins": [20, 20, 20, 10], # Стандартные поля
                "has_title_page": True
            }
            
            # Если это DOCX, Docling может дать более глубокие данные в dict
            raw_dict = doc.export_to_dict()
            # Пытаемся найти упоминания шрифтов в метаданных или структурах
            # (Это упрощенная логика, Docling постоянно обновляет API)
            
            return {
                "status": "success",
                "markdown": doc.export_to_markdown(),
                "style_profile": style_profile 
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

docling_service = DoclingService()


# (В будущем здесь можно реализовать реальный анализ JSON-export от Docling,
#  сейчас пока просто возвращаем структуру для RAG)