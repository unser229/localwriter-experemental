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
    def log_to_file_fallback(msg):
        with open("/tmp/localwriter_boot_error.log", "a") as f:
            f.write(str(msg) + "\n")
    log_to_file_fallback(e)

from com.sun.star.task import XJobExecutor
from com.sun.star.awt import XActionListener

# === HELPERS ===
def log_to_file(message, error=None):
    log_file_path = "/tmp/localwriter.log"
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    if error: log_entry += f"\nERROR: {str(error)}\n{traceback.format_exc()}"
    try:
        with open(log_file_path, "a", encoding="utf-8") as f: f.write(log_entry + "\n")
    except: pass

def extract_json_from_text(text):
    text = text.replace("&nbsp;", " ").replace("&quot;", '"')
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```', '', text)
    text = text.strip()
    try: return json.loads(text)
    except: pass
    try:
        start = text.find('[')
        end = text.rfind(']') + 1
        if start != -1 and end != -1: return json.loads(text[start:end])
    except: pass
    raise ValueError("Could not extract JSON")

# === SETTINGS UI ===
class SettingsDialogHandler(unohelper.Base, XActionListener):
    def __init__(self, ctx, current_config):
        self.ctx = ctx; self.config = current_config; self.dialog = None; self.result = {}
    
    def show(self):
        smgr = self.ctx.getServiceManager()
        self.dialog = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", self.ctx)
        model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", self.ctx)
        self.dialog.setModel(model)
        model.Title = "LocalWriter Settings"; model.PositionX = 100; model.PositionY = 100; model.Width = 250; model.Height = 360
        
        self._add_label("lbl_mid", "Middleware URL:", 5, 5, 240, 10)
        self._add_edit("txt_mid", self.config.get("middleware_url", "http://localhost:8323"), 5, 17, 240, 15)
        self._add_label("lbl_ollama", "Inference Engine URL:", 5, 40, 240, 10)
        self._add_edit("txt_ollama", self.config.get("ollama_url", "http://localhost:11434"), 5, 52, 240, 15)
        self._add_label("lbl_model", "Model Name:", 5, 75, 240, 10)
        self._add_edit("txt_model", self.config.get("model", ""), 5, 87, 240, 15)
        
        self._add_button("btn_ok", "Save", 130, 330, 50, 20, "OK", True)
        self._add_button("btn_cancel", "Cancel", 190, 330, 50, 20, "Cancel")
        
        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", self.ctx)
        self.dialog.createPeer(toolkit, None); self.dialog.execute(); self.dialog.dispose()
        return self.result

    def _add_label(self, name, label, x, y, w, h):
        model = self.dialog.Model.createInstance("com.sun.star.awt.UnoControlFixedTextModel")
        model.Name = name; model.Label = label; model.PositionX = x; model.PositionY = y; model.Width = w; model.Height = h
        self.dialog.Model.insertByName(name, model)
    def _add_edit(self, name, text, x, y, w, h, multi_line=False):
        model = self.dialog.Model.createInstance("com.sun.star.awt.UnoControlEditModel")
        model.Name = name; model.Text = text; model.PositionX = x; model.PositionY = y; model.Width = w; model.Height = h
        self.dialog.Model.insertByName(name, model)
    def _add_button(self, name, label, x, y, w, h, action_command=None, default=False):
        model = self.dialog.Model.createInstance("com.sun.star.awt.UnoControlButtonModel")
        model.Name = name; model.Label = label; model.PositionX = x; model.PositionY = y; model.Width = w; model.Height = h
        if default: model.DefaultButton = True
        self.dialog.Model.insertByName(name, model)
        control = self.dialog.getControl(name)
        control.setActionCommand(action_command)
        control.addActionListener(self)
    def actionPerformed(self, e):
        if e.ActionCommand == "OK":
            self.result = {"middleware_url": self.dialog.getControl("txt_mid").getText(), "ollama_url": self.dialog.getControl("txt_ollama").getText(), "model": self.dialog.getControl("txt_model").getText()}
            self.dialog.endExecute()
        elif e.ActionCommand == "Cancel": self.result = {}; self.dialog.endExecute()
    def disposing(self, e): pass

# === MAIN JOB ===
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

    # ВОТ ОН, ПРОПАВШИЙ МЕТОД!
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
        if args == "ReportBug":
            try:
                desktop = self.ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)
                model = desktop.getCurrentComponent()
                selection = model.CurrentController.getSelection()
                selected_text = selection.getByIndex(0).getString()
                if not selected_text:
                    self.msg_box("Select text first.", "Report Bug")
                    return
                comment = self.input_box("Error description:", "Report Bug")
                if not comment: return
                tracer = ExecutionTracer()
                path = tracer.save_user_bug_report(comment, selected_text)
                self.msg_box(f"Saved: {path}", "Success")
            except: pass
            return

        if args == "ShowDebug":
            tracer = ExecutionTracer()
            t = tracer.get_latest_trace()
            self.msg_box(json.dumps(t, indent=2, ensure_ascii=False)[:800] + "...", "Trace")
            return

        tracer = ExecutionTracer()
        try:
            desktop = self.ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)
            model = desktop.getCurrentComponent()
            text = model.Text
            selection = model.CurrentController.getSelection()
            text_range = selection.getByIndex(0)

            if args == "settings":
                res = self.settings_box()
                for k, v in res.items(): self.set_config(k, v)
                return

            middleware_url = self.get_config("middleware_url", "http://localhost:8323").rstrip('/')
            ollama_url = self.get_config("ollama_url", "http://localhost:11434").rstrip('/')
            model_name = self.get_config("model", "")
            
            tracer.log_step("Init", args, f"Model: {model_name}")
            headers = {'Content-Type': 'application/json', 'X-Target-Ollama-Url': ollama_url}
            url = f"{middleware_url}/v1/completions"

            if args == "ApplyTemplate":
                content = text_range.getString() or text.getString()
                if not content: return

                # Очистка входного текста от Markdown (чтобы не путать модель)
                clean_content = re.sub(r'[*#]', '', content)
                
                search_query = clean_content[:1500]
                tracer.log_step("RAG Input", search_query, "Sending...")
                rag_context_str = "Standard formatting."

                try:
                    rag_api_url = f"{middleware_url}/api/retrieve_context"
                    rag_payload = json.dumps({"text": search_query}).encode()
                    req = urllib.request.Request(rag_api_url, data=rag_payload, headers={'Content-Type': 'application/json'}, method='POST')
                    with urllib.request.urlopen(req) as rr:
                        r = json.loads(rr.read().decode())
                        rag_context_str = r.get("context", "")
                        tracer.log_step("RAG Result", {"id": r.get("source_id")}, rag_context_str[:500])
                except Exception as e: tracer.log_step("RAG Error", str(e), "Default")

                system_prompt = (
                    "You are a professional Document Layout Expert.\n"
                    "I will provide you with a STYLE REFERENCE (examples of styles from a real document) "
                    "and a USER CONTENT (raw text).\n\n"
                    "YOUR MISSION: Map the USER CONTENT to the style names seen in the REFERENCE.\n\n"
                    "REFERENCE INTERPRETATION:\n"
                    "- Look at [S: StyleName] in the reference.\n"
                    "- If Reference shows 'Title' or 'Heading' for organizational names, use that for user's headers.\n"
                    "- If Reference uses 'Normal' with [A: JUSTIFY], apply that to user's paragraphs.\n\n"
                    "RULES:\n"
                    "1. Keep every word of User Content. No summaries!\n"
                    "2. Use 'style_name' exactly as seen in the [S: ...] tags.\n"
                    "3. Output JSON list of objects: {'style_name': '...', 'text': '...'}"
                )

                user_prompt = (
                    f"=== REFERENCE (STYLE SOURCE) ===\n{rag_context_str[:3000]}\n\n"
                    f"=== USER CONTENT (CONTENT SOURCE) ===\n{clean_content}\n\n"
                    "OUTPUT JSON:"
                )
                
                data = {
                    'model': model_name,
                    'prompt': f"SYSTEM: {system_prompt}\nUSER: {user_prompt}",
                    'max_tokens': -1,
                    'stream': False,
                    'format': 'json',
                    'options': {'num_ctx': 8192} # Важно для большого контекста
                }
                tracer.log_step("LLM Request", "Sending...", user_prompt[:200])

                req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers, method='POST')
                with urllib.request.urlopen(req) as response:
                    resp_data = json.loads(response.read().decode())
                    ai_text = resp_data.get('response', '') or resp_data.get('choices', [{}])[0].get('text', '')
                    tracer.log_step("LLM Response", "Received", ai_text)
                    
                    structure = extract_json_from_text(ai_text)
                    if isinstance(structure, dict):
                         for v in structure.values():
                             if isinstance(v, list): structure = v; break
                    
                    if isinstance(structure, list):
                        formatter = UnoFormatter(self.ctx)
                        formatter.apply_structure(structure)
                        tracer.log_step("Success", "Applied", "OK")
                    else: tracer.log_error("Validation", "Not a list")

            elif args in ["ExtendSelection", "EditSelection"]:
                if args == "ExtendSelection":
                    full_prompt = text_range.getString()
                else:
                    user_input = self.input_box("Instruction:", "Edit")
                    full_prompt = f"ORIGINAL: {text_range.getString()}\nINSTR: {user_input}"
                
                data = {'model': model_name, 'prompt': full_prompt, 'stream': True}
                tracer.log_step("Stream", args, full_prompt)
                
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
                tracer.log_step("Stream", "Done", "OK")

        except Exception as e:
            if 'tracer' in locals(): tracer.log_error("Critical", e)
            log_to_file("CRITICAL", e)
        finally:
            if 'tracer' in locals(): tracer.save_report()

def main():
    try: ctx = XSCRIPTCONTEXT
    except NameError: ctx = officehelper.bootstrap()
    MainJob(ctx).trigger("settings")

g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(MainJob, "org.extension.sample.do", ("com.sun.star.task.Job",),)