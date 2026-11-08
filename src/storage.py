import json
import time
from pathlib import Path
from typing import Optional

from loguru import logger

from src.config import OUTPUT_DIR, DATA_DIR

INDEX_FILE = OUTPUT_DIR / "index.json"
PROGRESS_FILE = OUTPUT_DIR / "progress.json"
TEXT_DIR = OUTPUT_DIR / "text"


class Storage:
    def __init__(self, output_dir: Path = OUTPUT_DIR):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        TEXT_DIR.mkdir(parents=True, exist_ok=True)

    def _category_dir(self, category: str) -> Path:
        folder = self.output_dir / category
        folder.mkdir(exist_ok=True)
        return folder

    def save_detail(self, detail: dict, category: str = "其他"):
        cid = detail["content_id"]
        folder = self._category_dir(category)
        path = folder / f"{cid}.json"
        path.write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def save_plaintext(self, content_id: int, title: str, raw_text: str):
        if not raw_text or not raw_text.strip():
            return
        path = TEXT_DIR / f"{content_id}.txt"
        text = f"{title}\n{'=' * len(title)}\n\n{raw_text}"
        path.write_text(text, encoding="utf-8")
        return path

    def load_index(self) -> list[dict]:
        if not INDEX_FILE.exists():
            return []
        try:
            return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("index.json 损坏，重新生成")
            return []

    def save_index(self, entries: list[dict]):
        data = json.dumps(entries, ensure_ascii=False, indent=2)
        tmp = INDEX_FILE.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(INDEX_FILE)

    def load_progress(self) -> dict:
        if not PROGRESS_FILE.exists():
            return {"completed": {}, "failed": {}, "last_update": ""}
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning(f"progress.json 损坏，重置")
            return {"completed": {}, "failed": {}, "last_update": ""}

    def save_progress(self, progress: dict):
        progress["last_update"] = time.strftime("%Y-%m-%d %H:%M:%S")
        data = json.dumps(progress, ensure_ascii=False, indent=2)
        tmp_path = PROGRESS_FILE.with_suffix(".tmp")
        tmp_path.write_text(data, encoding="utf-8")
        tmp_path.replace(PROGRESS_FILE)

    def mark_completed(self, content_id: int, file_path: str, detail: Optional[dict] = None):
        progress = self.load_progress()
        progress["completed"][str(content_id)] = {
            "path": str(file_path),
            "title": detail.get("title", "") if detail else "",
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        progress["failed"].pop(str(content_id), None)
        self.save_progress(progress)

    def mark_failed(self, content_id: int, error: str):
        progress = self.load_progress()
        progress["failed"][str(content_id)] = {
            "error": error,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save_progress(progress)

    def is_completed(self, content_id: int) -> bool:
        progress = self.load_progress()
        return str(content_id) in progress["completed"]

    def get_failed_ids(self) -> list[int]:
        progress = self.load_progress()
        return [int(k) for k in progress["failed"].keys()]
