import uno
from com.sun.star.style.ParagraphAdjust import CENTER, LEFT, RIGHT, BLOCK
from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK
from com.sun.star.beans import PropertyValue
import re

class UnoFormatter:
    def __init__(self, ctx):
        self.ctx = ctx
        self.desktop = ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.frame.Desktop", ctx)
        self.doc = self.desktop.getCurrentComponent()
        self.text = self.doc.Text
        self.cursor = self.doc.CurrentController.getViewCursor()

    def import_styles_from_template(self, template_path):
        """Загрузка стилей из внешнего .odt/.ott (если потребуется)"""
        try:
            url = uno.systemPathToFileUrl(template_path)
            self.doc.StyleFamilies.loadStylesFromURL(url, [PropertyValue(Name="OverwriteStyles", Value=True)])
        except: pass

    def _apply_direct_formatting(self, cursor, block_data):
        """Применяет жесткое форматирование, если стиль не сработал или нужна детализация"""
        try:
            # 1. Шрифт (Deep Style mapping)
            if "font_family" in block_data:
                cursor.CharFontName = block_data["font_family"]
            
            if "font_size" in block_data:
                try: 
                    # UNO ожидает высоту, иногда нужно умножать, но в python-uno обычно передается float
                    cursor.CharHeight = float(block_data["font_size"])
                except: pass
            
            # 2. Начертание
            if block_data.get("bold") is True: 
                cursor.CharWeight = 150.0 # BOLD
            if block_data.get("italic") is True: 
                cursor.CharPosture = 1    # ITALIC

            # 3. Выравнивание
            align = block_data.get("align", "left").lower()
            if align == "center": cursor.ParaAdjust = CENTER
            elif align == "right": cursor.ParaAdjust = RIGHT
            elif align in ["justify", "block"]: cursor.ParaAdjust = BLOCK
            else: cursor.ParaAdjust = LEFT
            
        except Exception as e:
            print(f"Formatting Error: {e}")

    def insert_table(self, rows, cols, data=None):
        try:
            if rows < 1 or cols < 1: return

            table = self.doc.createInstance("com.sun.star.text.TextTable")
            table.initialize(rows, cols)
            self.text.insertTextContent(self.cursor, table, False)
            
            if data:
                for r in range(rows):
                    for c in range(cols):
                        if r < len(data) and c < len(data[r]):
                            cell = table.getCellByPosition(c, r)
                            content = data[r][c]
                            
                            # Извлекаем текст, если ячейка - это объект
                            text_str = ""
                            if isinstance(content, dict):
                                text_str = content.get("text", content.get("content", ""))
                            else:
                                text_str = str(content)
                            
                            # Чистим артефакты Markdown
                            text_str = text_str.replace("&nbsp;", " ").replace("**", "").replace("##", "")
                            cell.setString(text_str)
            
            self.cursor.gotoEnd(False)
        except Exception as e:
            print(f"Table Error: {e}")

    def insert_image_placeholder(self, cursor, filename):
        """Вставляет заглушку для картинки, которую предложил RAG"""
        try:
            # Вставляем текстовый плейсхолдер
            msg = f"[MEDIA PLACEHOLDER: {filename}]"
            self.text.insertString(cursor, msg, False)
            
            # Красим его в серый цвет, чтобы отличался
            temp_cursor = self.text.createTextCursorByRange(cursor)
            temp_cursor.goLeft(len(msg), True)
            temp_cursor.CharColor = 8421504 # Grey
            temp_cursor.ParaAdjust = CENTER
            
            self.text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
        except: pass

    def apply_structure(self, json_structure):
        """Главный метод применения верстки"""
        
        # 1. Определяем область действия (выделение или весь документ)
        selection = self.doc.CurrentController.getSelection()
        if selection.getCount() > 0:
            cursor = self.text.createTextCursorByRange(selection.getByIndex(0))
        else:
            cursor = self.text.createTextCursor()
            cursor.gotoStart(False)
            cursor.gotoEnd(True)

        # 2. Очищаем старый текст
        cursor.setString("") 
        
        for block in json_structure:
            if not isinstance(block, dict): continue
            
            block_type = block.get("type", "paragraph")
            
            # Эвристика исправления типов
            if "level" in block and block_type == "paragraph": block_type = "header"
            if "tableRows" in block: block_type = "table"; block["data"] = block["tableRows"]

            # Очистка текста
            text_content = block.get("text", block.get("content", ""))
            if text_content:
                # Убираем Markdown жирность, так как применим стиль программно
                text_content = re.sub(r'\*\*(.*?)\*\*', r'\1', text_content)
                text_content = re.sub(r'^#+\s*', '', text_content)

            # --- ВЕТВЛЕНИЕ ПО ТИПАМ ---
            
            if block_type == "table":
                rows = block.get("rows", len(block.get("data", [])))
                cols = block.get("cols", 1)
                # Если cols не задан явно, считаем по первой строке
                if cols == 1 and block.get("data") and len(block["data"]) > 0:
                     cols = len(block["data"][0])
                self.insert_table(rows, cols, block.get("data", []))
            
            elif block_type == "image":
                # RAG может вернуть имя файла или описание
                self.insert_image_placeholder(cursor, text_content or "Image")

            elif block_type == "page_break":
                cursor.BreakType = uno.getConstantByName("com.sun.star.style.BreakType.PAGE_BEFORE")
            
            elif block_type == "header":
                # Применяем стиль заголовка (Heading 1-N)
                try: 
                    # Попытка найти уровень, по умолчанию 1
                    level = block.get("level", 1)
                    # Если RAG прислал [S: Heading 2], пробуем использовать имя стиля напрямую
                    style_name = block.get("style_name", f"Heading {level}")
                    cursor.ParaStyleName = style_name
                except: pass
                
                if text_content: self.text.insertString(cursor, text_content, False)
                
                # Применяем Deep Style (шрифт, размер) поверх стиля
                temp_cursor = self.text.createTextCursorByRange(cursor)
                temp_cursor.goLeft(len(text_content), True)
                self._apply_direct_formatting(temp_cursor, block)
                
                self.text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)

            else:
                # Обычный параграф
                if block.get("style_name"):
                    try: cursor.ParaStyleName = block["style_name"]
                    except: pass
                
                if text_content:
                    self.text.insertString(cursor, text_content, False)
                    temp_cursor = self.text.createTextCursorByRange(cursor)
                    temp_cursor.goLeft(len(text_content), True)
                    self._apply_direct_formatting(temp_cursor, block)
                
                self.text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)