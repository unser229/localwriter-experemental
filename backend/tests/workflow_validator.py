import sys
import os
import glob
import zipfile
import pandas as pd
import subprocess
from lxml import etree
import docx
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_COLOR_INDEX
from datetime import datetime

# --- НАСТРОЙКА ПУТЕЙ ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_ROOT = os.path.dirname(CURRENT_DIR)
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

try:
    # Импортируем ваш текущий экстрактор (Predictor)
    from app.services.style_extractor import style_extractor
except ImportError as e:
    print(f"❌ Ошибка импорта: {e}")
    sys.exit(1)

TEST_DOCS_DIR = os.path.join(CURRENT_DIR, "test_docs")
REPORTS_DIR = os.path.join(CURRENT_DIR, "validation_reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

# --- 1. DATA COLLECTION: XML GROUND TRUTH (ИСПРАВЛЕННЫЙ) ---
class XmlFeatureExtractor:
    """
    Извлекает 'чистые' данные напрямую из XML структуры DOCX.
    Это наша 'Истина в последней инстанции' (Ground Truth).
    """
    def extract(self, docx_path):
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        features = []
        
        try:
            with zipfile.ZipFile(docx_path) as z:
                xml_content = z.read('word/document.xml')
                tree = etree.fromstring(xml_content)
                
                # Ищем все параграфы
                for p in tree.xpath('//w:p', namespaces=ns):
                    # Извлекаем текст (собираем из всех run-ов)
                    texts = p.xpath('.//w:t/text()', namespaces=ns)
                    full_text = "".join(texts).strip()
                    
                    # Чтобы синхронизироваться с RAG, пропускаем пустые, если RAG их пропускает
                    # (В style_extractor мы пропускаем пустые, если нет разрыва страницы)
                    # Для упрощения валидации пока берем только непустые
                    if not full_text:
                        continue

                    # 1. Свойства абзаца (Стиль)
                    pPr = p.find('w:pPr', namespaces=ns)
                    style_id = "Normal" # Default
                    if pPr is not None:
                        style_node = pPr.find('w:pStyle', namespaces=ns)
                        if style_node is not None:
                            style_id = style_node.get(f"{{{ns['w']}}}val")

                    # 2. Свойства первого Run (Жирность, Размер) - Direct Formatting
                    is_bold_xml = False
                    font_size_xml = "Inherited"
                    
                    runs = p.xpath('.//w:r', namespaces=ns)
                    if runs:
                        rPr = runs[0].find('w:rPr', namespaces=ns)
                        if rPr is not None:
                            # Жирность (<w:b/> существует?)
                            if rPr.find('w:b', namespaces=ns) is not None:
                                is_bold_xml = True
                            # Размер (<w:sz w:val="24"/>)
                            sz = rPr.find('w:sz', namespaces=ns)
                            if sz is not None:
                                val = sz.get(f"{{{ns['w']}}}val")
                                if val and val.isdigit():
                                    font_size_xml = str(int(val) / 2) # XML хранит в пол-пунктах

                    features.append({
                        "text_snippet": full_text[:50], # Для сверки
                        "full_text_len": len(full_text),
                        "xml_style": style_id,
                        "xml_bold": is_bold_xml,
                        "xml_size": font_size_xml
                    })
        except Exception as e:
            print(f"XML Parsing Error in {docx_path}: {e}")
            
        return features

# --- 2. CORE VALIDATOR LOGIC ---
class WorkflowValidator:
    def __init__(self):
        self.xml_parser = XmlFeatureExtractor()
    
    def normalize_style(self, name):
        """Приводит названия стилей к общему виду для сравнения"""
        if not name: return "normal"
        return str(name).lower().replace(" ", "").replace("heading", "heading").replace("title", "title")

    def run_validation(self):
        docx_files = glob.glob(os.path.join(TEST_DOCS_DIR, "*.docx"))
        if not docx_files:
            print("❌ Нет файлов для тестов!")
            return

        all_diffs = []

        print(f"🚀 Запуск валидации на {len(docx_files)} файлах...")

        for file_path in docx_files:
            filename = os.path.basename(file_path)
            
            # A. GROUND TRUTH (XML)
            xml_data = self.xml_parser.extract(file_path)
            
            # B. RAG PREDICTION (StyleExtractor)
            # ВАЖНО: StyleExtractor возвращает список словарей
            rag_data = style_extractor.parse_docx(file_path)
            
            # Фильтруем RAG данные, чтобы убрать <PAGE_BREAK> и пустые, 
            # чтобы списки совпали по длине (синхронизация)
            rag_data_clean = [x for x in rag_data if x['text'] != "<PAGE_BREAK>" and x['text'].strip()]

            # C. DIFFING (Сравнение)
            # Мы идем по минимальной длине, предполагая, что порядок параграфов сохранен
            limit = min(len(xml_data), len(rag_data_clean))
            
            print(f"📄 {filename}: XML нашёл {len(xml_data)} блоков, RAG нашёл {len(rag_data_clean)}. Сравниваем {limit}...")

            file_errors = []

            for i in range(limit):
                xml_item = xml_data[i]
                rag_item = rag_data_clean[i]
                rag_meta = rag_item['metadata']

                # --- ПРАВИЛА СРАВНЕНИЯ ---
                
                # 1. Проверка стиля
                s_xml = self.normalize_style(xml_item['xml_style'])
                s_rag = self.normalize_style(rag_meta['style_name'])
                
                # RAG должен распознать заголовок, если в XML это Heading
                # Или если RAG сам решил, что это Header (по эвристике размера)
                is_error = False
                error_type = ""

                # Логика ошибки: В XML это заголовок, а RAG говорит Normal
                if "heading" in s_xml and "heading" not in s_rag and not rag_meta['is_header']:
                    is_error = True
                    error_type = "Missed Header"
                
                # Логика ошибки: В XML это Normal, а RAG придумал Heading (хотя шрифт мелкий)
                elif "normal" in s_xml and "heading" in s_rag:
                    # Это может быть не ошибкой, если сработала эвристика размера!
                    # Проверяем эвристику:
                    if not rag_meta['is_header']: # Если RAG пометил это как Header только по имени стиля, которого нет
                         is_error = True
                         error_type = "Hallucinated Header"

                # Сохраняем результат
                all_diffs.append({
                    "file": filename,
                    "index": i,
                    "text": xml_item['text_snippet'],
                    "xml_style": xml_item['xml_style'],
                    "rag_style": rag_meta['style_name'],
                    "rag_is_header_flag": rag_meta['is_header'],
                    "status": "FAIL" if is_error else "PASS",
                    "error_type": error_type
                })
                
                if is_error:
                    file_errors.append(i) # Запоминаем индекс абзаца с ошибкой

            # D. REPORTING (Визуализация ошибок в PDF)
            if file_errors:
                self.create_error_pdf(file_path, file_errors, rag_data_clean, filename)

        # Сохранение CSV
        df = pd.DataFrame(all_diffs)
        csv_path = os.path.join(REPORTS_DIR, f"validation_benchmark_{datetime.now().strftime('%H%M')}.csv")
        df.to_csv(csv_path, index=False)
        
        # Консольный отчет
        error_count = len(df[df['status'] == 'FAIL'])
        total_count = len(df)
        print("\n=== РЕЗУЛЬТАТЫ ВАЛИДАЦИИ ===")
        print(f"Всего проверено параграфов: {total_count}")
        print(f"Ошибок найдено: {error_count}")
        print(f"Точность (Accuracy): {((total_count - error_count) / total_count * 100):.2f}%")
        print(f"Отчет сохранен: {csv_path}")
        print(f"PDF-рентгены с ошибками сохранены в: {REPORTS_DIR}")

    def create_error_pdf(self, original_path, error_indices, rag_data, filename):
        """Создает PDF, где ПОДСВЕЧЕНЫ КРАСНЫМ только ошибки"""
        try:
            doc = docx.Document(original_path)
            
            # Нужно синхронизироваться. Это сложно в docx напрямую, 
            # поэтому мы идем по счетчику непустых параграфов
            non_empty_idx = 0
            
            for para in doc.paragraphs:
                if not para.text.strip(): continue
                
                # Если этот индекс есть в списке ошибок
                if non_empty_idx in error_indices:
                    # Подсвечиваем текст красным фоном
                    for run in para.runs:
                        run.font.highlight_color = WD_COLOR_INDEX.RED
                    
                    # Добавляем комментарий RAG vs XML
                    rag_info = rag_data[non_empty_idx]
                    style_guess = rag_info['metadata']['style_name']
                    is_header = rag_info['metadata']['is_header']
                    
                    msg = f"[ERROR] RAG thought: {style_guess} (Header={is_header})"
                    
                    # Вставляем пометку
                    p_new = para.insert_paragraph_before(msg)
                    for r in p_new.runs: 
                        r.font.size = Pt(9)
                        r.font.bold = True
                        r.font.color.rgb = RGBColor(255, 0, 0)
                
                non_empty_idx += 1
                
            # Сохраняем и конвертируем
            temp_docx = os.path.join(REPORTS_DIR, f"FAILURES_{filename}")
            doc.save(temp_docx)
            
            subprocess.run([
                "soffice", "--headless", "--convert-to", "pdf",
                temp_docx, "--outdir", REPORTS_DIR
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # os.remove(temp_docx)
            
        except Exception as e:
            print(f"Не удалось создать PDF отчет для {filename}: {e}")

if __name__ == "__main__":
    validator = WorkflowValidator()
    validator.run_validation()