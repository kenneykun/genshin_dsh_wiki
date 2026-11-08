"""批量生成词条梗概卡（DeepSeek 并发批处理）

给每个词条产出一张结构化「梗概卡」：抓人标题 / 一句话亮点 / 3 个看点 / 标签，
写入 wiki.db 的 notes 表，信息流与条目页直接复用。

用法：
    .venv\\Scripts\\python.exe -m src.gen_notes --limit 100            # 生成 100 条
    .venv\\Scripts\\python.exe -m src.gen_notes --limit 100 --workers 8
    .venv\\Scripts\\python.exe -m src.gen_notes --category 角色 -l 20
    .venv\\Scripts\\python.exe -m src.gen_notes --dry-run -l 5          # 只看提示词
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import httpx
from loguru import logger

from src.config import OUTPUT_DIR
from web import db as wiki_db
from web import llm

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes(
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    hook TEXT,
    highlights TEXT,
    tags TEXT,
    model TEXT,
    created_at TEXT
);
"""

PROMPT_TEMPLATE = """你在给《原神》资料库做「梗概卡」。请阅读下面这段 wiki 原文，输出**一个 JSON 对象**（不要解释、不要 Markdown 代码块）：

{{"title": "≤18 字的抓人标题", "hook": "一句话亮点，≤40 字", "highlights": ["看点1", "看点2", "看点3"], "tags": ["标签1", "标签2", "标签3", "标签4", "标签5"]}}

规则：
- 只使用原文信息，不要编造游戏设定；
- highlights 每条 ≤30 字，写具体内容（谁、在哪、做了什么），不要空话；
- tags 用简短名词（角色名/地名/组织/机制），便于检索；
- 原文信息不足时，highlights 少给几条也可以。

词条：{title}（分类：{category}）
原文：
{text}
"""


# ---------------------------------------------------------------- 模型调用
def _call(cfg: dict, prompt: str, timeout: float = 120.0) -> str:
    payload = {
        "model": cfg["model"],
        "max_tokens": 1100,
        "system": "你是信息提炼助手，输出严格 JSON。",
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": cfg["api_key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    url = llm._messages_url(cfg["base_url"])
    with httpx.Client(timeout=httpx.Timeout(30.0, read=timeout)) as client:
        resp = client.post(url, headers=headers, json=payload)
    if resp.status_code != 200:
        raise RuntimeError(f"模型返回 {resp.status_code}：{resp.text[:200]}")
    data = resp.json()
    return "".join(
        block.get("text", "") for block in (data.get("content") or []) if block.get("type") == "text"
    ).strip()


def _repair_json(text: str) -> dict:
    """模型输出被截断时，尽量从残缺 JSON 里捞出可用字段。"""
    result = {}
    for field in ("title", "hook"):
        match = re.search(rf'"{field}"\s*:\s*"([^"]*)', text)
        if match:
            result[field] = match.group(1)
    for field in ("highlights", "tags"):
        block = re.search(rf'"{field}"\s*:\s*\[(.*?)(?:\]|$)', text, re.S)
        if block:
            items = re.findall(r'"([^"]+)', block.group(1))
            if items:
                result[field] = items[:5]
    if not result.get("title") and not result.get("hook"):
        raise ValueError("无法修复的输出")
    return result


def _parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0:
        raise ValueError(f"没有 JSON：{text[:120]}")
    if end <= start:
        return _repair_json(text)
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return _repair_json(text)


# ---------------------------------------------------------------- 数据准备
def _candidate_entries(limit: int, category: Optional[str], skip_done: bool = True) -> list[dict]:
    conn = wiki_db.connect()
    try:
        conn.executescript(SCHEMA)
        sql = (
            "SELECT e.id, e.title, e.category, e.chars FROM entries e "
            + ("LEFT JOIN notes n ON n.id = e.id " if skip_done else "")
            + "WHERE e.chars >= 400 "
            + ("AND n.id IS NULL " if skip_done else "")
            + ("AND e.category = ? " if category else "")
            + "ORDER BY (SELECT COALESCE(indegree,0) FROM nodes WHERE nodes.id = e.id) DESC, e.chars DESC "
            + "LIMIT ?"
        )
        params = ((category,) if category else ()) + (limit,)
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _entry_text(content_id: int, category: str, limit: int = 2600) -> str:
    path = OUTPUT_DIR / category / f"{content_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return ""
    parts = []
    used = 0
    for heading, content in (data.get("sections") or {}).items():
        if heading in ("_intro",) or heading.startswith("页签"):
            continue
        chunk = f"## {heading}\n{content}".strip()
        if not chunk:
            continue
        parts.append(chunk)
        used += len(chunk)
        if used >= limit:
            break
    text = "\n\n".join(parts)
    return text[:limit]


# ---------------------------------------------------------------- 主流程
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量生成词条梗概卡")
    parser.add_argument("-l", "--limit", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--category", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    cfg = llm.load_config()
    if not cfg.get("api_key"):
        logger.error("没有可用的 API Key")
        return 1
    if cfg.get("protocol") != "messages":
        logger.error(f"当前协议是 {cfg.get('protocol')}，梗概生成按 Messages 协议实现")
        return 1

    entries = _candidate_entries(args.limit, args.category)
    if not entries:
        logger.success("没有待生成的词条（可能都生成过了）")
        return 0
    logger.info(f"待生成 {len(entries)} 条，模型 {cfg['model']}，并发 {args.workers}")

    if args.dry_run:
        sample = entries[0]
        text = _entry_text(sample["id"], sample["category"])
        print(PROMPT_TEMPLATE.format(title=sample["title"], category=sample["category"], text=text[:600]))
        return 0

    result = generate(args.limit, args.workers, args.category)
    logger.success(
        f"生成完成：成功 {result['ok']}，失败 {result['fail']}，用时 {result['seconds']:.0f}s"
    )
    return 0 if result["ok"] or not result["fail"] else 1


def generate(limit: int = 100, workers: int = 8, category: Optional[str] = None,
             progress=None) -> dict:
    """批量生成梗概卡。progress(done, total, ok, fail) 可选，供界面显示进度。"""
    cfg = llm.load_config()
    entries = _candidate_entries(limit, category)
    total = len(entries)
    stats = {"ok": 0, "fail": 0, "total": total, "seconds": 0.0}
    if not entries:
        return stats

    conn = wiki_db.connect()
    conn.executescript(SCHEMA)
    lock = threading.Lock()
    started = time.time()

    def work(entry: dict) -> tuple[dict, Optional[dict]]:
        text = _entry_text(entry["id"], entry["category"])
        if not text:
            return entry, None
        prompt = PROMPT_TEMPLATE.format(title=entry["title"], category=entry["category"], text=text)
        for attempt in range(2):
            try:
                note = _parse_json(_call(cfg, prompt))
                note["id"] = entry["id"]
                return entry, note
            except Exception as exc:  # noqa: BLE001
                if attempt == 1:
                    logger.warning(f"[{entry['id']}] {entry['title']} 失败：{type(exc).__name__}: {exc}")
                    return entry, None
                time.sleep(1.0)
        return entry, None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(work, entry) for entry in entries]
        for index, future in enumerate(as_completed(futures), 1):
            entry, note = future.result()
            with lock:
                if note:
                    conn.execute(
                        "INSERT OR REPLACE INTO notes(id, title, hook, highlights, tags, model, created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (
                            note["id"],
                            str(note.get("title") or entry["title"])[:40],
                            str(note.get("hook") or "")[:120],
                            json.dumps(note.get("highlights") or [], ensure_ascii=False),
                            json.dumps(note.get("tags") or [], ensure_ascii=False),
                            cfg["model"],
                            time.strftime("%Y-%m-%d %H:%M:%S"),
                        ),
                    )
                    conn.commit()
                    stats["ok"] += 1
                else:
                    stats["fail"] += 1
            if index % 10 == 0 or index == total:
                logger.info(f"  进度 {index}/{total}（成功 {stats['ok']}，失败 {stats['fail']}）")
            if progress:
                progress(index, total, stats["ok"], stats["fail"])

    conn.close()
    stats["seconds"] = round(time.time() - started, 1)
    return stats


if __name__ == "__main__":
    sys.exit(main())
