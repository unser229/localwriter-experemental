"""
Сервис автосинхронизации документов при старте бэкенда.

Логика:
  1. Сканирует папку data/documents/ на наличие .docx файлов.
  2. Сравнивает SHA-256 хеши с манифестом data/.sync_manifest.json.
  3. Удаляет из ChromaDB "мёртвые" шаблоны (есть в манифесте, нет на диске).
  4. Индексирует новые файлы и переиндексирует изменённые (Delete → Add).
  5. Атомарно обновляет манифест.

Защита от Race Condition (multi-worker uvicorn):
  FileLock с timeout=0: первый воркер захватывает лок и делает работу,
  остальные молча пропускают (Timeout при попытке захватить занятый лок).
"""

import os
import json
import hashlib
import asyncio
from datetime import datetime
from pathlib import Path

from filelock import FileLock, Timeout

from app.services.rag_engine import rag_engine

# --- Пути ---
# os.getcwd() при запуске uvicorn из папки backend/ указывает на backend/
_DATA_DIR = Path(os.getcwd()) / "data"
WATCH_DIR = _DATA_DIR / "documents"
MANIFEST_PATH = _DATA_DIR / ".sync_manifest.json"
SYNC_LOCK_PATH = _DATA_DIR / ".sync.lock"


# ---------------------------------------------------------------------------
# Вспомогательные функции (синхронные, вызываются из asyncio.to_thread)
# ---------------------------------------------------------------------------

def _compute_sha256(path: Path) -> str:
    """Вычисляет SHA-256 хеш файла блоками (безопасно для больших файлов)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest() -> dict:
    """Читает манифест. Если файл отсутствует или повреждён — возвращает {}."""
    if not MANIFEST_PATH.exists():
        return {}
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_manifest(manifest: dict) -> None:
    """
    Атомарно записывает манифест через временный файл + os.replace.
    Гарантирует, что при сбое во время записи манифест не будет повреждён.
    """
    tmp_path = MANIFEST_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, MANIFEST_PATH)


def _delete_with_lock(filename: str) -> None:
    """Обёртка для удаления документа (вызывается через asyncio.to_thread)."""
    rag_engine.delete_document(filename)


def _add_with_lock(file_path: Path, filename: str) -> int:
    """
    Обёртка для индексации документа (вызывается через asyncio.to_thread).
    Возвращает количество добавленных чанков (заглушка — ChromaDB не возвращает это напрямую).
    """
    rag_engine.add_document(str(file_path), filename)
    return 0


# ---------------------------------------------------------------------------
# Основная логика синхронизации
# ---------------------------------------------------------------------------

async def _do_sync() -> None:
    """
    Полный цикл синхронизации. Вызывается только одним воркером (под FileLock).
    """
    # 0. Убеждаемся, что папка с документами существует
    WATCH_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Загружаем манифест и список файлов на диске
    manifest = _load_manifest()
    disk_files = {f.name: f for f in WATCH_DIR.glob("*.docx") if not f.name.startswith(".~lock")}

    stats = {"new": 0, "updated": 0, "deleted": 0, "skipped": 0, "errors": 0}

    # 2. ZOMBIE CLEANUP: есть в манифесте, нет на диске → удалить из ChromaDB
    zombie_names = set(manifest.keys()) - set(disk_files.keys())
    if zombie_names:
        print(f"🧹 Sync: найдено {len(zombie_names)} удалённых шаблонов, чистим базу...")
    for fname in zombie_names:
        try:
            await asyncio.to_thread(_delete_with_lock, fname)
            del manifest[fname]
            stats["deleted"] += 1
            print(f"   🧹 Удалён мёртвый шаблон: {fname}")
            # Сохраняем сразу после удаления — защита от прерывания на середине
            await asyncio.to_thread(_save_manifest, manifest)
        except Exception as e:
            stats["errors"] += 1
            print(f"   ❌ Ошибка удаления '{fname}': {e}")

    # 3. NEW / UPDATED: сравниваем хеши
    for fname, fpath in disk_files.items():
        try:
            # Вычисляем хеш в отдельном потоке, чтобы не блокировать event loop
            current_hash = await asyncio.to_thread(_compute_sha256, fpath)
            stored = manifest.get(fname, {})

            if not stored:
                # --- НОВЫЙ ФАЙЛ ---
                print(f"   ➕ Индексируем новый: {fname}")
                await asyncio.to_thread(_add_with_lock, fpath, fname)
                manifest[fname] = {
                    "sha256": current_hash,
                    "indexed_at": datetime.now().isoformat(timespec="seconds"),
                }
                stats["new"] += 1
                # Сохраняем сразу — если процесс убьют, этот файл не будет переиндексирован
                await asyncio.to_thread(_save_manifest, manifest)

            elif stored.get("sha256") != current_hash:
                # --- ИЗМЕНЁННЫЙ ФАЙЛ: Delete → Add ---
                print(f"   🔄 Переиндексируем изменённый: {fname}")
                await asyncio.to_thread(_delete_with_lock, fname)
                await asyncio.to_thread(_add_with_lock, fpath, fname)
                manifest[fname] = {
                    "sha256": current_hash,
                    "indexed_at": datetime.now().isoformat(timespec="seconds"),
                }
                stats["updated"] += 1
                # Сохраняем сразу — следующий старт увидит новый хеш
                await asyncio.to_thread(_save_manifest, manifest)

            else:
                # --- БЕЗ ИЗМЕНЕНИЙ — манифест не трогаем ---
                stats["skipped"] += 1

        except Exception as e:
            stats["errors"] += 1
            print(f"   ❌ Ошибка обработки '{fname}': {e}")
            # Манифест НЕ сохраняем для этого файла — при следующем старте попробуем снова

    # 5. Итоговый лог
    print(
        f"✅ Sync завершён: "
        f"+{stats['new']} новых, "
        f"~{stats['updated']} обновлено, "
        f"🗑 {stats['deleted']} удалено, "
        f"⏭  {stats['skipped']} без изменений"
        + (f", ❌ {stats['errors']} ошибок" if stats["errors"] else "")
    )


async def sync_documents_on_startup() -> None:
    """
    Точка входа. Захватывает FileLock (timeout=0 — non-blocking).
    Если лок занят другим воркером — молча выходим.
    """
    try:
        # timeout=0: не ждать, сразу выйти если лок занят
        with FileLock(str(SYNC_LOCK_PATH), timeout=0):
            print(f"🔍 Sync: сканируем '{WATCH_DIR}' ...")
            await _do_sync()
    except Timeout:
        print("⏭️  Sync: другой воркер уже проводит синхронизацию, пропускаем.")
    except Exception as e:
        # Синхронизация не должна убивать сервер
        print(f"❌ Sync: неожиданная ошибка — {e}")
