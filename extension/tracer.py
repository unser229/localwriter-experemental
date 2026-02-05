import json
import os
import datetime
import traceback

class ExecutionTracer:
    def __init__(self):
        # Папка для логов: /tmp/localwriter_reports/
        self.report_dir = os.path.join("/tmp", "localwriter_reports")
        if not os.path.exists(self.report_dir):
            os.makedirs(self.report_dir)
            
        self.session_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.steps = []
        self.error = None
        
    def log_step(self, stage_name, input_data, output_data, notes=""):
        """Записывает этап обработки"""
        def safe_serialize(obj):
            try:
                if isinstance(obj, (str, int, float, bool, dict, list, type(None))):
                    return obj
                return str(obj)
            except: return "<Non-serializable>"

        step_record = {
            "timestamp": datetime.datetime.now().strftime("%H:%M:%S.%f"),
            "stage": stage_name,
            "input": safe_serialize(input_data),
            "output": safe_serialize(output_data),
            "notes": notes
        }
        self.steps.append(step_record)

    def log_error(self, stage_name, exception):
        self.error = {
            "stage": stage_name,
            "message": str(exception),
            "traceback": traceback.format_exc()
        }
        self.save_report()

    def save_report(self):
        """Сохраняет текущую сессию (автоматический лог)"""
        filename = f"trace_{self.session_id}.json"
        filepath = os.path.join(self.report_dir, filename)
        
        full_report = {
            "session_id": self.session_id,
            "steps": self.steps,
            "error": self.error
        }
        
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(full_report, f, indent=2, ensure_ascii=False)
            
            # Обновляем ссылку на ПОСЛЕДНИЙ отчет
            latest_path = os.path.join(self.report_dir, "latest_trace.json")
            with open(latest_path, "w", encoding="utf-8") as f:
                json.dump(full_report, f, indent=2, ensure_ascii=False)
                
            return filepath
        except Exception as e:
            print(f"Failed to save report: {e}")
            return None

    def get_latest_trace(self):
        """Читает последний сохраненный автоматический лог"""
        try:
            latest_path = os.path.join(self.report_dir, "latest_trace.json")
            if os.path.exists(latest_path):
                with open(latest_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except: pass
        return None

    def save_user_bug_report(self, user_comment, selected_text):
        """
        Сохраняет комбинированный отчет:
        Комментарий + Выделенный текст + История последнего выполнения
        """
        bug_file = os.path.join(self.report_dir, "bug_reports.jsonl")
        
        # Получаем контекст последнего выполнения (почему произошла ошибка)
        last_trace = self.get_latest_trace()
        
        report_entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "type": "USER_BUG_REPORT",
            "user_comment": user_comment,
            "selected_context": selected_text, # То, что пользователь выделил как ошибку
            "system_trace_snapshot": last_trace # Что система делала перед этим
        }
        
        try:
            # Append mode (добавляем в конец файла)
            with open(bug_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(report_entry, ensure_ascii=False) + "\n")
            return bug_file
        except Exception as e:
            return str(e)