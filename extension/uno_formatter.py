import uno
from com.sun.star.style.ParagraphAdjust import CENTER, LEFT, RIGHT, BLOCK
from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK
from com.sun.star.beans import PropertyValue

class UnoFormatter:
    def __init__(self, ctx):
        self.ctx = ctx
        self.desktop = ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.frame.Desktop", ctx)
        self.doc = self.desktop.getCurrentComponent()
        self.text = self.doc.Text
        self.cursor = self.doc.CurrentController.getViewCursor()

    def import_styles_from_template(self, template_path):
        try:
            url = uno.systemPathToFileUrl(template_path)
            self.doc.StyleFamilies.loadStylesFromURL(url, [PropertyValue(Name="OverwriteStyles", Value=True)])
        except: pass

    def _apply_direct_formatting(self, cursor, block_data):
        try:
            # Шрифт
            if "font_family" in block_data:
                cursor.CharFontName = block_data["font_family"]
            if "font_size" in block_data:
                try: cursor.CharHeight = float(block_data["font_size"])
                except: pass
            
            # Жирность / Курсив
            if block_data.get("bold") is True: cursor.CharWeight = 150.0
            if block_data.get("italic") is True: cursor.CharPosture = 1

            # Выравнивание
            align = block_data.get("align", "left").lower()
            if align == "center": cursor.ParaAdjust = CENTER
            elif align == "right": cursor.ParaAdjust = RIGHT
            elif align in ["justify", "block"]: cursor.ParaAdjust = BLOCK
            else: cursor.ParaAdjust = LEFT
        except: pass

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
                            
                            text_str = ""
                            if isinstance(content, dict):
                                text_str = content.get("text", content.get("content", ""))
                            else:
                                text_str = str(content)
                            
                            # Чистим артефакты
                            text_str = text_str.replace("&nbsp;", " ").replace("**", "").replace("##", "")
                            cell.setString(text_str)
            
            self.cursor.gotoEnd(False)
        except Exception as e:
            print(f"Table Error: {e}")

    def apply_structure(self, json_structure):
        selection = self.doc.CurrentController.getSelection()
        if selection.getCount() > 0:
            cursor = self.text.createTextCursorByRange(selection.getByIndex(0))
        else:
            cursor = self.text.createTextCursor()
            cursor.gotoStart(False)
            cursor.gotoEnd(True)

        # Очищаем старое
        cursor.setString("") 
        
        for block in json_structure:
            if not isinstance(block, dict): continue
            
            # --- ЭВРИСТИКА: Угадываем тип, если LLM ошиблась ---
            block_type = block.get("type", "paragraph")
            
            # Если есть 'level', но нет типа -> это header
            if "level" in block and block_type == "paragraph":
                block_type = "header"
            
            # Если есть 'tableRows' -> это таблица
            if "tableRows" in block:
                block_type = "table"
                block["data"] = block["tableRows"]
                block["rows"] = len(block["data"])
                block["cols"] = len(block["data"][0]) if block["rows"] > 0 else 0

            # ----------------------------------------------------

            text_content = block.get("text", block.get("content", ""))
            
            # Чистка текста от Markdown-мусора (**text**, ## text)
            if text_content:
                import re
                # Убираем **bold**, ## header markers, &nbsp;
                text_content = text_content.replace("&nbsp;", " ")
                text_content = re.sub(r'\*\*(.*?)\*\*', r'\1', text_content) # Bold remove
                text_content = re.sub(r'^#+\s*', '', text_content)           # Header remove

            if block_type == "table":
                rows = block.get("rows", len(block.get("data", [])))
                cols = block.get("cols", 1)
                # Если cols не задан, пытаемся посчитать
                if cols == 1 and block.get("data") and len(block["data"]) > 0:
                     cols = len(block["data"][0])
                
                self.insert_table(rows, cols, block.get("data", []))
            
            elif block_type == "page_break":
                cursor.BreakType = uno.getConstantByName("com.sun.star.style.BreakType.PAGE_BEFORE")
            
            elif block_type == "header":
                try: 
                    level = block.get("level", 1)
                    cursor.ParaStyleName = f"Heading {level}"
                except: pass
                
                if text_content: self.text.insertString(cursor, text_content, False)
                # Форматирование
                temp_cursor = self.text.createTextCursorByRange(cursor)
                temp_cursor.goLeft(len(text_content), True)
                self._apply_direct_formatting(temp_cursor, block)
                
                self.text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)

            else:
                # Paragraph
                if text_content:
                    self.text.insertString(cursor, text_content, False)
                    temp_cursor = self.text.createTextCursorByRange(cursor)
                    temp_cursor.goLeft(len(text_content), True)
                    self._apply_direct_formatting(temp_cursor, block)
                
                self.text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)