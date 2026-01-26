import uno
from com.sun.star.style.ParagraphAdjust import CENTER, LEFT, RIGHT, BLOCK
from com.sun.star.beans import PropertyValue
from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK

class UnoFormatter:
    def __init__(self, ctx):
        self.ctx = ctx
        self.desktop = ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.frame.Desktop", ctx)
        self.doc = self.desktop.getCurrentComponent()
        self.text = self.doc.Text
        self.cursor = self.doc.CurrentController.getViewCursor()

    def import_styles_from_template(self, template_path):
        """Загружает стили из файла-шаблона."""
        try:
            url = uno.systemPathToFileUrl(template_path)
            style_families = self.doc.StyleFamilies
            # Опции загрузки: Overwrite (перезаписать)
            load_options = (
                PropertyValue(Name="OverwriteStyles", Value=True),
            )
            style_families.loadStylesFromURL(url, load_options)
            print(f"Styles loaded from {template_path}")
            return True
        except Exception as e:
            print(f"Error loading styles: {e}")
            return False

    def insert_toc(self):
        try:
            toc = self.doc.createInstance("com.sun.star.text.ContentIndex")
            toc.CreateFromOutline = True
            toc.Title = "ОГЛАВЛЕНИЕ"
            self.text.insertTextContent(self.cursor, toc, False)
        except: pass

    def insert_page_number(self):
        try:
            field = self.doc.createInstance("com.sun.star.text.TextField.PageNumber")
            field.NumberingType = 4 
            self.text.insertTextContent(self.cursor, field, False)
        except: pass

    def _safe_set_style(self, cursor, style_name, fallback="Standard"):
        """Безопасная установка стиля. Если стиля нет, ставит Standard."""
        try:
            cursor.ParaStyleName = style_name
        except:
            # Если упало, пробуем fallback (обычно "Standard" есть всегда)
            try:
                cursor.ParaStyleName = fallback
            except:
                pass # Если даже Standard нет, ничего не делаем

    def apply_structure(self, json_structure):
        cursor = self.text.createTextCursorByRange(self.cursor)
        
        for block in json_structure:
            if not isinstance(block, dict): continue
            
            text_content = block.get("text", "")
            block_type = block.get("type", "paragraph")
            style_name = block.get("style", "")
            
            # 1. ЗАГОЛОВКИ
            if block_type == "header":
                level = block.get("level", 1)
                # Heading 1, Heading 2... (Это внутренние имена, они работают везде)
                self._safe_set_style(cursor, f"Heading {level}")
                if text_content: self.text.insertString(cursor, text_content, False)
            
            # 2. ПАРАГРАФЫ
            elif block_type == "paragraph":
                # Приоритет: стиль от LLM -> Text Body -> Standard
                target_style = style_name if style_name else "Text Body"
                self._safe_set_style(cursor, target_style, fallback="Standard")
                
                align = block.get("align", "left")
                if align == "center": cursor.ParaAdjust = CENTER
                elif align == "right": cursor.ParaAdjust = RIGHT
                elif align == "justify": cursor.ParaAdjust = BLOCK
                
                if text_content: self.text.insertString(cursor, text_content, False)

            # 3. СПЕЦ. ЭЛЕМЕНТЫ
            elif block_type == "toc": 
                self.insert_toc()
            elif block_type == "page_break":
                cursor.BreakType = uno.getConstantByName("com.sun.star.style.BreakType.PAGE_BEFORE")
            elif block_type == "field":
                if block.get("kind") == "page_number": self.insert_page_number()

            # Перенос строки
            self.text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)