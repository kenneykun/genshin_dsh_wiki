# -*- coding: utf-8 -*-
"""本地假 DeepSeek 服务：验证对话链路（不消耗真实额度）

- 第一次请求：返回一个 wiki_search 的 tool_call
- 检测到 tool 结果后：返回一段带 id 的答案
- 非流式（summary）：返回固定摘要
"""
from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8399


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages") or []
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        wants_stream = bool(body.get("stream"))
        print(f"[mock] messages={len(messages)} tool_result={has_tool_result} stream={wants_stream}", flush=True)

        if not wants_stream:
            text = (
                "## 对话摘要\n"
                "1. **主题**：钟离的台词与关系\n"
                "2. **结论**：钟离与若陀龙王、归终存在长期同行关系。\n"
                "3. **涉及词条**：钟离(id=1290)、若陀龙王(id=3866)\n"
                "4. **待查**：归终的具体出处。"
            )
            payload = json.dumps({
                "id": "mock", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def send(obj):
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        if not has_tool_result:
            send({"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "wiki_search", "arguments": json.dumps({"query": "钟离", "limit": 3}, ensure_ascii=False)}}
            ]}}]})
            send({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        else:
            for piece in ["根据资料库检索结果，", "钟离的台词里提到过 **岩王帝君** 与 **若陀龙王**。",
                          "\n\n- 出处：钟离 id=1290\n- 相关：若陀龙王 id=3866"]:
                send({"choices": [{"index": 0, "delta": {"content": piece}}]})
                time.sleep(0.05)
            send({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"mock DeepSeek on http://127.0.0.1:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
