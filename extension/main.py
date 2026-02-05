import sys
import os
import uno
import unohelper
import officehelper
import json
import urllib.request
import urllib.error
import urllib.parse
import traceback
import datetime
import re

# === ИМПОРТЫ ===
try:
    sys.path.append(os.path.dirname(__file__))
    from uno_formatter import UnoFormatter
    from tracer import ExecutionTracer
except ImportError as e:
    pass

from com.sun.star.task import XJobExecutor
from com.sun.star.awt import XActionListener

# === LOGGING HELPER ===
def log_to_file(message, error=None):
    log_file_path = "/tmp/localwriter.log"
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    if error: 
        log_entry += f"\nERROR: {str(error)}\n{traceback.format_exc()}"
    try:
        with open(log_file_path, "a", encoding="utf-8") as f: f.write(log_entry + "\n")
    except: pass

# === JSON PARSER HELPER ===
def extract_json_from_text(text):
    if not text: return None
    text = text.replace("&nbsp;", " ").replace("&quot;", '"')
    
    match = re.search(r'```json\s*([\s\S]*?)\s*```', text)
    if match:
        try: return json.loads(match.group(1).strip())
        except: pass

    match = re.search(r'```\s*([\s\S]*?)\s*```', text)
    if match:
        try: return json.loads(match.group(1).strip())
        except: pass
        
    match = re.search(r'^\s*\[[\s\S]*\]\s*$', text)
    if match:
        try: return json.loads(match.group(0))
        except: pass
        
    try: return json.loads(text)
    except: pass
    
    try:
        start = text.find('[')
        end = text.rfind(']') + 1
        if start != -1 and end != -1:
            return json.loads(text[start:end])
    except: pass

    return None

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
        model.Height = 220 
        
        self._add_label("lbl_mid", "Middleware URL (Backend):", 10, 10, 240, 10)
        self._add_edit("txt_mid", self.config.get("middleware_url", "http://localhost:8323"), 10, 22, 240, 15)
        
        self._add_label("lbl_ollama", "Inference Engine URL (Ollama):", 10, 45, 240, 10)
        self._add_edit("txt_ollama", self.config.get("ollama_url", "http://localhost:11434"), 10, 57, 240, 15)
        
        self._add_button("btn_check", "🔄 Check Connection & Fetch Models", 10, 80, 240, 20, "CheckConn")

        self._add_label("lbl_model", "Select Model:", 10, 110, 240, 10)
        current_model = self.config.get("model", "")
        self._add_combo("cb_model", current_model, 10, 122, 240, 15)

        self._add_button("btn_ok", "Save Settings", 90, 190, 70, 20, "OK", True)
        self._add_button("btn_cancel", "Cancel", 170, 190, 50, 20, "Cancel")
        
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
                oll_url = self.dialog.getControl("txt_ollama").getText().rstrip('/')
                
                target = f"{mid_url}/api/tags"
                # Добавляем user-agent на всякий случай
                headers = {
                    "X-Target-Ollama-Url": oll_url,
                    "User-Agent": "LocalWriter-Client"
                }
                req = urllib.request.Request(target, headers=headers)
                
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode())
                    
                models = []
                if "models" in data:
                    models = [m.get("name") for m in data["models"]]
                
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
                "ollama_url": self.dialog.getControl("txt_ollama").getText(),
                "model": self.dialog.getControl("cb_model").getText()
            }
            self.dialog.endExecute()
            
        elif cmd == "Cancel":
            self.result = {}
            self.dialog.endExecute()

    def disposing(self, e): pass

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
            "ollama_url": self.get_config("ollama_url", "http://localhost:11434"),
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

        # Инициализация переменных
        middleware_url = self.get_config("middleware_url", "http://localhost:8323").rstrip('/')
        ollama_url = self.get_config("ollama_url", "http://localhost:11434").rstrip('/')
        model_name = self.get_config("model", "")
        headers = {'Content-Type': 'application/json', 'X-Target-Ollama-Url': ollama_url}
        url = f"{middleware_url}/v1/completions"

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
            
            clean_content = re.sub(r'[*#]', '', content)
            payload_prompt = f"=== USER CONTENT (CONTENT SOURCE) ===\n{clean_content}"
            
            data = {
                'model': model_name,
                'prompt': payload_prompt,
                'stream': False, # Backend теперь это уважает
                'format': 'json', 
                'options': {'num_ctx': 8192}
            }
            
            try:
                # ВАЖНО: Увеличен таймаут для Apply Template, так как модель может долго думать
                req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers, method='POST')
                
                
                with urllib.request.urlopen(req, timeout=300) as response:
                    resp_data = json.loads(response.read().decode())
                    ai_text = resp_data.get('response', '') or resp_data.get('choices', [{}])[0].get('text', '')
                    
                    structure = extract_json_from_text(ai_text)
                    
                    if structure and isinstance(structure, list):
                        formatter = UnoFormatter(self.ctx)
                        formatter.apply_structure(structure)
                    else:
                        log_to_file(f"JSON ERROR. Raw LLM Response:\n{ai_text}")
                        self.msg_box("AI returned invalid data.\nCheck log: /tmp/localwriter.log", "Formatting Failed")
            except Exception as e:
                log_to_file("Network/Runtime Error", e)
                self.msg_box(f"Connection Error: {e}", "Fail")
            return

        # --- COMMANDS: STREAMING (Extend/Edit) ---
        if args in ["ExtendSelection", "EditSelection"]:
            if args == "ExtendSelection":
                full_prompt = text_range.getString()
            else:
                user_input = self.input_box("Instruction:", "Edit")
                full_prompt = f"ORIGINAL: {text_range.getString()}\nINSTR: {user_input}"
            
            data = {'model': model_name, 'prompt': full_prompt, 'stream': True}
            
            try:
                req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers, method='POST')
                toolkit = self.ctx.getServiceManager().createInstanceWithContext("com.sun.star.awt.Toolkit", self.ctx)
                
                is_first = True
                with urllib.request.urlopen(req) as response:
                    for line in response:
                        if line.startswith(b"data: "):
                            try:
                                payload = line[6:].decode()
                                if payload.strip() == "[DONE]": break
                                chunk = json.loads(payload)
                                delta = chunk.get("response", "") or chunk.get("choices", [{}])[0].get("text", "")
                                if delta:
                                    if args == "EditSelection" and is_first:
                                        text_range.setString("")
                                        is_first = False
                                    text_range.setString(text_range.getString() + delta)
                                    toolkit.processEventsToIdle()
                            except: pass
            except Exception as e:
                log_to_file("Stream Error", e)

def main():
    try: ctx = XSCRIPTCONTEXT
    except NameError: ctx = officehelper.bootstrap()
    MainJob(ctx).trigger("settings")

g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(MainJob, "org.extension.sample.do", ("com.sun.star.task.Job",),)