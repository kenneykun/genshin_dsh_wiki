"""本地资料库 Web 服务（标准库，无额外依赖）

用法：
    .venv\\Scripts\\python.exe -m web.server            # 默认 http://127.0.0.1:8321
    .venv\\Scripts\\python.exe -m web.server --open     # 顺便打开浏览器
    .venv\\Scripts\\python.exe -m web.server --port 9000
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from loguru import logger

from web import db, llm, manage

STATIC_DIR = Path(__file__).parent / "static"
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")

_conn = None
_conn_lock = threading.Lock()


def get_conn():
    global _conn
    with _conn_lock:
        if _conn is None:
            _conn = db.connect()
        return _conn


def close_conn() -> None:
    """重建索引前释放共享连接（Windows 上文件被占用无法删除/替换）。"""
    global _conn
    with _conn_lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:  # noqa: BLE001
                pass
            _conn = None


def reopen_conn() -> None:
    get_conn()


class Handler(BaseHTTPRequestHandler):
    server_version = "YuanshenWiki/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------- 基础响应 ----------------
    def _send(self, status: int, body: bytes, content_type: str, cache: str = "no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str):
        self._json({"error": message}, status=status)

    def log_message(self, fmt: str, *args):
        logger.debug(f"{self.address_string()} {fmt % args}")

    # ---------------- 路由 ----------------
    def do_GET(self):
        try:
            self._route()
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - 服务端兜底
            logger.exception(f"请求失败: {self.path}")
            try:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")
            except Exception:
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        try:
            self._route_post()
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"POST 失败: {self.path}")
            try:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")
            except Exception:
                pass

    def _route_post(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._error(HTTPStatus.BAD_REQUEST, "请求体不是合法 JSON")
        if not isinstance(body, dict):
            return self._error(HTTPStatus.BAD_REQUEST, "请求体必须是 JSON 对象")

        if path == "/api/chat/config":
            return self._json(llm.public_config(llm.save_config(body)))

        if path == "/api/chat/summary":
            try:
                summary = llm.summarize(body.get("messages") or [])
            except RuntimeError as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return self._json({"summary": summary})

        if path == "/api/chat":
            return self._chat(body)

        if path == "/api/manage/run":
            result = manage.start(str(body.get("action") or ""), body.get("params") or {})
            if "error" in result:
                return self._error(HTTPStatus.BAD_REQUEST, result["error"])
            return self._json(result)

        return self._error(HTTPStatus.NOT_FOUND, "未知接口")

    def _chat(self, body: dict):
        """流式对话：SSE，事件为 delta / tool / error / done。"""
        messages = body.get("messages") or []
        if not isinstance(messages, list) or not messages:
            return self._error(HTTPStatus.BAD_REQUEST, "messages 不能为空")

        conn = db.connect()  # 独立连接，避免与查询请求共用同一 sqlite 对象
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def emit(event: dict):
            payload = "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            self.wfile.write(payload.encode("utf-8"))
            self.wfile.flush()

        try:
            llm.chat_stream(messages, conn, emit)
        except Exception as exc:  # noqa: BLE001 - 失败也要把原因推给前端
            logger.warning(f"对话失败: {type(exc).__name__}: {exc}")
            emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            emit({"type": "done", "tools": []})
        finally:
            conn.close()

    def _route(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}

        if path.startswith("/api/"):
            return self._api(path, query)
        return self._static(path)

    # ---------------- API ----------------
    def _api(self, path: str, query: dict):
        try:
            return self._api_inner(path, query)
        except sqlite3.OperationalError:
            # 重建索引期间连接可能指向已替换的文件，丢一次再试
            logger.warning(f"数据库连接失效，重连后重试：{path}")
            close_conn()
            return self._api_inner(path, query)

    def _api_inner(self, path: str, query: dict):
        conn = get_conn()

        if path == "/api/stats":
            return self._json(db.stats(conn))

        if path == "/api/chat/config":
            return self._json(llm.public_config())

        if path == "/api/manage/overview":
            return self._json(manage.overview())

        if path == "/api/manage/status":
            return self._json(manage.status())

        if path == "/api/entries":
            category = query.get("category") or None
            page = int(query.get("page", 1))
            size = min(500, max(1, int(query.get("size", 200))))
            return self._json(db.entries_in(conn, category, page, size))

        if path == "/api/search":
            q = (query.get("q") or "").strip()
            if not q:
                return self._json({"q": q, "items": []})
            limit = min(100, max(1, int(query.get("limit", 40))))
            items = db.search(conn, q, category=query.get("category") or None, limit=limit)
            return self._json({"q": q, "count": len(items), "items": items})

        if path.startswith("/api/entry/"):
            try:
                content_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._error(HTTPStatus.BAD_REQUEST, "content_id 必须是整数")
            data = db.load_entry(conn, content_id)
            if data is None:
                return self._error(HTTPStatus.NOT_FOUND, "条目不存在")
            return self._json(data)

        if path == "/api/random":
            cid = db.random_entry(conn, query.get("category") or None)
            if cid is None:
                return self._error(HTTPStatus.NOT_FOUND, "没有条目")
            return self._json({"id": cid})

        if path == "/api/featured":
            limit = int(query.get("limit", 12))
            return self._json({"items": db.featured(conn, limit)})

        if path == "/api/feed":
            category = query.get("category") or None
            offset = max(0, int(query.get("offset", 0)))
            size = int(query.get("size", 24))
            mode = query.get("mode") or "hot"
            return self._json(db.feed(conn, category, offset, size, mode))

        if path == "/api/explore":
            limit = int(query.get("limit", 24))
            return self._json({"items": db.explore_notes(conn, limit)})

        if path.startswith("/api/related/"):
            try:
                content_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._error(HTTPStatus.BAD_REQUEST, "content_id 必须是整数")
            return self._json(db.links_for(conn, content_id))

        return self._error(HTTPStatus.NOT_FOUND, "未知接口")

    # ---------------- 静态文件 ----------------
    def _static(self, path: str):
        if path in ("/", "/index.html"):
            target = STATIC_DIR / "index.html"
        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = (STATIC_DIR / rel).resolve()
            if not str(target).startswith(str(STATIC_DIR.resolve())):
                return self._error(HTTPStatus.FORBIDDEN, "越界路径")
        else:
            return self._error(HTTPStatus.NOT_FOUND, "文件不存在")
        if not target.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "文件不存在")
        body = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        return self._send(HTTPStatus.OK, body, ctype, cache="no-cache")


def serve(host: str = "127.0.0.1", port: int = 8321, open_browser: bool = False) -> None:
    if not db.DB_FILE.exists():
        logger.warning(f"索引不存在，请先运行: python -m web.build_db （缺少 {db.DB_FILE}）")
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    url = f"http://{host}:{port}/"
    logger.success(f"资料库已启动: {url}")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("正在关闭…")
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动本地资料库 Web 前端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8321)
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args(argv)
    serve(args.host, args.port, args.open)
    return 0


if __name__ == "__main__":
    sys.exit(main())
