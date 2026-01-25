import os
from pathlib import Path
from docling.document_converter import DocumentConverter, PdfFormatOption, WordFormatOption
from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
from docling.datamodel.base_models import InputFormat

class DoclingService:
    def __init__(self):
        # Настраиваем пайплайн для PDF (OCR, Таблицы)
        # Мы включаем TableStructure, чтобы понимать сложные таблицы в договорах
        pipeline_options = PdfPipelineOptions(do_table_structure=True)
        pipeline_options.table_structure_options.mode = TableFormerMode.ACCURATE

        self.converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
                InputFormat.DOCX: WordFormatOption() # DOCX поддерживается нативно
            }
        )

    def process_file(self, file_path: str):
        """
        Превращает файл в структуру данных.
        Возвращает Markdown (для смысла) и JSON (для структуры/стилей).
        """
        try:
            print(f"Starting Docling processing for: {file_path}")
            result = self.converter.convert(file_path)
            doc = result.document
            
            # 1. Markdown - это пойдет в контекст LLM (RAG)
            markdown_content = doc.export_to_markdown()
            
            # 2. JSON/Dict - это структура документа (заголовки, таблицы, иерархия)
            # Это пригодится для извлечения "Шаблона стиля"
            structure_data = doc.export_to_dict()
            
            return {
                "status": "success",
                "markdown": markdown_content,
                "metadata": structure_data.get("metadata", {}),
                # "full_structure": structure_data # Можно раскомментировать для дебага, но там много данных
            }
            
        except Exception as e:
            print(f"Docling error: {e}")
            return {"status": "error", "message": str(e)}

# Создаем синглтон, чтобы не грузить модели в память при каждом запросе
docling_service = DoclingService()