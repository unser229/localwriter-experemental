import sys
import uno
import unohelper
import officehelper
import json
import urllib.request
import urllib.error
import urllib.parse
import os
import traceback
import datetime

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

def fetch_models(middleware_url, target_ollama_url):
    """
    Запрашивает модели у Middleware, передавая Target URL в заголовке.
    """
    clean_middleware = middleware_url.rstrip('/')
    api_url = f"{clean_middleware}/api/tags"
    
    req = urllib.request.Request(api_url)
    # ВАЖНО: Передаем адрес удаленной Ollama в заголовке
    req.add_header("X-Target-Ollama-Url", target_ollama_url)
    
    try:
        log_to_file(f"Fetching models via {api_url} -> {target_ollama_url}")
        with urllib.request.urlopen(req, timeout=3) as response:
            data = json.loads(response.read().decode())
            if "models" in data:
                return [m["name"] for m in data["models"]]
    except Exception as e:
        log_to_file("Error fetching models", e)
        return []
    return []

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
        
        # Увеличил высоту окна, чтобы влезло второе поле
        width, height = 250, 360 
        model.Title = "LocalWriter Settings"
        model.PositionX = 100; model.PositionY = 100; model.Width = width; model.Height = height
        
        # 1. Middleware URL (Наш локальный Python сервер)
        self._add_label("lbl_mid", "Middleware URL (Local Backend):", 5, 5, 240, 10)
        self._add_edit("txt_mid", self.config.get("middleware_url", "http://localhost:8323"), 5, 17, 240, 15)
        
        # 2. Target Ollama URL (Удаленный сервер с GPU)
        self._add_label("lbl_ollama", "Inference Engine URL (Ollama):", 5, 40, 240, 10)
        self._add_edit("txt_ollama", self.config.get("ollama_url", "http://192.168.0.107:11434"), 5, 52, 240, 15)
        
        # 3. Model Selection
        self._add_label("lbl_model", "Model / Template:", 5, 75, 150, 10)
        self._add_button("btn_refresh", "Refresh List", 165, 73, 80, 14, "Refresh")
        self._add_listbox("lst_model", 5, 90, 240, 15)
        
        current_model = self.config.get("model", "")
        if current_model:
            self.dialog.getControl("lst_model").addItem(current_model, 0)
            self.dialog.getControl("lst_model").selectItem(current_model, True)

        # 4. Parameters
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
        self.dialog.createPeer(toolkit, None)
        self.dialog.execute()
        self.dialog.dispose()
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
        mid_url = self.dialog.getControl("txt_mid").getText()
        ollama_url = self.dialog.getControl("txt_ollama").getText()
        
        models = fetch_models(mid_url, ollama_url)
        
        listbox = self.dialog.getControl("lst_model")
        listbox.removeItems(0, listbox.getItemCount())
        if models:
            listbox.addItems(tuple(models), 0)
            listbox.selectItem(models[0], True)
        else:
            listbox.addItem("Connection failed", 0)

    def actionPerformed(self, actionEvent):
        cmd = actionEvent.ActionCommand
        if cmd == "Refresh": self._refresh_models()
        elif cmd == "OK":
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
        elif cmd == "Cancel":
            self.result = {}; self.dialog.endExecute()

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
            self.desktop = self.ctx.getServiceManager().createInstanceWithContext(
                "com.sun.star.frame.Desktop", self.ctx)
    
    def get_config(self, key, default):
        try:
            path_settings = self.sm.createInstanceWithContext('com.sun.star.util.PathSettings', self.ctx)
            user_config_path = getattr(path_settings, "UserConfig")
            if user_config_path.startswith('file://'):
                user_config_path = str(uno.fileUrlToSystemPath(user_config_path))
            
            config_file_path = os.path.join(user_config_path, "localwriter.json")
            if not os.path.exists(config_file_path): return default
            
            with open(config_file_path, 'r') as file:
                config_data = json.load(file)
            return config_data.get(key, default)
        except: return default

    def set_config(self, key, value):
        try:
            path_settings = self.sm.createInstanceWithContext('com.sun.star.util.PathSettings', self.ctx)
            user_config_path = getattr(path_settings, "UserConfig")
            if user_config_path.startswith('file://'):
                user_config_path = str(uno.fileUrlToSystemPath(user_config_path))

            config_file_path = os.path.join(user_config_path, "localwriter.json")
            config_data = {}
            if os.path.exists(config_file_path):
                with open(config_file_path, 'r') as file:
                    try: config_data = json.load(file)
                    except: pass
            
            config_data[key] = value
            with open(config_file_path, 'w') as file:
                json.dump(config_data, file, indent=4)
        except Exception as e:
            log_to_file("Config Write Error", e)

    def input_box(self, message, title="", default=""):
        import uno
        from com.sun.star.awt.PosSize import POS, SIZE
        from com.sun.star.awt.PushButtonType import OK
        
        ctx = uno.getComponentContext()
        smgr = ctx.getServiceManager()
        dialog = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", ctx)
        model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", ctx)
        dialog.setModel(model)
        dialog.setVisible(False); dialog.setTitle(title); dialog.setPosSize(0,0, 400, 100, SIZE)
        
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
        dialog.createPeer(toolkit, None)
        dialog.setPosSize(0,0,0,0, POS) 
        
        ret = ""
        if dialog.execute(): ret = dialog.getControl("edt").getModel().Text
        dialog.dispose()
        return ret

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
                    res = self.settings_box()
                    for k, v in res.items(): self.set_config(k, v)
                    return

                # Читаем конфигурацию
                middleware_url = self.get_config("middleware_url", "http://localhost:8323").rstrip('/')
                ollama_url = self.get_config("ollama_url", "http://localhost:11434").rstrip('/')
                model_name = self.get_config("model", "")
                
                # Формируем URL для Middleware
                url = f"{middleware_url}/v1/completions"
                
                # ВАЖНО: Добавляем целевой URL Ollama в заголовки
                headers = {
                    'Content-Type': 'application/json',
                    'X-Target-Ollama-Url': ollama_url
                }
                
                if not model_name:
                    log_to_file("ERROR: No model selected!")
                    return

                data = {}
                
                if args == "ExtendSelection":
                    prompt = text_range.getString()
                    if not prompt: return
                    sys_prompt = self.get_config("extend_selection_system_prompt", "")
                    if sys_prompt: prompt = f"SYSTEM PROMPT\n{sys_prompt}\nEND SYSTEM PROMPT\n{prompt}"
                    try: max_t = int(self.get_config("extend_selection_max_tokens", "100") or 100)
                    except: max_t = 100
                    data = {'model': model_name, 'prompt': prompt, 'max_tokens': max_t, 'stream': True}

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

                log_to_file(f"Requesting Middleware: {url} (Target: {ollama_url})")
                json_data = json.dumps(data).encode('utf-8')
                req = urllib.request.Request(url, data=json_data, headers=headers, method='POST')
                toolkit = self.ctx.getServiceManager().createInstanceWithContext("com.sun.star.awt.Toolkit", self.ctx)
                
                is_first_chunk = True 

                try:
                    with urllib.request.urlopen(req) as response:
                        log_to_file("Connection established. Reading stream...")
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
                        log_to_file("Stream finished successfully.")

                except urllib.error.HTTPError as e:
                    # Если бэкенд вернет ошибку (например, Ollama недоступна), мы запишем её
                    err_text = e.read().decode()
                    log_to_file(f"HTTP ERROR: {e.code} - {err_text}")

        except Exception as e:
            log_to_file("CRITICAL MAIN ERROR", e)

def main():
    try: ctx = XSCRIPTCONTEXT
    except NameError: ctx = officehelper.bootstrap()
    MainJob(ctx).trigger("settings")

if __name__ == "__main__": main()
g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(MainJob, "org.extension.sample.do", ("com.sun.star.task.Job",),)