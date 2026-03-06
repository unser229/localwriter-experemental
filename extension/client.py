"""
Клиент LibreOffice-расширения для взаимодействия с backend (/app).

Чистый Python, без UNO-зависимостей.
Содержит ту же HTTP-логику, что extension/main.py выполняет изнутри LibreOffice.
Используется напрямую тестами (test_formatting_quality.py) для точной имитации
поведения расширения без участия пользователя.
"""

import re
import json
import time
import urllib.request
import urllib.error
import urllib.parse
import os


# ============================================================================
# JSON-парсер (скопирован 1-в-1 из extension/main.py, строки 37-66)
# ============================================================================

def extract_json_from_text(text):
    """
    Fuzzy-извлечение JSON из сырого ответа LLM.
    Ищет: markdown-блок ```json```, чистый массив [...], объект {...}.
    Аналог функции в main.py — используется и расширением, и тестом.
    """
    if not text:
        return None

    # Заменяем HTML-сущности (response из LibreOffice WebView иногда их добавляет)
    text = text.replace("&nbsp;", " ").replace("&quot;", '"')

    # 1. Markdown ```json ... ```
    match = re.search(r'```json\s*([\s\S]*?)\s*```', text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except Exception:
            pass

    # 2. Markdown ``` ... ```
    match = re.search(r'```\s*([\s\S]*?)\s*```', text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except Exception:
            pass

    # 3. Чистый JSON-массив
    match = re.search(r'^\s*\[[\s\S]*\]\s*$', text)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass

    # 4. Прямой json.loads
    try:
        return json.loads(text)
    except Exception:
        pass

    # 5. Поиск первого [ ... ]
    try:
        start = text.find('[')
        end = text.rfind(']') + 1
        if start != -1 and end > start:
            return json.loads(text[start:end])
    except Exception:
        pass

    return None


# ============================================================================
# Подготовка контента (скопировано из main.py, строка 316)
# ============================================================================

def clean_content_for_llm(text: str) -> str:
    """
    Очищает markdown-артефакты перед отправкой в LLM.
    Точно соответствует main.py:316: re.sub(r'[*#]', '', content)
    """
    return re.sub(r'[*#]', '', text)


# ============================================================================
# HTTP-вызовы к backend (логика из main.py trigger("ApplyTemplate"))
# ============================================================================

def call_apply_template(
    content: str,
    model: str,
    middleware_url: str,
    timeout: int = 300,
) -> tuple:
    """
    Отправляет текст документа на /v1/completions.
    Точно воспроизводит trigger("ApplyTemplate") из main.py:310-347:
      - очищает markdown
      - строит payload
      - делает HTTP POST
      - парсит JSON из ответа через extract_json_from_text

    Возвращает: (parsed_list | None, elapsed_sec, rag_template_id | None)
    """
    # Очистка (main.py:316)
    clean_content = clean_content_for_llm(content)

    # Payload (main.py:317-325)
    payload_prompt = f"=== USER CONTENT (CONTENT SOURCE) ===\n{clean_content}"
    data = {
        'model': model,
        'prompt': payload_prompt,
        'stream': False,
        'format': 'json',
        'options': {'num_ctx': 8192},
    }

    # Заголовки
    headers = {
        'Content-Type': 'application/json',
    }

    url = f"{middleware_url.rstrip('/')}/v1/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode(),
        headers=headers,
        method='POST',
    )

    start = time.time()
    structure = []
    accumulated_raw = ""
    rag_template_id = None
    
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            # Читаем заголовки
            rag_template_id = response.headers.get('X-Best-Template-ID')
            if rag_template_id:
                rag_template_id = urllib.parse.unquote(rag_template_id)
                
            # Читаем NDJSON построчно
            for line in response:
                line_str = line.decode('utf-8').strip()
                if not line_str: continue # Heartbeat
                
                accumulated_raw += line_str + "\n"
                
                try:
                    obj = json.loads(line_str)
                    if "error" in obj:
                        print(f"Backend Error: {obj['error']}")
                        break
                    structure.append(obj)
                except json.JSONDecodeError:
                    pass
                    
    except Exception as e:
        return None, time.time() - start, None

    elapsed = time.time() - start

    # Если бэкенд вернул пустой список — логируем аккумулированный raw-ответ для отладки
    if not structure:
        # Записываем raw-ответ в /tmp/localwriter_raw.log
        try:
            with open('/tmp/localwriter_raw.log', 'a', encoding='utf-8') as f:
                f.write(f"\n--- RAW RESPONSE ({len(accumulated_raw)} chars) ---\n{accumulated_raw[:2000]}\n")
        except Exception:
            pass

    return structure, elapsed, rag_template_id

import threading
import queue

def call_apply_template_ndjson(
    content: str | list[str],
    model: str,
    middleware_url: str,
    result_queue: queue.Queue,
    stop_event: threading.Event,
    timeout_per_line: int = 20,
) -> tuple[bool, str]:
    """
    НОВАЯ АРХИТЕКТУРА (Шаг 4): Клиентский батчинг + NDJSON.
    
    1. Нарезает контент на параграфы и присваивает глобальные ID (1..N).
    2. Разделяет на батчи (BATCH_SIZE = 15).
    3. Шлет POST /v1/completions для каждого батча.
    4. Бэкенд возвращает каждую строчку как {"id": ID, "style_name": ...}.
    5. Клиент кладет результат в очередь, макрос в LivreOffice применяет стиль по ID.
    """
    
    # 1. Формирование глобального ID-массива параграфов
    paragraphs = []
    if isinstance(content, str):
        # LibreOffice присылает длинную строку, разбиваем по \n
        raw_paragraphs = content.split('\n')
    else:
        # Либо напрямую список строк (от extract_ground_truth в тестах)
        raw_paragraphs = content
        
    for i, raw_text in enumerate(raw_paragraphs):
        text_clean = clean_content_for_llm(raw_text).strip()
        if text_clean:
            paragraphs.append({"id": i, "text": text_clean})

    if not paragraphs:
        result_queue.put({"DONE": True})
        return False, None

    BATCH_SIZE = 15
    is_degraded = False
    rag_template_id = None
    first_batch = True

    def _ndjson_reader():
        nonlocal is_degraded, rag_template_id, first_batch
        
        try:
            for batch_start in range(0, len(paragraphs), BATCH_SIZE):
                if stop_event.is_set():
                    break
                    
                batch = paragraphs[batch_start : batch_start + BATCH_SIZE]
                
                # Формируем payload для нового гибридного API
                # Отправляем JSON-массив параграфов внутри prompt
                payload_prompt = f"=== USER CONTENT (CONTENT SOURCE) ===\n{json.dumps(batch, ensure_ascii=False)}"
                
                data = {
                    'model': model,
                    'prompt': payload_prompt,
                    'stream': False, # Запускает NDJSON-стриминг на сервере (proxy_completions)
                    'options': {},
                }

                url = f"{middleware_url.rstrip('/')}/v1/completions"
                req = urllib.request.Request(
                    url,
                    data=json.dumps(data).encode(),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                
                try:
                    response = urllib.request.urlopen(req, timeout=30)
                    
                    if first_batch:
                        is_degraded = response.headers.get('X-Degraded-Mode') == 'true'
                        rag_tid = response.headers.get('X-Best-Template-ID')
                        if rag_tid:
                            rag_template_id = urllib.parse.unquote(rag_tid)
                        first_batch = False
                        
                    # Читаем этот батч
                    while not stop_event.is_set():
                        line = response.readline()
                        if not line:
                            break # Конец потока/батча
                            
                        line_str = line.decode('utf-8').strip()
                        if not line_str:
                            continue # Heartbeat
                            
                        try:
                            parsed_obj = json.loads(line_str)
                            if "error" in parsed_obj:
                                result_queue.put({"error": parsed_obj["error"]})
                                return # Фатальная ошибка, прерываем всё
                            
                            # Бэкенд возвращает {"id": N, "style_name": "..."}
                            # Передаем макросу LibreOffice
                            result_queue.put(parsed_obj)
                        except json.JSONDecodeError:
                            pass
                            
                    response.close()
                    
                except Exception as batch_e:
                    result_queue.put({"error": f"Batch Error: {str(batch_e)}"})
                    return
                    
        except Exception as e:
            if not stop_event.is_set():
                result_queue.put({"error": f"Network Error: {str(e)}"})
        finally:
            result_queue.put({"DONE": True})

    # Запускаем обработку батчей в фоне
    t = threading.Thread(target=_ndjson_reader, daemon=True)
    t.start()
    
    # Возвращаем заглушки для первого запроса, так как заголовки появятся позже
    return False, None


def call_ingest(docx_path: str, middleware_url: str, timeout: int = 120) -> dict:
    """
    Загружает .docx файл через POST /api/ingest для индексации в RAG.
    Возвращает {'status': 'indexed', 'uuid': '...'} или {'error': '...'}.
    """
    url = f"{middleware_url.rstrip('/')}/api/ingest"
    filename = os.path.basename(docx_path)

    try:
        with open(docx_path, 'rb') as f:
            file_data = f.read()

        boundary = "----FormBoundary" + str(int(time.time()))
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n"
            f"\r\n"
        ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            url,
            data=body,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    except Exception as e:
        return {'error': str(e)}


def call_extract_ground_truth(docx_path: str, middleware_url: str, timeout: int = 120) -> dict:
    """
    Извлекает ground truth через POST /api/extract_ground_truth.
    Возвращает {'ground_truth': [...], 'plain_text': '...'} или {'error': '...'}.
    """
    url = f"{middleware_url.rstrip('/')}/api/extract_ground_truth"
    filename = os.path.basename(docx_path)

    try:
        with open(docx_path, 'rb') as f:
            file_data = f.read()

        boundary = "----FormBoundary" + str(int(time.time())) + "GT"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n"
            f"\r\n"
        ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            url,
            data=body,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    except Exception as e:
        return {'error': str(e)}


# ============================================================================
# Валидация UNO-совместимости (uno_formatter.apply_structure ожидает эти поля)
# ============================================================================

# Поля, которые uno_formatter.apply_structure() читает напрямую
_UNO_REQUIRED = {'text'}
_UNO_EXPECTED = {'style_name', 'type', 'font_family', 'font_size', 'bold', 'align'}


def validate_uno_fields(structure: list) -> dict:
    """
    Проверяет, что каждый объект в structure совместим с uno_formatter.apply_structure().
    Возвращает:
      {
        'total': int,
        'compatible': int,        # имеют 'text'
        'with_style': int,        # имеют 'style_name'
        'compat_pct': float,
        'missing_text': [...],    # примеры объектов без 'text'
      }
    """
    if not structure:
        return {'total': 0, 'compatible': 0, 'with_style': 0,
                'compat_pct': 0.0, 'missing_text': []}

    compatible = 0
    with_style = 0
    missing_text = []

    for item in structure:
        if not isinstance(item, dict):
            continue
        has_text = 'text' in item and bool(item['text'])
        if has_text:
            compatible += 1
        else:
            if len(missing_text) < 3:
                missing_text.append(str(item)[:80])
        if item.get('style_name'):
            with_style += 1

    total = len(structure)
    compat_pct = round(compatible / total * 100, 1) if total else 0.0

    return {
        'total': total,
        'compatible': compatible,
        'with_style': with_style,
        'compat_pct': compat_pct,
        'missing_text': missing_text,
    }


# ============================================================================
# Проверка соединения (из actionPerformed/CheckConn в main.py)
# ============================================================================

def check_connection(middleware_url: str, timeout: int = 15) -> list:
    """
    Проверяет соединение с backend и возвращает список доступных моделей.
    Используется диалогом настроек (CheckConn).
    Возвращает список имён моделей. При ошибке — бросает исключение.
    """
    target = f"{middleware_url.rstrip('/')}/api/tags"
    headers = {
        "User-Agent": "LocalWriter-Client",
    }
    req = urllib.request.Request(target, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    models = [m.get("name") for m in data.get("models", [])]
    return models


# ============================================================================
# Streaming-запросы (из trigger/ExtendSelection/EditSelection в main.py)
# ============================================================================

def call_streaming_completion(
    prompt: str,
    model: str,
    middleware_url: str,
):
    """
    Генератор: делает POST /v1/completions со stream=True и итеративно
    возвращает текстовые дельты из SSE-потока.

    Использование:
        for delta in call_streaming_completion(prompt, model, url):
            # delta — строка с очередным фрагментом ответа
    """
    url = f"{middleware_url.rstrip('/')}/v1/completions"
    data = {
        'model': model,
        'prompt': prompt,
        'stream': True,
    }
    headers = {
        'Content-Type': 'application/json',
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode(),
        headers=headers,
        method='POST',
    )
    with urllib.request.urlopen(req) as response:
        for line in response:
            if line.startswith(b"data: "):
                try:
                    payload = line[6:].decode()
                    if payload.strip() == "[DONE]":
                        break
                    chunk = json.loads(payload)
                    delta = (
                        chunk.get("response", "")
                        or chunk.get("choices", [{}])[0].get("text", "")
                    )
                    if delta:
                        yield delta
                except Exception:
                    pass
