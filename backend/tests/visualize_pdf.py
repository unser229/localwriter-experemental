import sys
import os
import glob
import subprocess
import docx
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_COLOR_INDEX

# Настройка путей
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_ROOT = os.path.dirname(CURRENT_DIR)
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

try:
    # Импортируем ваш экстрактор, чтобы использовать ту же логику
    from app.services.style_extractor import style_extractor
except ImportError as e:
    print(f"❌ Ошибка импорта: {e}")
    sys.exit(1)

TEST_DOCS_DIR = os.path.join(CURRENT_DIR, "test_docs")
REPORTS_DIR = os.path.join(CURRENT_DIR, "visual_reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

def add_rag_annotation(doc, original_path):
    """
    Проходит по документу и вставляет технические пометки RAG
    """
    # Мы используем логику парсинга из style_extractor, 
    # но нам нужно применять её построчно к объектам docx
    
    # Чтобы не дублировать код парсинга, мы "симулируем" проход
    # Но для визуализации нам нужно модифицировать сам объект doc
    
    # Счетчик для эвристик (как в style_extractor)
    for i, para in enumerate(doc.paragraphs):
        text = para.text.strip()
        if not text: continue

        # --- 1. ПОВТОРЯЕМ ЛОГИКУ StyleExtractor ---
        # (Копируем логику получения тегов, чтобы видеть то же самое, что и RAG)
        text_lower = text.lower()
        section_type = "body"
        
        # ТА ЖЕ ЭВРИСТИКА, ЧТО В ВАШЕМ КОДЕ:
        if i < 15 and any(w in text_lower for w in ["министерство", "федеральное", "университет", "выполнил", "проверил", "студент"]):
            section_type = "title_page"
        elif any(w in text_lower for w in ["введение", "цель работы", "актуальность"]):
            section_type = "intro"
        elif "список" in text_lower and "литератур" in text_lower:
            section_type = "references"

        # Получаем стили
        style_name = para.style.name
        
        # --- 2. ВИЗУАЛИЗАЦИЯ (Вставка в документ) ---
        
        # А. Подсветка фона (Highlight) в зависимости от типа секции
        highlight_color = WD_COLOR_INDEX.AUTO
        if section_type == "title_page": highlight_color = WD_COLOR_INDEX.TURQUOISE
        elif section_type == "intro": highlight_color = WD_COLOR_INDEX.BRIGHT_GREEN
        elif section_type == "references": highlight_color = WD_COLOR_INDEX.YELLOW
        
        if highlight_color != WD_COLOR_INDEX.AUTO:
            for run in para.runs:
                run.font.highlight_color = highlight_color

        # Б. Вставка технической строки НАД абзацем
        # Формируем строку описания
        rag_info = f"[RAG SEES: {section_type.upper()}] [Style: {style_name}]"
        
        # Пытаемся вставить параграф перед текущим
        try:
            p_new = para.insert_paragraph_before(rag_info)
            # Форматируем этот технический параграф
            p_new.style = "Normal" # Сброс стиля
            run = p_new.runs[0]
            run.font.size = Pt(8)
            run.font.color.rgb = RGBColor(255, 0, 0) # Красный цвет
            run.font.name = "Courier New"
            p_new.paragraph_format.space_after = Pt(0) # Прижать к тексту
        except Exception as e:
            print(f"Warning inserting annotation: {e}")

    return doc

def convert_to_pdf(docx_path, output_dir):
    """Конвертирует DOCX в PDF используя LibreOffice"""
    try:
        cmd = [
            "soffice", "--headless", "--convert-to", "pdf",
            docx_path, "--outdir", output_dir
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"⚠️ Не удалось конвертировать в PDF (нужен libreoffice): {e}")
        return False

def main():
    docx_files = glob.glob(os.path.join(TEST_DOCS_DIR, "*.docx"))
    if not docx_files:
        print("❌ Нет файлов в tests/test_docs")
        return

    print(f"🔍 Генерация визуальных PDF в {REPORTS_DIR}...")

    for file_path in docx_files:
        filename = os.path.basename(file_path)
        print(f"Processing: {filename}...")
        
        try:
            # 1. Открываем оригинал
            doc = docx.Document(file_path)
            
            # 2. Размечаем
            doc = add_rag_annotation(doc, file_path)
            
            # 3. Сохраняем временный DOCX
            annotated_docx = os.path.join(REPORTS_DIR, f"RAG_VIEW_{filename}")
            doc.save(annotated_docx)
            
            # 4. Конвертируем в PDF
            if convert_to_pdf(annotated_docx, REPORTS_DIR):
                print(f"✅ Created PDF: RAG_VIEW_{filename.replace('.docx', '.pdf')}")
                # Удаляем временный docx, чтобы не мусорить (опционально)
                # os.remove(annotated_docx) 
            else:
                print(f"⚠️ Created DOCX only: {annotated_docx}")
            
        except Exception as e:
            print(f"❌ Error processing {filename}: {e}")

if __name__ == "__main__":
    main()