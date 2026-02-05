import os
import hashlib
import docx
import inspect
from docx.document import Document
from docx.text.font import Font
from docx.text.parfmt import ParagraphFormat
from collections import Counter
from typing import List, Dict, Any

# --- НАСТРОЙКА ПУТЕЙ ---
# Создаем папку для картинок, если её нет
MEDIA_ROOT = os.path.join(os.getcwd(), "data", "media")
os.makedirs(MEDIA_ROOT, exist_ok=True)

class StyleIntrospector:
    def __init__(self):
        self.blacklist = {
            'part', 'parent', 'element', 'file', 'msg', 'sql', 
            'date', 'time', 'hidden', 'shadow', 'outline', 
            'engrave', 'emboss', 'imprint', 'no_proof', 'web_hidden',
            'snap_to_grid', 'contextual_spacing'
        }
        self.font_props = self._discover_properties(Font)
        self.para_props = self._discover_properties(ParagraphFormat)
        
        self.aliases = {
            "name": "F", "size": "P", "bold": "B", "italic": "I", 
            "underline": "U", "strike": "STRIKE", 
            "color": "C", "all_caps": "CAPS", "small_caps": "SCAPS",
            "alignment": "A", 
            "first_line_indent": "IND-F", "left_indent": "IND-L", "right_indent": "IND-R",
            "space_before": "SB", "space_after": "SA", "line_spacing": "LS",
            "keep_together": "KEEP", "keep_with_next": "KWN", "page_break_before": "PG",
            "widow_control": "WIDOW"
        }

    def _discover_properties(self, cls):
        props = []
        for name, kind in inspect.getmembers(cls):
            if name.startswith("_"): continue
            if name in self.blacklist: continue
            if isinstance(kind, property):
                props.append(name)
        return props

    def get_all_formatting_keys(self):
        keys = ["S"] 
        for p in self.font_props:
            key = self.aliases.get(p, f"font.{p}")
            if key not in keys: keys.append(key)
        for p in self.para_props:
            key = self.aliases.get(p, f"para.{p}")
            if key not in keys: keys.append(key)
        if "CAPS" not in keys: keys.append("CAPS")
        return keys

class StyleExtractor:
    def __init__(self):
        self.schema = StyleIntrospector()

    def _safe_val(self, obj, prop_name):
        try:
            val = getattr(obj, prop_name, None)
            if val is None: return None
            
            if prop_name == 'color':
                if hasattr(val, 'rgb') and val.rgb: return str(val.rgb)
                return None

            if hasattr(val, 'pt'): return round(val.pt, 1) 
            if isinstance(val, float): return round(val, 1)
            
            if not isinstance(val, (str, int, float, bool)):
                return str(val) if val else None

            return val
        except Exception:
            return None

    def _resolve_inheritance(self, para, prop_name, is_font=False):
        """Deep Style Resolution с жесткими дефолтами"""
        
        # 1. Direct Formatting
        if is_font:
            direct_val = self._analyze_runs_direct(para, prop_name)
        else:
            direct_val = self._safe_val(para.paragraph_format, prop_name)
            
        if direct_val is not None:
            return direct_val

        # 2. Hierarchy
        current_style = para.style
        depth = 0
        while current_style and depth < 5:
            try:
                if is_font: style_obj = current_style.font
                else: style_obj = current_style.paragraph_format
                
                val = self._safe_val(style_obj, prop_name)
                if val is not None: return val
                
                current_style = getattr(current_style, 'base_style', None)
                depth += 1
            except: break
                
        # 3. HARD DEFAULTS
        if prop_name == 'name': return "Calibri"
        if prop_name == 'size': return 11.0
        if prop_name == 'alignment': return 0
        if prop_name == 'line_spacing': return 1.0
        if prop_name in ['bold', 'italic', 'underline', 'all_caps', 'small_caps', 'strike']: return False
        if prop_name in ['space_before', 'space_after', 'left_indent', 'right_indent', 'first_line_indent']: return 0.0
        
        return None

    def _analyze_runs_direct(self, para, prop_name):
        if not para.runs: return None
        
        values = []
        for run in para.runs:
            val = self._safe_val(run.font, prop_name)
            if val is not None:
                values.append(val)
        
        if not values: return None
        
        if prop_name == "size": return max(values)
        if prop_name in ["bold", "italic", "underline", "all_caps", "small_caps", "strike"]:
            return any(v is True for v in values) if values else None
        
        try: return Counter(values).most_common(1)[0][0]
        except: return str(values[0])

    def _extract_images_from_para(self, para, doc_part):
        """
        Ищет картинки (blip) внутри XML параграфа и сохраняет их на диск.
        Возвращает список токенов [MEDIA: filename].
        """
        img_tokens = []
        
        # Лезем в XML параграфа (namespace w:drawing)
        if 'w:drawing' in para._element.xml:
            # Ищем все ссылки на rId (relationship ID)
            for rId in para._element.xpath('.//a:blip/@r:embed'):
                try:
                    # Получаем бинарную часть по rId
                    if rId in doc_part.related_parts:
                        image_part = doc_part.related_parts[rId]
                        image_blob = image_part.blob
                        content_type = image_part.content_type
                        
                        # Генерируем имя файла
                        ext = "png" if "png" in content_type else "jpg"
                        img_hash = hashlib.md5(image_blob).hexdigest()
                        filename = f"{img_hash}.{ext}"
                        filepath = os.path.join(MEDIA_ROOT, filename)
                        
                        # Сохраняем на диск, если еще нет
                        if not os.path.exists(filepath):
                            with open(filepath, "wb") as f:
                                f.write(image_blob)
                        
                        img_tokens.append(f"[MEDIA: {filename}]")
                except Exception:
                    continue
                    
        return " ".join(img_tokens)

    def parse_docx(self, file_path: str) -> List[Dict[str, Any]]:
        doc = docx.Document(file_path)
        chunks = []

        for i, para in enumerate(doc.paragraphs):
            text = para.text.strip()
            
            # 1. Ищем картинки
            media_tag = self._extract_images_from_para(para, doc.part)
            
            # Безопасная проверка page break
            page_break = False
            if para.runs:
                for run in para.runs:
                    b_type = getattr(run, 'break_type', None)
                    if b_type and 'PAGE' in str(b_type):
                        page_break = True
                        break

            if not text and not page_break and not media_tag: continue

            # Сбор данных
            para_data = {}
            for prop in self.schema.para_props:
                val = self._resolve_inheritance(para, prop, is_font=False)
                if val is not None: para_data[prop] = val

            font_data = {}
            for prop in self.schema.font_props:
                val = self._resolve_inheritance(para, prop, is_font=True)
                if val is not None: font_data[prop] = val

            if text.isupper() and len(text) > 3: font_data['all_caps'] = True

            # ГЕНЕРАЦИЯ СТРОКИ
            style_parts = [f"[S: {para.style.name}]"]
            
            # Добавляем MEDIA токен
            if media_tag:
                style_parts.append(media_tag)
            
            for prop, val in font_data.items():
                key = self.schema.aliases.get(prop, f"font.{prop}")
                style_parts.append(f"[{key}: {val}]")

            for prop, val in para_data.items():
                if val is None: continue
                key = self.schema.aliases.get(prop, f"para.{prop}")
                style_parts.append(f"[{key}: {val}]")

            style_desc = " ".join(style_parts)
            
            # Плейсхолдер для текста, если есть только картинка
            if not text and media_tag:
                text = "<IMAGE_PLACEHOLDER>"

            # Эвристики
            section_type = "body"
            font_size = font_data.get('size', 11)
            is_bold = font_data.get('bold', False)
            
            if "Heading" in para.style.name: section_type = "header"
            elif isinstance(font_size, (int, float)) and font_size >= 14: section_type = "header"
            elif is_bold and len(text.split()) < 10 and not text.endswith("."): section_type = "header"

            chunks.append({
                "text": text,
                "semantic_tag": "SECTION_HEADER " if section_type == "header" else "",
                "style_desc": style_desc,
                "metadata": {
                    "style_name": para.style.name,
                    "has_image": bool(media_tag),
                    "is_header": section_type == "header",
                    "section_type": section_type,
                    "source_idx": i
                }
            })
        return chunks

style_extractor = StyleExtractor()