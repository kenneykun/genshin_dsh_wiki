"""观测枢双通道 JSON 采集客户端

两条官方接口，都不需要浏览器：
- 旧模型（content_id 较小，如 111/1076）：blackboard content/info
- 新模型（5xxxxx 起的词条页）：hoyowiki entry_page

用法：
    from src.api_client import ApiClient
    with ApiClient() as api:
        kind, payload = api.fetch(111)
"""
from __future__ import annotations

import json
import threading
import time
from typing import Optional

import httpx
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.config import BASE_URL, REQUEST_TIMEOUT

OLD_API = "https://api-static.mihoyo.com/common/blackboard/ys_obc/v1/content/info"
NEW_API = "https://act-api-takumi-static.mihoyo.com/hoyowiki/genshin/wapi/entry_page"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": _UA,
    "Referer": f"{BASE_URL}/ys/obc/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}

# 这两类错误值得重试；4xx 之类直接放弃
_RETRYABLE = (httpx.TransportError, httpx.TimeoutException)


class ApiError(Exception):
    """接口返回了业务错误（retcode != 0）"""

    def __init__(self, retcode: int, message: str, url: str):
        super().__init__(f"retcode={retcode} message={message!r} url={url}")
        self.retcode = retcode
        self.message = message
        self.url = url


class ApiClient:
    """线程安全的双通道客户端：内部共用一个 httpx.Client。"""

    def __init__(self, timeout: float = REQUEST_TIMEOUT, workers: int = 8):
        self.timeout = timeout
        self.workers = workers
        limits = httpx.Limits(max_connections=workers * 2, max_keepalive_connections=workers * 2)
        self._client = httpx.Client(
            headers=HEADERS,
            timeout=httpx.Timeout(timeout),
            limits=limits,
            follow_redirects=True,
            http2=False,
        )
        self._lock = threading.Lock()
        self.stats = {"old": 0, "new": 0, "miss": 0, "error": 0, "bytes": 0}

    # ---------- 底层请求 ----------
    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1.0, min=1.0, max=8.0),
        reraise=True,
    )
    def _get_json(self, url: str, params: dict) -> dict:
        resp = self._client.get(url, params=params)
        resp.raise_for_status()
        with self._lock:
            self.stats["bytes"] += len(resp.content)
        return resp.json()

    @staticmethod
    def _payload_or_raise(data: dict, url: str) -> dict:
        if data.get("retcode") != 0:
            raise ApiError(data.get("retcode", -1), str(data.get("message")), url)
        return data.get("data") or {}

    # ---------- 单条抓取 ----------
    def fetch_old(self, content_id: int) -> dict:
        data = self._get_json(OLD_API, {"content_id": content_id, "app_sn": "ys_obc"})
        return self._payload_or_raise(data, OLD_API)

    def fetch_new(self, content_id: int) -> dict:
        data = self._get_json(NEW_API, {"entry_page_id": content_id})
        return self._payload_or_raise(data, NEW_API)

    def fetch(self, content_id: int, prefer_new: Optional[bool] = None) -> tuple[str, dict]:
        """返回 (kind, payload)。kind ∈ {'old','new'}，payload 已剥离 retcode 外壳。

        prefer_new=None 时按 id 大小猜（旧模型都是小 id），失败再切另一条通道。
        """
        order = []
        if prefer_new is True:
            order = ["new", "old"]
        elif prefer_new is False:
            order = ["old", "new"]
        else:
            order = ["old", "new"] if content_id < 5000 else ["new", "old"]

        last_err: Optional[Exception] = None
        for kind in order:
            try:
                payload = self.fetch_old(content_id) if kind == "old" else self.fetch_new(content_id)
                with self._lock:
                    self.stats[kind] += 1
                return kind, payload
            except ApiError as exc:
                last_err = exc
                continue

        with self._lock:
            self.stats["miss"] += 1
        raise last_err or ApiError(-1, "unknown", "")

    # ---------- 资源管理 ----------
    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class ProgressTracker:
    """批量进度：内存里累计，定时落盘，避免每条都全量重写 progress.json。"""

    def __init__(self, storage, flush_every: int = 40):
        self.storage = storage
        self.flush_every = flush_every
        self.progress = storage.load_progress()
        self._lock = threading.Lock()
        self._pending = 0

    def mark_done(self, content_id: int, path: str, title: str):
        with self._lock:
            self.progress["completed"][str(content_id)] = {
                "path": str(path),
                "title": title,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self.progress["failed"].pop(str(content_id), None)
            self._pending += 1
            if self._pending >= self.flush_every:
                self._flush_locked()

    def mark_failed(self, content_id: int, error: str):
        with self._lock:
            self.progress["failed"][str(content_id)] = {
                "error": error[:500],
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._pending += 1
            if self._pending >= self.flush_every:
                self._flush_locked()

    def _flush_locked(self):
        self.storage.save_progress(self.progress)
        self._pending = 0

    def flush(self):
        with self._lock:
            self._flush_locked()

    def completed_ids(self) -> set[int]:
        return {int(k) for k in self.progress["completed"]}


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
