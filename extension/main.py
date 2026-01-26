import sys
import os

try:
    sys.path.append(os.path.dirname(__file__))
except: pass

import uno
import unohelper
import officehelper
import json
import urllib.request
import urllib.error
import urllib.parse
import traceback
import datetime
import re # Добавили регулярки
from uno_formatter import UnoFormatter 
from com.sun.star.task import XJobExecutor
from com.sun.star.awt import XActionListener

# === LOGGING ===
def log_to_file(message, error=None):
    log_file_path = "/tmp/localwriter.log"
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    if error:
        log_entry += f"\nERROR: {str(error)}\n{traceback.format_exc()}"
    try:
        with open(log_file_path, "a", encoding="utf-8") as f:
            f.write(log_entry + "\n")
    except: pass

def log_json_debug(data, filename="llm_debug.json"):
    try:
        with open(f"/tmp/{filename}", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except: pass

def fetch_models(middleware_url, target_ollama_url):
    clean_middleware = middleware_url.rstrip('/')
    api_url = f"{clean_middleware}/api/tags"
    req = urllib.request.Request(api_url)
    req.add_header("X-Target-Ollama-Url", target_ollama_url)
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            data = json.loads(response.read().decode())
            if "models" in data: return [m["name"] for m in data["models"]]
    except: return []
    return []

# === HELPERS ===
def extract_json_from_text(text):
    """
    Пытается вытащить валидный JSON из ответа модели.
    Убирает Markdown ```json ... ``` и ищет первый [ ... ] или { ... }
    """
    # 1. Удаляем Markdown блоки
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```', '', text)
    text = text.strip()

    # 2. Ищем JSON структуру
    try:
        # Пытаемся распарсить как есть
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 3. Если не вышло, ищем первую [ и последнюю ]
    try:
        start = text.find('[')
        end = text.rfind(']') + 1
        if start != -1 and end != -1:
            return json.loads(text[start:end])
    except: pass

    # 4. Если и это не вышло, ищем { }
    try:
        start = text.find('{')
        end = text.rfind('}') + 1
        if start != -1 and end != -1:
            return json.loads(text[start:end])
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
        self._add_edit("txt_ollama", self.config.get("ollama_url", "http://192.168.0.107:11434"), 5, 52, 240, 15)
        self._add_label("lbl_model", "Model:", 5, 75, 150, 10)
        self._add_button("btn_refresh", "Refresh List", 165, 73, 80, 14, "Refresh")
        self._add_listbox("lst_model", 5, 90, 240, 15)
        
        current_model = self.config.get("model", "")
        if current_model:
            self.dialog.getControl("lst_model").addItem(current_model, 0)
            self.dialog.getControl("lst_model").selectItem(current_model, True)

        y = 120
        self._add_label("lbl_tk1", "Extend Max Tokens:", 5, y, 120, 10)
        self._add_edit("txt_ext_tokens", str(self.config.get("extend_selection_max_tokens", "100")), 130, y, 115, 15)
        y += 25
        self._add_label("lbl_tk2", "Edit Max New Tokens:", 5, y, 120, 10)
        self._add_edit("txt_edit_tokens", str(self.config.get("edit_selection_max_new_tokens", "0")), 130, y, 115, 15)
        y += 25
        self._add_label("lbl_sys1", "Extend System Prompt:", 5, y, 240, 10)
        self._add_edit("txt_sys_ext", self.config.get("extend_selection_system_prompt", ""), 5, y+12, 240, 40, True)
        y += 60
        self._add_label("lbl_sys2", "Edit System Prompt:", 5, y, 240, 10)
        self._add_edit("txt_sys_edit", self.config.get("edit_selection_system_prompt", ""), 5, y+12, 240, 40, True)

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
        model.MultiLine = multi_line
        if multi_line: model.VScroll = True
        self.dialog.Model.insertByName(name, model)
    def _add_button(self, name, label, x, y, w, h, action_command=None, default=False):
        model = self.dialog.Model.createInstance("com.sun.star.awt.UnoControlButtonModel")
        model.Name = name; model.Label = label; model.PositionX = x; model.PositionY = y; model.Width = w; model.Height = h
        if default: model.DefaultButton = True
        self.dialog.Model.insertByName(name, model)
        control = self.dialog.getControl(name)
        control.setActionCommand(action_command)
        control.addActionListener(self)
    def _add_listbox(self, name, x, y, w, h):
        model = self.dialog.Model.createInstance("com.sun.star.awt.UnoControlListBoxModel")
        model.Name = name; model.Dropdown = True; model.PositionX = x; model.PositionY = y; model.Width = w; model.Height = h
        self.dialog.Model.insertByName(name, model)
    def _refresh_models(self):
        mid = self.dialog.getControl("txt_mid").getText()
        ollama = self.dialog.getControl("txt_ollama").getText()
        models = fetch_models(mid, ollama)
        listbox = self.dialog.getControl("lst_model")
        listbox.removeItems(0, listbox.getItemCount())
        if models:
            listbox.addItems(tuple(models), 0)
            listbox.selectItem(models[0], True)
        else: listbox.addItem("Connection failed", 0)
    def actionPerformed(self, actionEvent):
        if actionEvent.ActionCommand == "Refresh": self._refresh_models()
        elif actionEvent.ActionCommand == "OK":
            self.result = {
                "middleware_url": self.dialog.getControl("txt_mid").getText(),
                "ollama_url": self.dialog.getControl("txt_ollama").getText(),
                "model": self.dialog.getControl("lst_model").getSelectedItem(),
                "extend_selection_max_tokens": self.dialog.getControl("txt_ext_tokens").getText(),
                "edit_selection_max_new_tokens": self.dialog.getControl("txt_edit_tokens").getText(),
                "extend_selection_system_prompt": self.dialog.getControl("txt_sys_ext").getText(),
                "edit_selection_system_prompt": self.dialog.getControl("txt_sys_edit").getText(),
            }
            self.dialog.endExecute()
        elif actionEvent.ActionCommand == "Cancel": self.result = {}; self.dialog.endExecute()
    def disposing(self, event): pass

# === MAIN LOGIC ===
class MainJob(unohelper.Base, XJobExecutor):
    def __init__(self, ctx):
        self.ctx = ctx
        try:
            self.sm = ctx.getServiceManager()
            self.desktop = XSCRIPTCONTEXT.getDesktop()
        except NameError:
            self.sm = ctx.ServiceManager
            self.desktop = self.ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)
    
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
            config_data = {}
            if os.path.exists(config_file_path):
                with open(config_file_path, 'r') as file: 
                    try: config_data = json.load(file)
                    except: pass
            config_data[key] = value
            with open(config_file_path, 'w') as file: json.dump(config_data, file, indent=4)
        except Exception as e: log_to_file("Config Write Error", e)

    def input_box(self, message, title="", default=""):
        import uno
        from com.sun.star.awt.PosSize import POS, SIZE
        from com.sun.star.awt.PushButtonType import OK
        ctx = uno.getComponentContext(); smgr = ctx.getServiceManager()
        dialog = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", ctx)
        model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", ctx)
        dialog.setModel(model); dialog.setVisible(False); dialog.setTitle(title); dialog.setPosSize(0,0, 400, 100, SIZE)
        lbl = model.createInstance("com.sun.star.awt.UnoControlFixedTextModel")
        lbl.Name = "lbl"; lbl.Label = str(message); lbl.PositionX=5; lbl.PositionY=5; lbl.Width=390; lbl.Height=20
        model.insertByName("lbl", lbl)
        edt = model.createInstance("com.sun.star.awt.UnoControlEditModel")
        edt.Name = "edt"; edt.Text = str(default); edt.PositionX=5; edt.PositionY=30; edt.Width=390; edt.Height=20
        model.insertByName("edt", edt)
        btn = model.createInstance("com.sun.star.awt.UnoControlButtonModel")
        btn.Name = "btn"; btn.Label = "OK"; btn.PositionX=150; btn.PositionY=60; btn.Width=100; btn.Height=25; btn.PushButtonType=OK; btn.DefaultButton=True
        model.insertByName("btn", btn)
        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        dialog.createPeer(toolkit, None); dialog.setPosSize(0,0,0,0, POS) 
        ret = ""; 
        if dialog.execute(): ret = dialog.getControl("edt").getModel().Text
        dialog.dispose(); return ret

    # === ВОТ ЭТО БЫЛО ПОТЕРЯНО! ===
    def settings_box(self):
        cfg = {
            "middleware_url": self.get_config("middleware_url", "http://localhost:8323"),
            "ollama_url": self.get_config("ollama_url", "http://192.168.0.107:11434"),
            "model": self.get_config("model", ""),
            "extend_selection_max_tokens": self.get_config("extend_selection_max_tokens", "100"),
            "edit_selection_max_new_tokens": self.get_config("edit_selection_max_new_tokens", "0"),
            "extend_selection_system_prompt": self.get_config("extend_selection_system_prompt", ""),
            "edit_selection_system_prompt": self.get_config("edit_selection_system_prompt", "")
        }
        return SettingsDialogHandler(self.ctx, cfg).show()

    def trigger(self, args):
        log_to_file(f"=== TRIGGER STARTED: {args} ===")
        try:
            desktop = self.ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)
            model = desktop.getCurrentComponent()

            if hasattr(model, "Text"):
                text = model.Text
                selection = model.CurrentController.getSelection()
                text_range = selection.getByIndex(0)

                if args == "settings":
                    res = self.settings_box() # Теперь этот метод существует
                    for k, v in res.items(): self.set_config(k, v)
                    return

                middleware_url = self.get_config("middleware_url", "http://localhost:8323").rstrip('/')
                ollama_url = self.get_config("ollama_url", "http://localhost:11434").rstrip('/')
                model_name = self.get_config("model", "")
                
                if not model_name: return

                url = f"{middleware_url}/v1/completions"
                headers = {'Content-Type': 'application/json', 'X-Target-Ollama-Url': ollama_url}
                data = {}

                # ==========================================
                # === APPLY TEMPLATE WITH STYLES ===
                # ==========================================
                if args == "ApplyTemplate":
                    content = text.getString()
                    if not content: content = text_range.getString()
                    if not content: return

                    # УЖЕСТОЧАЕМ ПРОМТ ДЛЯ JSON
                    system_prompt = (
                        "Ты — технический JSON API. Твоя задача — вернуть СТРОГО валидный JSON список (Array).\n"
                        "Каждый элемент списка должен быть ОБЪЕКТОМ (Dict).\n"
                        "ЗАПРЕЩЕНО возвращать просто строки внутри списка.\n"
                        "ЗАПРЕЩЕНО писать вступления или markdown.\n"
                        "Формат объектов:\n"
                        "- {'type': 'header', 'level': 1, 'text': '...'}\n"
                        "- {'type': 'paragraph', 'text': '...', 'align': 'justify'}\n"
                        "- {'type': 'page_break'}\n"
                        "- {'type': 'toc'}\n"
                    )
                    user_prompt = f"Переделай этот текст в структуру JSON:\n{content[:4000]}"
                    
                    data = {
                        'model': model_name,
                        'prompt': f"SYSTEM: {system_prompt}\nUSER: {user_prompt}",
                        'max_tokens': 4000,
                        'stream': False,
                        'format': 'json'
                    }
                    
                    log_to_file("Sending ApplyTemplate request...")
                    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers, method='POST')
                    
                    try:
                        with urllib.request.urlopen(req) as response:
                            best_template = response.headers.get("X-Best-Template-ID")
                            if best_template:
                                log_to_file(f"Found template: {best_template}")
                                download_url = f"{middleware_url}/api/download_template/{best_template}"
                                tmp_file = os.path.join("/tmp", f"style_{best_template}")
                                try:
                                    urllib.request.urlretrieve(download_url, tmp_file)
                                    formatter = UnoFormatter(self.ctx)
                                    formatter.import_styles_from_template(tmp_file)
                                    log_to_file("Styles imported!")
                                except Exception as e:
                                    log_to_file("Failed to download/import styles", e)

                            resp_data = json.loads(response.read().decode())
                            ai_text = ""
                            if 'choices' in resp_data: ai_text = resp_data['choices'][0]['text']
                            elif 'response' in resp_data: ai_text = resp_data['response']
                            
                            # Логируем сырой ответ перед парсингом
                            log_to_file(f"RAW AI: {ai_text[:200]}...")

                            try:
                                # 1. Парсинг
                                structure = extract_json_from_text(ai_text)
                                log_json_debug(structure)
                                
                                if not isinstance(structure, list) and isinstance(structure, dict):
                                     for key in structure:
                                        if isinstance(structure[key], list):
                                            structure = structure[key]; break
                                
                                if not isinstance(structure, list): raise ValueError("Not a list")

                                # 2. Применение (UNO)
                                formatter = UnoFormatter(self.ctx)
                                text.setString("") 
                                formatter.apply_structure(structure)
                                log_to_file("Structure applied!")
                                
                            except json.JSONDecodeError as e:
                                log_to_file("JSON Parsing Failed", e)
                                log_to_file(f"RAW: {ai_text[:500]}")
                            except Exception as e:
                                log_to_file("UNO/Formatter Failed", e)
                                
                    except Exception as e:
                        log_to_file("Network Error", e)
                    return

                # --- EXTEND ---
                elif args == "ExtendSelection":
                    prompt = text_range.getString()
                    if not prompt: return
                    sys_prompt = self.get_config("extend_selection_system_prompt", "")
                    if sys_prompt: prompt = f"SYSTEM PROMPT\n{sys_prompt}\nEND SYSTEM PROMPT\n{prompt}"
                    try: max_t = int(self.get_config("extend_selection_max_tokens", "100") or 100)
                    except: max_t = 100
                    data = {'model': model_name, 'prompt': prompt, 'max_tokens': max_t, 'stream': True}

                # --- EDIT ---
                elif args == "EditSelection":
                    user_input = self.input_box("Enter instructions:", "Edit Selection")
                    if not user_input: return
                    original = text_range.getString()
                    prompt = f"ORIGINAL:\n{original}\nINSTRUCTIONS:\n{user_input}\nEDITED:\n"
                    sys_prompt = self.get_config("edit_selection_system_prompt", "")
                    if sys_prompt: prompt = f"SYSTEM PROMPT\n{sys_prompt}\nEND SYSTEM PROMPT\n{prompt}"
                    try: max_n = int(self.get_config("edit_selection_max_new_tokens", "0") or 0)
                    except: max_n = 0
                    data = {'model': model_name, 'prompt': prompt, 'max_tokens': len(original) + max_n, 'stream': True}
                else: return

                log_to_file(f"Requesting URL: {url}")
                json_data = json.dumps(data).encode('utf-8')
                req = urllib.request.Request(url, data=json_data, headers=headers, method='POST')
                toolkit = self.ctx.getServiceManager().createInstanceWithContext("com.sun.star.awt.Toolkit", self.ctx)
                
                is_first_chunk = True 
                try:
                    with urllib.request.urlopen(req) as response:
                        for line in response:
                            if line.strip() and line.startswith(b"data: "):
                                try:
                                    payload = line[len(b"data: "):].decode("utf-8")
                                    if payload.strip() == "[DONE]": break
                                    
                                    chunk = json.loads(payload)
                                    delta = ""
                                    if "choices" in chunk and len(chunk["choices"]) > 0:
                                        if "text" in chunk["choices"][0]:
                                            delta = chunk["choices"][0]["text"]
                                        elif "delta" in chunk["choices"][0] and "content" in chunk["choices"][0]["delta"]:
                                            delta = chunk["choices"][0]["delta"]["content"]
                                    
                                    if delta:
                                        if args == "EditSelection" and is_first_chunk:
                                            text_range.setString("")
                                            is_first_chunk = False
                                        text_range.setString(text_range.getString() + delta)
                                        toolkit.processEventsToIdle()
                                except json.JSONDecodeError: pass
                except urllib.error.HTTPError as e:
                    log_to_file(f"HTTP ERROR: {e.code} - {e.read().decode()}")

        except Exception as e:
            log_to_file("CRITICAL MAIN ERROR", e)

def main():
    try: ctx = XSCRIPTCONTEXT
    except NameError: ctx = officehelper.bootstrap()
    MainJob(ctx).trigger("settings")

if __name__ == "__main__": main()
g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(MainJob, "org.extension.sample.do", ("com.sun.star.task.Job",),)