import sys
import os
import uno
import unohelper
import officehelper
import json
import traceback
import datetime
import threading
import queue
import time
import urllib.parse

from com.sun.star.task import XJobExecutor
from com.sun.star.awt import XActionListener, XWindowListener
from com.sun.star.lang import EventObject

# XTimeoutListener нужно извлекать динамически, прямой импорт часто падает в UNO
try:
    XTimeoutListener = uno.getClass("com.sun.star.awt.XTimeoutListener")
except:
    XTimeoutListener = None

# === ИМПОРТЫ ===
# LibreOffice Python loader does not always add the extension folder to sys.path
_cur_dir = os.path.dirname(os.path.abspath(__file__))
if _cur_dir not in sys.path:
    sys.path.insert(0, _cur_dir)

import client as lw_client
from uno_formatter import UnoFormatter
from tracer import ExecutionTracer

def log_to_file(message, error=None):
    log_file_path = "/tmp/localwriter.log"
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    if error: 
        log_entry += f"\nERROR: {str(error)}\n{traceback.format_exc()}"
    try:
        with open(log_file_path, "a", encoding="utf-8") as f: f.write(log_entry + "\n")
    except: pass


# === SETTINGS UI ===
class SettingsDialogHandler(unohelper.Base, XActionListener):
    def __init__(self, ctx, current_config):
        self.ctx = ctx
        self.config = current_config
        self.dialog = None
        self.result = {}

    def show(self):
        smgr = self.ctx.getServiceManager()
        self.dialog = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", self.ctx)
        model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", self.ctx)
        self.dialog.setModel(model)
        
        model.Title = "LocalWriter Configuration"
        model.PositionX = 100
        model.PositionY = 100
        model.Width = 260
        model.Height = 170
        
        self._add_label("lbl_mid", "Middleware URL (Backend):", 10, 10, 240, 10)
        self._add_edit("txt_mid", self.config.get("middleware_url", "http://localhost:8323"), 10, 22, 240, 15)
        
        self._add_button("btn_check", "🔄 Check Connection & Fetch Models", 10, 47, 240, 20, "CheckConn")

        self._add_label("lbl_model", "Select Model:", 10, 77, 240, 10)
        current_model = self.config.get("model", "")
        self._add_combo("cb_model", current_model, 10, 89, 240, 15)

        self._add_button("btn_ok", "Save Settings", 90, 140, 70, 20, "OK", True)
        self._add_button("btn_cancel", "Cancel", 170, 140, 50, 20, "Cancel")
        
        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", self.ctx)
        self.dialog.createPeer(toolkit, None)
        self.dialog.execute()
        self.dialog.dispose()
        return self.result

    def _add_label(self, name, label, x, y, w, h):
        m = self.dialog.Model.createInstance("com.sun.star.awt.UnoControlFixedTextModel")
        m.Name = name; m.Label = label; m.PositionX = x; m.PositionY = y; m.Width = w; m.Height = h
        self.dialog.Model.insertByName(name, m)

    def _add_edit(self, name, text, x, y, w, h):
        m = self.dialog.Model.createInstance("com.sun.star.awt.UnoControlEditModel")
        m.Name = name; m.Text = text; m.PositionX = x; m.PositionY = y; m.Width = w; m.Height = h
        self.dialog.Model.insertByName(name, m)

    def _add_combo(self, name, text, x, y, w, h):
        m = self.dialog.Model.createInstance("com.sun.star.awt.UnoControlComboBoxModel")
        m.Name = name; m.Text = text; m.PositionX = x; m.PositionY = y; m.Width = w; m.Height = h
        m.Dropdown = True
        self.dialog.Model.insertByName(name, m)

    def _add_button(self, name, label, x, y, w, h, action_command=None, default=False):
        m = self.dialog.Model.createInstance("com.sun.star.awt.UnoControlButtonModel")
        m.Name = name; m.Label = label; m.PositionX = x; m.PositionY = y; m.Width = w; m.Height = h
        if default: m.DefaultButton = True
        self.dialog.Model.insertByName(name, m)
        control = self.dialog.getControl(name)
        control.setActionCommand(action_command)
        control.addActionListener(self)

    def _msg_box(self, message, title="Info"):
        try:
            toolkit = self.ctx.getServiceManager().createInstanceWithContext("com.sun.star.awt.Toolkit", self.ctx)
            msgbox = toolkit.createMessageBox(None, "messbox", 1, title, str(message))
            msgbox.execute()
        except: pass

    def actionPerformed(self, e):
        cmd = e.ActionCommand
        
        # --- SAFE CHECK CONNECTION HANDLER ---
        if cmd == "CheckConn":
            try:
                mid_url = self.dialog.getControl("txt_mid").getText().rstrip('/')

                # HTTP-вызов через client.py
                models = lw_client.check_connection(mid_url, timeout=5)

                if models:
                    cb = self.dialog.getControl("cb_model")
                    cb.getModel().StringItemList = tuple(models)
                    curr = cb.getText()
                    if curr not in models:
                        cb.setText(models[0])
                    self._msg_box(f"✅ Connection Successful!\nLoaded {len(models)} models.", "Success")
                else:
                    self._msg_box("⚠️ Connection OK, but no models found.", "Warning")

            except Exception as ex:
                log_to_file("Check Connection Failed", ex)
                # Показываем ошибку пользователю
                self._msg_box(f"❌ Connection Failed:\n{str(ex)}", "Error")

        elif cmd == "OK":
            self.result = {
                "middleware_url": self.dialog.getControl("txt_mid").getText(),
                "model": self.dialog.getControl("cb_model").getText()
            }
            self.dialog.endExecute()
            
        elif cmd == "Cancel":
            self.result = {}
            self.dialog.endExecute()

    def disposing(self, e): pass

# === PROGRESS DIALOG ===
class ProgressDialogHandler(unohelper.Base, XActionListener):
    def __init__(self, ctx, cancel_event):
        self.ctx = ctx
        self.cancel_event = cancel_event
        self.dialog = None
        self.toolkit = None
    
    def create(self):
        smgr = self.ctx.getServiceManager()
        self.dialog = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", self.ctx)
        model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", self.ctx)
        self.dialog.setModel(model)
        
        model.Title = "AI Formatting in Progress..."
        model.PositionX = 100
        model.PositionY = 100
        model.Width = 200
        model.Height = 80
        
        # Label
        lbl = model.createInstance("com.sun.star.awt.UnoControlFixedTextModel")
        lbl.Name = "lblStatus"
        lbl.Label = "Generating formatting... Please wait.\nDo not edit document manually."
        lbl.PositionX = 10; lbl.PositionY = 15; lbl.Width = 180; lbl.Height = 20
        model.insertByName("lblStatus", lbl)
        
        # Button Cancel
        btn = model.createInstance("com.sun.star.awt.UnoControlButtonModel")
        btn.Name = "btnCancel"
        btn.Label = "Cancel"
        btn.PositionX = 65; btn.PositionY = 45; btn.Width = 70; btn.Height = 20
        model.insertByName("btnCancel", btn)
        self.dialog.getControl("btnCancel").setActionCommand("Cancel")
        self.dialog.getControl("btnCancel").addActionListener(self)
        
        self.toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", self.ctx)
        self.dialog.createPeer(self.toolkit, None)
        return self.dialog
        
    def update_status(self, text):
        if self.dialog:
            try:
                self.dialog.getControl("lblStatus").setText(text)
            except: pass

    def actionPerformed(self, e):
        if e.ActionCommand == "Cancel":
            self.cancel_event.set()
            self.update_status("Canceling... Waiting for backend to abort.")
            
    def disposing(self, e): pass


# === TIMER LISTENER (POLLING QUEUE) ===
# В UNO (LibreOffice Python) достаточно наследоваться от unohelper.Base 
# и реализовать нужный метод timeout, явное наследование от XTimeoutListener вызывает metaclass conflict.
class ApplyTemplateTimerListener(unohelper.Base):
    def __init__(self, main_job, queue_obj, cancel_event, dialog_handler, bookmark_name, doc, formatter):
        self.main_job = main_job
        self.queue = queue_obj
        self.cancel_event = cancel_event
        self.dialog_handler = dialog_handler
        self.bookmark_name = bookmark_name
        self.doc = doc
        self.formatter = formatter
        self.chunk_count = 0
        self.is_finished = False
        
        # Очищаем закладку при старте (первый чанк затирает выделение)
        self.first_chunk_received = False
        
        # Включаем Undo Manager
        try:
            self.undo_manager = self.doc.getUndoManager()
            self.undo_manager.enterUndoContext("AI Formatting")
        except:
            self.undo_manager = None
        
    def timeout(self, timer_event):
        """Вызывается XTimer'ом каждый тик в UI-потоке LibreOffice"""
        if self.is_finished:
            return
            
        try:
            # Читаем ВСЕ доступные сообщения в очереди (batch processing frame)
            while not self.queue.empty():
                item = self.queue.get_nowait()
                
                if "error" in item:
                    self.main_job.msg_box(f"AI Error:\n{item['error']}", "Error")
                    self._finish()
                    return
                    
                if "DONE" in item:
                    # Успех
                    self._finish()
                    return
                
                # Обработка готового параграфа JSON
                self._process_chunk(item)
                
            # Позволяем GUI обновиться
            if self.dialog_handler.toolkit:
                self.dialog_handler.toolkit.processEventsToIdle()
                
        except queue.Empty:
            pass
        except Exception as e:
            log_to_file("Timer polling error", e)
            self.main_job.msg_box(f"Error applying style: {str(e)}", "Error")
            self._finish()

    def _process_chunk(self, block_data):
        self.chunk_count += 1
        self.dialog_handler.update_status(f"Applying AI styling...\nParagraphs processed: {self.chunk_count}")
        
        # Получаем закладку по ID параграфа из JSON
        p_id = block_data.get("id")
        if p_id is None:
            log_to_file(f"Warning: No ID in block_data: {block_data}")
            return
            
        target_bookmark = f"{self.bookmark_name}_p{p_id}"
        
        try:
            bookmarks = self.doc.getBookmarks()
            if not bookmarks.hasByName(target_bookmark):
                log_to_file(f"Bookmark {target_bookmark} not found.")
                return
            bookmark = bookmarks.getByName(target_bookmark)
            anchor = bookmark.getAnchor()
        except Exception as e:
            log_to_file(f"Bookmark retrieval error: {e}")
            return

        # Курсор на выделение всей закладки (абзаца), чтобы заменить его содержимым, если оно поменялось,
        # или просто применить стиль.
        text = self.doc.Text
        cursor = text.createTextCursorByRange(anchor)
        
        # Используем formatter для вставки одного абзаца
        # UNO_formatter.apply_structure_to_cursor_chunk(cursor, block_data)
        # Так как старый uno_formatter очищал все, мы вызываем его логику вручную тут
        # для одного блока.
        try:
            text_content = block_data.get("text", block_data.get("content", ""))
            if text_content:
                import re
                text_content = re.sub(r'\*\*(.*?)\*\*', r'\1', text_content)
                text_content = re.sub(r'^#+\s*', '', text_content)
                
            block_type = block_data.get("type", "paragraph")
            if "level" in block_data and block_type == "paragraph": block_type = "header"
            if "tableRows" in block_data: block_type = "table"; block_data["data"] = block_data["tableRows"]
            
            from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK
            
            if block_type == "table":
                rows = block_data.get("rows", len(block_data.get("data", [])))
                cols = block_data.get("cols", 1)
                if cols == 1 and block_data.get("data") and len(block_data["data"]) > 0:
                     cols = len(block_data["data"][0])
                # Временно перехватчик insert_table (старый код полагался на self.cursor, а не переданный)
                self.formatter.cursor = cursor
                self.formatter.text = text
                self.formatter.insert_table(rows, cols, block_data.get("data", []))
                
            elif block_type == "image":
                self.formatter.insert_image_placeholder(cursor, text_content or "Image")
                
            elif block_type == "page_break":
                cursor.BreakType = uno.getConstantByName("com.sun.star.style.BreakType.PAGE_BEFORE")
                
            elif block_type == "header":
                level = block_data.get("level", 1)
                style_name = block_data.get("style_name", f"Heading {level}")
                self.formatter.ensure_style_exists(style_name, block_data)
                try: cursor.ParaStyleName = style_name
                except: pass
                if text_content: text.insertString(cursor, text_content, False)
                temp_cursor = text.createTextCursorByRange(cursor)
                temp_cursor.goLeft(len(text_content), True)
                self.formatter._apply_direct_formatting(temp_cursor, block_data)
                text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
                
            else:
                if block_data.get("style_name"):
                    self.formatter.ensure_style_exists(block_data["style_name"], block_data)
                    try: cursor.ParaStyleName = block_data["style_name"]
                    except: pass
                if text_content:
                    text.insertString(cursor, text_content, False)
                    temp_cursor = text.createTextCursorByRange(cursor)
                    temp_cursor.goLeft(len(text_content), True)
                    self.formatter._apply_direct_formatting(temp_cursor, block_data)
                text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
                
            # Расширяем закладку, сдвигая ее конец за вставленный текст
            # Закладка автоматом расширяется, если вставлять ВНУТРИ неё.
        except Exception as e:
            log_to_file("Chunk format error", e)

    def _finish(self):
        self.is_finished = True
        
        # Удаляем все созданные для батчинга закладки
        try:
            bookmarks = self.doc.getBookmarks()
            # bookmarks не поддерживает итерацию напрямую удобным способом, берем имена:
            bookmark_names = bookmarks.getElementNames()
            for b_name in bookmark_names:
                if b_name.startswith(self.bookmark_name):
                    try:
                        bookmarks.getByName(b_name).dispose()
                    except:
                        pass
        except: pass
        
        # Закрываем Undo Context
        if self.undo_manager:
            try: self.undo_manager.leaveUndoContext()
            except: pass
            
        # Закрываем диалог (это разблокирует execute() в основном потоке)
        if self.dialog_handler and self.dialog_handler.dialog:
            try: self.dialog_handler.dialog.endExecute()
            except: pass

# === MAIN EXECUTOR ===
class MainJob(unohelper.Base, XJobExecutor):
    def __init__(self, ctx):
        self.ctx = ctx
        try: self.sm = ctx.getServiceManager(); self.desktop = XSCRIPTCONTEXT.getDesktop()
        except NameError: self.sm = ctx.ServiceManager; self.desktop = self.ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)

    def get_config(self, key, default):
        try:
            path_settings = self.sm.createInstanceWithContext('com.sun.star.util.PathSettings', self.ctx)
            user_config_path = getattr(path_settings, "UserConfig")
            if user_config_path.startswith('file://'): user_config_path = str(uno.fileUrlToSystemPath(user_config_path))
            config_file_path = os.path.join(user_config_path, "localwriter.json")
            if not os.path.exists(config_file_path): return default
            with open(config_file_path, 'r') as file: return json.load(file).get(key, default)
        except: return default

    def set_config(self, key, value):
        try:
            path_settings = self.sm.createInstanceWithContext('com.sun.star.util.PathSettings', self.ctx)
            user_config_path = getattr(path_settings, "UserConfig")
            if user_config_path.startswith('file://'): user_config_path = str(uno.fileUrlToSystemPath(user_config_path))
            config_file_path = os.path.join(user_config_path, "localwriter.json")
            data = {}
            if os.path.exists(config_file_path):
                with open(config_file_path, 'r') as f: data = json.load(f)
            data[key] = value
            with open(config_file_path, 'w') as f: json.dump(data, f, indent=4)
        except: pass

    def settings_box(self):
        cfg = {
            "middleware_url": self.get_config("middleware_url", "http://localhost:8323"),
            "model": self.get_config("model", "")
        }
        return SettingsDialogHandler(self.ctx, cfg).show()
    
    def input_box(self, message, title="", default=""):
        try:
            smgr = self.ctx.getServiceManager()
            dialog = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", self.ctx)
            model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", self.ctx)
            dialog.setModel(model); dialog.setVisible(False); dialog.setTitle(title); dialog.setPosSize(0,0,400,100,15)
            lbl = model.createInstance("com.sun.star.awt.UnoControlFixedTextModel")
            lbl.Name="lbl"; lbl.Label=str(message); lbl.PositionX=5; lbl.PositionY=5; lbl.Width=390; lbl.Height=20
            model.insertByName("lbl", lbl)
            edt = model.createInstance("com.sun.star.awt.UnoControlEditModel")
            edt.Name="edt"; edt.Text=str(default); edt.PositionX=5; edt.PositionY=30; edt.Width=390; edt.Height=20
            model.insertByName("edt", edt)
            btn = model.createInstance("com.sun.star.awt.UnoControlButtonModel")
            btn.Name="btn"; btn.Label="OK"; btn.PositionX=150; btn.PositionY=60; btn.Width=100; btn.Height=25; btn.PushButtonType=1; btn.DefaultButton=True
            model.insertByName("btn", btn)
            toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", self.ctx)
            dialog.createPeer(toolkit, None)
            ret = ""
            if dialog.execute(): ret = dialog.getControl("edt").getModel().Text
            dialog.dispose()
            return ret
        except: return ""

    def msg_box(self, message, title="Info"):
        try:
            toolkit = self.ctx.getServiceManager().createInstanceWithContext("com.sun.star.awt.Toolkit", self.ctx)
            msgbox = toolkit.createMessageBox(None, "messbox", 1, title, str(message))
            msgbox.execute()
        except: pass

    def trigger(self, args):
        if args == "settings":
            res = self.settings_box()
            for k, v in res.items(): self.set_config(k, v)
            return

        # Инициализация параметров (HTTP-вызовы делегированы в lw_client)
        middleware_url = self.get_config("middleware_url", "http://localhost:8323").rstrip('/')
        model_name = self.get_config("model", "")

        # Получение выделения
        try:
            desktop = self.ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)
            model = desktop.getCurrentComponent()
            text = model.Text
            selection = model.CurrentController.getSelection()
            text_range = selection.getByIndex(0)
        except Exception as e:
            log_to_file("Context Error", e)
            return
        
        if args == "ReportBug":
            selected_text = text_range.getString()
            if not selected_text:
                self.msg_box("Select the text causing issues first.", "Report Bug")
                return
            comment = self.input_box("Describe the error:", "Report Bug")
            try:
                tracer = ExecutionTracer()
                path = tracer.save_user_bug_report(comment, selected_text)
                self.msg_box(f"Bug report saved to: {path}", "Saved")
            except: pass
            return

        if args == "ShowDebug":
            try:
                tracer = ExecutionTracer()
                t = tracer.get_latest_trace()
                self.msg_box(json.dumps(t, indent=2, ensure_ascii=False)[:800] + "...", "Last Trace")
            except: pass
            return

        # --- COMMAND: APPLY TEMPLATE (CORE) ---
        if args == "ApplyTemplate":
            content = text_range.getString() or text.getString()
            if not content:
                self.msg_box("Document is empty or nothing selected.", "Info")
                return

            try:
                # 1. Защита ввода: Расставляем закладки ПО АБЗАЦАМ
                # Это критично для гибридного конвейера, так как ответы (ID) приходят не по порядку
                bookmarks = model.getBookmarks()
                base_bookmark_name = f"LW_AI_Target_{int(time.time())}"
                
                # Создаем курсор (enumeration) по параграфам выделенного текста
                enum = text_range.createEnumeration()
                p_id = 0
                paragraphs_text = []
                
                while enum.hasMoreElements():
                    para = enum.nextElement()
                    if para.supportsService("com.sun.star.text.Paragraph"):
                        # Пропускаем пустые абзацы, чтобы не забивать контекст LLM мусором
                        if not para.getString().strip():
                            continue
                            
                        # Создаем индивидуальную закладку для абзаца
                        b_name = f"{base_bookmark_name}_p{p_id}"
                        bookmark = model.createInstance("com.sun.star.text.Bookmark")
                        bookmark.Name = b_name
                        
                        # Вставляем закладку, охватывающую абзац
                        para_cursor = text.createTextCursorByRange(para)
                        model.Text.insertTextContent(para_cursor, bookmark, True)
                        
                        paragraphs_text.append(para.getString())
                        p_id += 1
                
                if not paragraphs_text:
                    self.msg_box("Document is empty or nothing selected.", "Info")
                    return
                
                # 2. Очередь и контроль
                result_queue = queue.Queue()
                stop_event = threading.Event()
                
                # 3. Делаем стартовый быстрый UNO-согласованный UI
                dlg_handler = ProgressDialogHandler(self.ctx, stop_event)
                dialog = dlg_handler.create()
                
                # 4. Запускаем фоновый HTTP NDJSON поток
                is_degraded, rag_id = lw_client.call_apply_template_ndjson(
                    content=paragraphs_text,
                    model=model_name,
                    middleware_url=middleware_url,
                    result_queue=result_queue,
                    stop_event=stop_event,
                )
                
                if is_degraded:
                    # Всплывающее уведомление, что сервер работает в Degraded Mode
                    self.msg_box((
                        "⚠️ Server is low on RAM!\n\n"
                        "LocalWriter is running in Degraded Mode (4096 tokens).\n"
                        "Context memory is capped to prevent Out of Memory crash.\n\n"
                        "The result might be slightly degraded."
                    ), "Memory Warning")
                
                # 5. Timer Listener (Polling Queue in GUI Thread)
                formatter = UnoFormatter(self.ctx)
                timer_listener = ApplyTemplateTimerListener(
                    self, result_queue, stop_event, dlg_handler, base_bookmark_name, model, formatter
                )
                
                # Запускаем XTimer каждые 100 мс (10 FPS для UI-прогресса)
                timer = self.ctx.getServiceManager().createInstanceWithContext("com.sun.star.awt.XTimer", self.ctx)
                if not timer:
                    timer = self.ctx.getServiceManager().createInstance("com.sun.star.awt.Timer")
                    
                if timer:
                    timer.setInterval(100) # ms
                    timer.addTimeoutListener(timer_listener)
                    timer.start()
                else:
                    # Fallback для версий LibreOffice, где XTimer недоступен
                    def poll_queue_fallback():
                        while not timer_listener.is_finished:
                            timer_listener.timeout(None)
                            time.sleep(0.1)
                    threading.Thread(target=poll_queue_fallback, daemon=True).start()
                
                # БЛОКИРУЕМ UI LibreOffice (модальный диалог), но оставляем GUI цикл живым 
                # (XTimer будет дергать timer_listener.timeout, который будет менять док)
                try:
                    dialog.execute()
                finally:
                    # Гарантированная очистка (даже при отмене или ошибках сети)
                    stop_event.set()
                    if timer:
                        try:
                            timer.stop()
                        except: pass
                    try:
                        dialog.dispose()
                    except: pass
                    
                    # Принудительно вызываем очистку закладок и закрываем UndoContext
                    try:
                        timer_listener._finish()
                    except: pass
                
                log_to_file(f"ApplyTemplate NDJSON Finished.")

            except Exception as e:
                log_to_file("Network/Runtime Error", e)
                try: stop_event.set()
                except: pass
                self.msg_box(f"Application Error: {e}", "Fail")
            return

        # --- COMMANDS: STREAMING (Extend/Edit) ---
        if args in ["ExtendSelection", "EditSelection"]:
            if args == "ExtendSelection":
                full_prompt = text_range.getString()
            else:
                user_input = self.input_box("Instruction:", "Edit")
                full_prompt = f"ORIGINAL: {text_range.getString()}\nINSTR: {user_input}"

            try:
                toolkit = self.ctx.getServiceManager().createInstanceWithContext("com.sun.star.awt.Toolkit", self.ctx)
                is_first = True
                # Получаем дельты через генератор из client.py
                for delta in lw_client.call_streaming_completion(full_prompt, model_name, middleware_url):
                    if args == "EditSelection" and is_first:
                        text_range.setString("")
                        is_first = False
                    text_range.setString(text_range.getString() + delta)
                    toolkit.processEventsToIdle()
            except Exception as e:
                log_to_file("Stream Error", e)


def main():
    try: ctx = XSCRIPTCONTEXT
    except NameError: ctx = officehelper.bootstrap()
    MainJob(ctx).trigger("settings")

g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(MainJob, "org.extension.sample.do", ("com.sun.star.task.Job",),)