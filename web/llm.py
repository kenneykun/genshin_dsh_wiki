"""资料库助手：DeepSeek 对话 + 本地检索工具循环

凭据与模型的解析顺序（都可覆盖）：
1. 环境变量 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL
2. 本机 data/llm_config.json（页面「设置」写入）
3. 本机 DSH 凭据库 ~/.dsh/.credentials.yaml 的 refs.DEEPSEEK_API_KEY

协议按 base_url 自动判断：
- 含 /anthropic → DeepSeek Messages 协议（x-api-key + /v1/messages），模型 deepseek-flash
- 其它 → OpenAI 兼容 /chat/completions，模型 deepseek-chat
这样默认就是「用这台机器上已经在跑的 deepseek-flash」，不需要额外配置。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator, Optional

import httpx

from src.config import DATA_DIR
from web import db as wiki_db

CONFIG_FILE = DATA_DIR / "llm_config.json"
DSH_CREDENTIALS = Path.home() / ".dsh" / ".credentials.yaml"

MESSAGES_BASE = "https://api.deepseek.com/anthropic"
OPENAI_BASE = "https://api.deepseek.com"
CREDENTIAL_ENV = "DEEPSEEK_API_KEY"

MAX_ROUNDS = 4
TOOL_RESULT_LIMIT = 6000
READ_TIMEOUT = 180.0
MAX_TOKENS = 4096

SYSTEM_PROMPT = """你是「原神观测枢资料库」的检索助手。资料库是本地镜像的原神 wiki 全站文本：角色、武器、圣遗物、敌人、书籍、任务、地图文本、NPC、成就、考据等 30 个分类，共 1.4 万条。

工作方式：
1. 用户问游戏设定、台词、剧情、材料、角色关系时，**先用工具查资料库**，不要凭记忆作答；
2. 台词类问题优先用 wiki_search 搜关键短语，再用 wiki_get_entry / wiki_get_section 取上下文；
3. 引用资料时用「标题」或「id」标注来源，方便用户回查；
4. 资料库没收录的内容，明确说「资料库中未收录」，不要编造；
5. 中文作答，简洁分点；长引用用引号包裹并注明出处。

可用工具：wiki_search、wiki_get_entry、wiki_get_text、wiki_get_section、wiki_related、wiki_list_categories。"""

# 同一份工具定义，按协议转换成 Messages 或 OpenAI 的 schema
TOOL_DEFS = [
    {
        "name": "wiki_search",
        "description": "在资料库里全文检索（标题+正文，中文按字索引）。返回条目 id、标题、分类、字数与命中片段。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词，如角色名、台词片段、地名"},
                "category": {"type": "string", "description": "可选，限定分类，如「角色」「任务」「书籍」"},
                "limit": {"type": "integer", "description": "返回条数，默认 5，最多 20"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "wiki_get_entry",
        "description": "按 id 读取词条的结构化正文（章节、表格、结构化字段）。长条目会截断。",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "max_chars": {"type": "integer", "description": "最多返回字数，默认 4000"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "wiki_get_text",
        "description": "分段读取词条纯文本正文，用于长任务/书籍/考据继续往下读。",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "offset": {"type": "integer", "description": "起始偏移，默认 0"},
                "max_chars": {"type": "integer", "description": "本段字数，默认 3000"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "wiki_get_section",
        "description": "只取词条的某一章节（标题模糊匹配）。",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "heading": {"type": "string"}},
            "required": ["id", "heading"],
        },
    },
    {
        "name": "wiki_related",
        "description": "取词条的关联网络：本文提及、被谁提及、延伸阅读。用于顺关系链查人物/组织。",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "limit": {"type": "integer"}},
            "required": ["id"],
        },
    },
    {
        "name": "wiki_list_categories",
        "description": "列出全部分类及条目数，用于确认资料库覆盖范围。",
        "parameters": {"type": "object", "properties": {}},
    },
]


# ---------------------------------------------------------------- 配置
def _read_dsh_key() -> str:
    """从 DSH 凭据库读 DeepSeek API Key（refs.DEEPSEEK_API_KEY）。"""
    try:
        text = DSH_CREDENTIALS.read_text(encoding="utf-8")
    except OSError:
        return ""
    in_refs = False
    for line in text.splitlines():
        if line.strip().startswith("refs:"):
            in_refs = True
            continue
        if in_refs:
            if line and not line.startswith((" ", "\t")):
                break
            if CREDENTIAL_ENV in line:
                _, _, value = line.partition(":")
                return value.strip().strip("'\"")
    return ""


def detect_protocol(base_url: str) -> str:
    return "messages" if "/anthropic" in (base_url or "") else "openai"


def load_config() -> dict:
    cfg = {"api_key": "", "model": "", "base_url": ""}
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    if not cfg.get("api_key"):
        cfg["api_key"] = _read_dsh_key()
    if os.environ.get(CREDENTIAL_ENV):
        cfg["api_key"] = os.environ[CREDENTIAL_ENV]
    if not cfg.get("base_url"):
        cfg["base_url"] = MESSAGES_BASE
    if os.environ.get("DEEPSEEK_BASE_URL"):
        cfg["base_url"] = os.environ["DEEPSEEK_BASE_URL"]

    protocol = detect_protocol(cfg["base_url"])
    if not cfg.get("model"):
        cfg["model"] = "deepseek-flash" if protocol == "messages" else "deepseek-chat"
    cfg["protocol"] = protocol
    return cfg


def model_catalog(protocol: str) -> list[dict]:
    if protocol == "messages":
        return [
            {"id": "deepseek-flash", "label": "deepseek-flash（默认，带工具）"},
            {"id": "deepseek-v4-pro", "label": "deepseek-v4-pro"},
        ]
    return [
        {"id": "deepseek-chat", "label": "deepseek-chat（带工具查询）"},
        {"id": "deepseek-reasoner", "label": "deepseek-reasoner（先查后想）"},
    ]


def save_config(patch: dict) -> dict:
    cfg = load_config()
    stored = {}
    if CONFIG_FILE.exists():
        try:
            stored = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            stored = {}
    if patch.get("clear_key"):
        stored["api_key"] = ""
    elif patch.get("api_key"):
        stored["api_key"] = str(patch["api_key"]).strip()
    if patch.get("model"):
        stored["model"] = str(patch["model"]).strip()
    if patch.get("base_url"):
        stored["base_url"] = str(patch["base_url"]).strip().rstrip("/")
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_config()


def public_config(cfg: Optional[dict] = None) -> dict:
    cfg = cfg or load_config()
    key = cfg.get("api_key") or ""
    protocol = cfg.get("protocol") or detect_protocol(cfg.get("base_url", ""))
    return {
        "has_key": bool(key),
        "key_hint": f"{key[:6]}…{key[-4:]}" if len(key) > 12 else ("已配置" if key else ""),
        "key_source": "环境变量" if os.environ.get(CREDENTIAL_ENV)
                      else "页面设置" if CONFIG_FILE.exists() and _stored_key()
                      else "本机 DSH 凭据" if key else "未配置",
        "model": cfg.get("model"),
        "base_url": cfg.get("base_url"),
        "protocol": protocol,
        "models": model_catalog(protocol),
    }


def _stored_key() -> str:
    try:
        return (json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}).get("api_key") or ""
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return ""


# ---------------------------------------------------------------- 工具执行
def _truncate(text: str, limit: int = TOOL_RESULT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n…（已截断，原文 {len(text)} 字）"


def run_tool(name: str, args: dict, conn) -> tuple[str, str]:
    """返回 (给模型的结果, 给界面看的摘要)。"""
    try:
        if name == "wiki_search":
            query = str(args.get("query") or "").strip()
            limit = max(1, min(20, int(args.get("limit") or 5)))
            hits = wiki_db.search(conn, query, category=args.get("category") or None, limit=limit)
            if not hits:
                return f"没有找到与「{query}」相关的内容。", f"检索「{query}」→ 0 条"
            lines = [f"共 {len(hits)} 条："]
            for hit in hits:
                lines.append(
                    f"- [{hit['id']}] {hit['title']}（{hit['category']}，{hit['chars']} 字，{hit['where']}命中）"
                )
                if hit.get("snippet"):
                    lines.append(f"  片段：{hit['snippet'][:160]}")
            return _truncate("\n".join(lines)), f"检索「{query}」→ {len(hits)} 条"

        if name == "wiki_get_entry":
            cid = int(args["id"])
            data = wiki_db.load_entry(conn, cid)
            if data is None:
                return f"没有 id={cid} 的条目。", f"读取词条 {cid} → 不存在"
            limit = max(500, min(20000, int(args.get("max_chars") or 4000)))
            parts = [f"# {data.get('title')}（{data.get('category')}）"]
            used = 0
            for heading, content in (data.get("sections") or {}).items():
                if used >= limit:
                    break
                chunk = f"\n## {heading}\n{content}"
                parts.append(chunk[: max(0, limit - used)])
                used += len(chunk)
            if (data.get("tables") or []) and used < limit:
                parts.append(f"\n（另有 {len(data['tables'])} 张数据表，需要时可取原文）")
            text = _truncate("".join(parts), limit)
            return text, f"读取词条「{data.get('title')}」（{len(text)} 字）"

        if name == "wiki_get_text":
            cid = int(args["id"])
            data = wiki_db.load_entry(conn, cid)
            if data is None:
                return f"没有 id={cid} 的条目。", f"读取正文 {cid} → 不存在"
            body = data.get("raw_text") or ""
            offset = max(0, int(args.get("offset") or 0))
            size = max(300, min(20000, int(args.get("max_chars") or 3000)))
            chunk = body[offset : offset + size]
            tail = offset + len(chunk)
            more = f"\n\n（未完，next_offset={tail}）" if tail < len(body) else "\n\n（已读完）"
            return _truncate(chunk + more), f"读正文「{data.get('title')}」{offset}-{tail} 字"

        if name == "wiki_get_section":
            cid = int(args["id"])
            heading = str(args.get("heading") or "")
            data = wiki_db.load_entry(conn, cid)
            if data is None:
                return f"没有 id={cid} 的条目。", f"取章节 {cid} → 不存在"
            for section_name, content in (data.get("sections") or {}).items():
                if heading and heading in section_name:
                    return _truncate(f"## {section_name}\n{content}"), f"章节「{section_name}」"
            return ("没有匹配的章节：" + "、".join(list((data.get("sections") or {}).keys())[:12])), "章节未命中"

        if name == "wiki_related":
            cid = int(args["id"])
            data = wiki_db.load_entry(conn, cid)
            if data is None:
                return f"没有 id={cid} 的条目。", f"关联 {cid} → 不存在"
            links = data.get("_links") or {}
            limit = max(1, min(20, int(args.get("limit") or 8)))
            lines = [f"# {data.get('title')} 的关联"]
            for label, key in (("本文提及", "mentions"), ("被提及", "mentioned_by"), ("延伸阅读", "related")):
                items = links.get(key) or []
                if items:
                    lines.append(f"\n{label}：" + "、".join(f"{i['title']}({i['id']})" for i in items[:limit]))
            return _truncate("\n".join(lines)), f"关联「{data.get('title')}」"

        if name == "wiki_list_categories":
            stats = wiki_db.stats(conn)
            lines = [f"共 {stats['entries']} 条 / {stats['chars']} 字："]
            lines += [f"- {c['name']}：{c['count']} 条" for c in stats["categories"]]
            return _truncate("\n".join(lines)), "列出分类"

        return f"未知工具：{name}", f"未知工具 {name}"
    except Exception as exc:  # noqa: BLE001 - 工具错误回给模型，不中断对话
        return f"工具 {name} 执行失败：{type(exc).__name__}: {exc}", f"{name} 失败"


# ---------------------------------------------------------------- Messages 协议（深寻官方）
def _messages_url(base_url: str) -> str:
    base = (base_url or MESSAGES_BASE).rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/messages"
    if base.endswith("/v1/messages"):
        return base
    return f"{base}/v1/messages"


def _messages_tools() -> list[dict]:
    return [
        {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
        for t in TOOL_DEFS
    ]


def _messages_stream(cfg: dict, system: str, messages: list[dict], use_tools: bool = True) -> Iterator[dict]:
    """流式调用 Messages 接口，产出 text / tool_use / stop 事件。"""
    payload = {
        "model": cfg["model"],
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": messages,
        "stream": True,
    }
    if use_tools:
        payload["tools"] = _messages_tools()
    headers = {
        "x-api-key": cfg["api_key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "accept": "text/event-stream",
    }
    blocks: dict[int, dict] = {}
    with httpx.Client(timeout=httpx.Timeout(30.0, read=READ_TIMEOUT)) as client:
        with client.stream("POST", _messages_url(cfg["base_url"]), headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", "replace")[:400]
                raise RuntimeError(f"模型接口返回 {resp.status_code}：{body}")
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if kind == "content_block_start":
                    block = event.get("content_block") or {}
                    index = event.get("index", 0)
                    if block.get("type") == "tool_use":
                        blocks[index] = {"id": block.get("id", ""), "name": block.get("name", ""), "json": ""}
                elif kind == "content_block_delta":
                    index = event.get("index", 0)
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield {"type": "text", "text": delta["text"]}
                    elif delta.get("type") == "input_json_delta" and index in blocks:
                        blocks[index]["json"] += delta.get("partial_json") or ""
                elif kind == "content_block_stop":
                    index = event.get("index", 0)
                    slot = blocks.pop(index, None)
                    if slot:
                        try:
                            args = json.loads(slot["json"] or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        yield {"type": "tool_use", "id": slot["id"], "name": slot["name"], "input": args}
                elif kind == "error":
                    message = (event.get("error") or {}).get("message") or str(event)
                    raise RuntimeError(f"模型返回错误：{message}")
                elif kind in ("message_stop", "message_delta"):
                    stop = (event.get("delta") or {}).get("stop_reason")
                    if stop:
                        yield {"type": "stop", "reason": stop}


def _run_messages_loop(cfg: dict, messages: list[dict], conn, on_event) -> None:
    convo = [dict(m) for m in messages]
    total_tools = 0
    for round_index in range(MAX_ROUNDS):
        on_event({"type": "status", "text": f"第 {round_index + 1} 轮：模型思考中…"})
        text = ""
        tool_uses = []
        for event in _messages_stream(cfg, SYSTEM_PROMPT, convo):
            if event["type"] == "text":
                text += event["text"]
                on_event({"type": "delta", "text": event["text"]})
            elif event["type"] == "tool_use":
                tool_uses.append(event)
        if not tool_uses:
            if not text:
                on_event({"type": "error", "message": "模型这一轮没有返回内容，可以再问一次或换个问法。"})
            return

        content: list[dict] = []
        if text:
            content.append({"type": "text", "text": text})
        for call in tool_uses:
            content.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["input"]})
        convo.append({"role": "assistant", "content": content})

        results: list[dict] = []
        for call in tool_uses:
            total_tools += 1
            on_event({"type": "status", "text": f"正在执行 {call['name']}…"})
            output, summary = run_tool(call["name"], call["input"], conn)
            on_event({"type": "tool", "name": call["name"], "args": call["input"], "summary": summary})
            results.append({"type": "tool_result", "tool_use_id": call["id"], "content": output})
        convo.append({"role": "user", "content": results})

    # 轮次用尽：明确要求收尾作答，且不再提供工具，否则模型会继续调工具而不出文字
    on_event({"type": "status", "text": "资料已查够，正在整理回答…"})
    convo.append({
        "role": "user",
        "content": "（工具轮次已用完，请不要再查资料，直接基于上面的结果给出最终回答；没有查到的部分明确说明。）",
    })
    answer = ""
    for event in _messages_stream(
        cfg,
        SYSTEM_PROMPT + "\n\n注意：本轮禁止继续检索，直接作答。",
        convo,
        use_tools=False,
    ):
        if event["type"] == "text":
            answer += event["text"]
            on_event({"type": "delta", "text": event["text"]})
    if not answer:
        on_event({"type": "error", "message": f"已调用 {total_tools} 次工具，但模型没有给出最终回答，可以再试一次。"})


# ---------------------------------------------------------------- OpenAI 兼容协议（自定义网关）
def _openai_url(base_url: str) -> str:
    base = (base_url or OPENAI_BASE).rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _openai_tools() -> list[dict]:
    return [
        {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
        for t in TOOL_DEFS
    ]


def _openai_stream(cfg: dict, messages: list[dict], use_tools: bool = True) -> Iterator[dict]:
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "stream": True,
        "temperature": 0.3,
    }
    if use_tools:
        payload["tools"] = _openai_tools()
        payload["tool_choice"] = "auto"
    headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
    with httpx.Client(timeout=httpx.Timeout(30.0, read=READ_TIMEOUT)) as client:
        with client.stream("POST", _openai_url(cfg["base_url"]), headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", "replace")[:400]
                raise RuntimeError(f"模型接口返回 {resp.status_code}：{body}")
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue


def _run_openai_loop(cfg: dict, messages: list[dict], conn, on_event) -> None:
    convo = [{"role": "system", "content": SYSTEM_PROMPT}, *messages]
    tool_rounds = 0
    for round_index in range(MAX_ROUNDS):
        on_event({"type": "status", "text": f"第 {round_index + 1} 轮：模型思考中…"})
        text_parts: list[str] = []
        calls: dict[int, dict] = {}
        for chunk in _openai_stream(cfg, convo):
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text_parts.append(delta["content"])
                    on_event({"type": "delta", "text": delta["content"]})
                for tc in delta.get("tool_calls") or []:
                    index = tc.get("index", 0)
                    slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
        if not calls:
            return
        tool_rounds += 1
        text = "".join(text_parts)
        convo.append({
            "role": "assistant",
            "content": text or "",
            "tool_calls": [
                {"id": c["id"] or f"call_{i}", "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                for i, c in sorted(calls.items())
            ],
        })
        for i, call in sorted(calls.items()):
            try:
                args = json.loads(call["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            on_event({"type": "status", "text": f"正在执行 {call['name']}…"})
            output, summary = run_tool(call["name"], args, conn)
            on_event({"type": "tool", "name": call["name"], "args": args, "summary": summary})
            convo.append({"role": "tool", "tool_call_id": call["id"] or f"call_{i}", "content": output})

    on_event({"type": "status", "text": "资料已查够，正在整理回答…"})
    convo.append({"role": "user", "content": "（工具轮次已用完，请直接给出最终回答，不要继续检索。）"})
    answer = ""
    for chunk in _openai_stream(cfg, convo, use_tools=False):
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                answer += delta["content"]
                on_event({"type": "delta", "text": delta["content"]})
    if not answer and tool_rounds:
        on_event({"type": "error", "message": "模型没有给出最终回答，可以再试一次。"})


# ---------------------------------------------------------------- 对外入口
def chat_stream(messages: list[dict], conn, on_event) -> None:
    """跑一轮对话，事件：delta / tool / error / done。"""
    cfg = load_config()
    if not cfg.get("api_key"):
        on_event({"type": "error", "message": "没有找到可用的 API Key：在页面「设置」里填一个，或确认本机 DSH 已登录 DeepSeek。"})
        return
    clean = [{"role": m["role"], "content": str(m.get("content") or "")}
             for m in messages if m.get("role") in ("user", "assistant")]
    if not clean:
        on_event({"type": "error", "message": "对话内容为空"})
        return
    try:
        if cfg["protocol"] == "messages":
            _run_messages_loop(cfg, clean, conn, on_event)
        else:
            _run_openai_loop(cfg, clean, conn, on_event)
    except Exception as exc:  # noqa: BLE001
        on_event({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        return
    on_event({"type": "done", "model": cfg["model"], "protocol": cfg["protocol"]})


def summarize(messages: list[dict]) -> str:
    """把当前对话压缩成结构化笔记。"""
    cfg = load_config()
    if not cfg.get("api_key"):
        raise RuntimeError("没有可用的 API Key")
    transcript = "\n".join(
        f"{'用户' if m.get('role') == 'user' else '助手'}：{str(m.get('content') or '')[:1500]}"
        for m in messages if m.get("role") in ("user", "assistant") and m.get("content")
    )
    if not transcript.strip():
        raise RuntimeError("对话还是空的")

    instruction = (
        "请总结下面这段关于《原神》资料库的对话，输出：\n"
        "1. 一句话主题；\n2. 3-6 条关键结论（含出处条目名/ID）；\n"
        "3. 涉及到的词条清单（标题 + id，若能识别）；\n4. 还没查清、值得继续查的点。\n\n"
        f"对话内容：\n{transcript[:12000]}"
    )

    if cfg["protocol"] == "messages":
        payload = {
            "model": cfg["model"],
            "max_tokens": 1024,
            "system": "你是资料整理助手。把对话总结成简体中文要点，不要编造对话里没有的信息。",
            "messages": [{"role": "user", "content": instruction}],
        }
        headers = {"x-api-key": cfg["api_key"], "anthropic-version": "2023-06-01", "content-type": "application/json"}
        with httpx.Client(timeout=httpx.Timeout(30.0, read=READ_TIMEOUT)) as client:
            resp = client.post(_messages_url(cfg["base_url"]), headers=headers, json=payload)
        if resp.status_code != 200:
            raise RuntimeError(f"模型接口返回 {resp.status_code}：{resp.text[:300]}")
        data = resp.json()
        return "".join(
            block.get("text", "") for block in (data.get("content") or []) if block.get("type") == "text"
        ).strip()

    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": "你是资料整理助手。把对话总结成简体中文要点，不要编造。"},
            {"role": "user", "content": instruction},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
    with httpx.Client(timeout=httpx.Timeout(30.0, read=READ_TIMEOUT)) as client:
        resp = client.post(_openai_url(cfg["base_url"]), headers=headers, json=payload)
    if resp.status_code != 200:
        raise RuntimeError(f"模型接口返回 {resp.status_code}：{resp.text[:300]}")
    data = resp.json()
    return ((data.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "").strip()
