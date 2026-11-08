"""探索式笔记：让模型自己翻资料库，自己决定写什么

和 src/gen_notes.py（按热度挑词条、直接压缩）不同，这里是**自主浏览**：

    模型 → 自己搜关键词 → 读词条 / 看章节 / 查关联 → 判断哪条最值得写 → 产出笔记

多个 explorer 并发跑，各带一个「兴趣种子」（冷门地名、语音反差、食谱来历…），
互相不重复取材，凑够目标条数就停。

    .venv\\Scripts\\python.exe -m src.explore_notes -n 20 --agents 4
    .venv\\Scripts\\python.exe -m src.explore_notes -n 3 --agents 1 --verbose
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import httpx
from loguru import logger

from web import db as wiki_db
from web import llm

MAX_ROUNDS = 6
SCHEMA = """
CREATE TABLE IF NOT EXISTS explore_notes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    primary_id INTEGER,
    title TEXT NOT NULL,
    hook TEXT,
    highlights TEXT,
    tags TEXT,
    sources TEXT,
    agent_seed TEXT,
    model TEXT,
    created_at TEXT
);
"""

INTEREST_SEEDS = [
    "冷门地名与它们的来历",
    "角色语音里的性格反差",
    "某道料理和它的家乡",
    "书籍里提到的神话体系",
    "敌人图鉴里容易被忽略的细节",
    "成就名称背后的梗",
    "武器描述里藏着的往事",
    "圣遗物套装之间的叙事",
    "NPC 的碎碎念与世界观",
    "地图文本里的小故事",
    "洞天摆设的由来",
    "同一个事件的两种说法",
    "看起来不起眼但设定很重的道具",
    "组织之间的关系网",
    "时间线里的矛盾点",
]

EXPLORER_SYSTEM = """你是《原神》资料库的探索者，一个好奇心很重的读者。

你手里有一套检索工具，资料库是本地镜像的原神 wiki 全站文本（角色/武器/圣遗物/敌人/书籍/任务/地图文本/NPC/成就/考据等 30 类，1.4 万条）。

你的工作方式：
1. 先用 wiki_search / wiki_list_categories / wiki_browse 找线索，至少读 2 个不同词条；
2. 读到有意思的地方可以继续深挖（wiki_get_section / wiki_get_text / wiki_related）；
3. 挑出**一条**你觉得最值得分享的内容——最好是具体、有画面感、别人不一定知道的细节；
4. 不要写成百科条目，要像在跟朋友分享一个发现。

注意：只用资料库里真实存在的信息，不要编造。"""

FINAL_PROMPT = """时间到了。把你刚才读到的最值得分享的那条内容，写成一条笔记，**只输出 JSON**（不要解释、不要代码块）：

{"title": "≤18 字的抓人标题", "hook": "一句话亮点，≤40 字", "highlights": ["看点1", "看点2", "看点3"], "tags": ["标签1", "标签2", "标签3", "标签4"], "source_ids": [你实际读过的词条 id]}

要求：
- title 要有分享欲，可以带「冷知识」「原来」「没想到」这类钩子，但不要标题党到失真；
- highlights 写具体内容（谁、在哪、做了什么、为什么有意思），每条 ≤40 字；
- source_ids 必须是你这次真正读过的词条 id。"""


# ---------------------------------------------------------------- 模型调用（带工具）
def _messages_url(base_url: str) -> str:
    return llm._messages_url(base_url)


def _call(cfg: dict, messages: list[dict], tools: Optional[list] = None, timeout: float = 180.0) -> dict:
    payload = {
        "model": cfg["model"],
        "max_tokens": 1500,
        "system": EXPLORER_SYSTEM,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
    headers = {
        "x-api-key": cfg["api_key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    with httpx.Client(timeout=httpx.Timeout(60.0, read=timeout)) as client:
        resp = client.post(_messages_url(cfg["base_url"]), headers=headers, json=payload)
    if resp.status_code != 200:
        raise RuntimeError(f"模型返回 {resp.status_code}：{resp.text[:200]}")
    return resp.json()


def _blocks(data: dict) -> tuple[str, list[dict]]:
    text, tools = "", []
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text += block.get("text", "")
        elif block.get("type") == "tool_use":
            tools.append(block)
    return text.strip(), tools


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"没有完整 JSON：{text[:120]}")
    return json.loads(text[start : end + 1])


# ---------------------------------------------------------------- 单个探索者
class Explorer:
    def __init__(self, seed: str, index: int, cfg: dict, verbose: bool = False):
        self.seed = seed
        self.index = index
        self.cfg = cfg
        self.verbose = verbose
        self.conn = wiki_db.connect()
        self.visited: list[int] = []

    def close(self) -> None:
        self.conn.close()

    def _log(self, message: str) -> None:
        if self.verbose:
            logger.info(f"  [探索者{self.index}] {message}")

    def explore(self) -> Optional[dict]:
        """跑一轮探索，产出 1 条笔记（失败返回 None）。"""
        messages = [{
            "role": "user",
            "content": (
                f"你这次特别想了解的方向：{self.seed}。\n"
                "先搜两三个关键词，至少读 2 个词条，再决定写哪一条。直接开始。"
            ),
        }]
        for round_index in range(MAX_ROUNDS):
            data = _call(self.cfg, messages, tools=llm._messages_tools())
            text, tool_uses = _blocks(data)
            if not tool_uses:
                self._log(f"第 {round_index + 1} 轮读完，准备收尾")
                break
            content = []
            if text:
                content.append({"type": "text", "text": text})
            for call in tool_uses:
                content.append({
                    "type": "tool_use", "id": call["id"],
                    "name": call["name"], "input": call.get("input") or {},
                })
            messages.append({"role": "assistant", "content": content})

            results = []
            for call in tool_uses:
                output, summary = llm.run_tool(call["name"], call.get("input") or {}, self.conn)
                for hit in re.findall(r"\[(\d{2,7})\]", output):
                    cid = int(hit)
                    if cid not in self.visited:
                        self.visited.append(cid)
                self._log(f"{summary}")
                results.append({"type": "tool_result", "tool_use_id": call["id"], "content": output})
            messages.append({"role": "user", "content": results})

        messages.append({"role": "user", "content": FINAL_PROMPT})
        for attempt in range(2):
            try:
                data = _call(self.cfg, messages, tools=None)
                text, _ = _blocks(data)
                note = _parse_json(text)
                ids = [int(i) for i in (note.get("source_ids") or []) if str(i).isdigit()]
                note["source_ids"] = ids or self.visited[:3]
                return note
            except Exception as exc:  # noqa: BLE001
                if attempt == 1:
                    self._log(f"收尾失败：{type(exc).__name__}: {exc}")
                    return None
                messages.append({"role": "user", "content": "JSON 不完整，请重新只输出一个合法 JSON 对象。"})
        return None


# ---------------------------------------------------------------- 主流程
def run(count: int = 20, agents: int = 4, verbose: bool = False, progress=None) -> dict:
    cfg = llm.load_config()
    if not cfg.get("api_key") or cfg.get("protocol") != "messages":
        raise RuntimeError("需要 Messages 协议的 DeepSeek 凭据")

    write_conn = wiki_db.connect()
    write_conn.executescript(SCHEMA)
    write_conn.commit()
    used_titles = {row[0] for row in write_conn.execute("SELECT title FROM explore_notes")}

    seeds = INTEREST_SEEDS[:]
    random.shuffle(seeds)
    lock = threading.Lock()
    state = {"done": 0, "fail": 0}
    started = time.time()
    slots = max(1, min(agents, count))

    def agent_task(index: int) -> None:
        explorer = Explorer(seeds[index % len(seeds)], index + 1, cfg, verbose)
        try:
            for _ in range(count):
                with lock:
                    if state["done"] >= count:
                        return
                note = explorer.explore()
                if not note:
                    with lock:
                        state["fail"] += 1
                    continue
                source_ids = [int(i) for i in (note.get("source_ids") or []) if str(i).isdigit()]
                with lock:
                    if str(note.get("title") or "") in used_titles:
                        state["fail"] += 1
                        continue
                    _save(write_conn, note, cfg["model"], explorer.seed)
                    state["done"] += 1
                    if progress:
                        progress(state["done"], count, str(note.get("title") or ""))
        finally:
            explorer.close()

    with ThreadPoolExecutor(max_workers=slots) as pool:
        futures = [pool.submit(agent_task, i) for i in range(slots)]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"探索者异常：{type(exc).__name__}: {exc}")

    write_conn.close()
    return {"ok": state["done"], "fail": state["fail"], "seconds": round(time.time() - started, 1)}


def _save(conn, note: dict, model: str, seed: str) -> None:
    sources = [int(i) for i in (note.get("source_ids") or []) if str(i).isdigit()]
    conn.execute(
        "INSERT INTO explore_notes(primary_id, title, hook, highlights, tags, sources, agent_seed, model, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            sources[0] if sources else 0,
            str(note.get("title") or "")[:40],
            str(note.get("hook") or "")[:120],
            json.dumps(note.get("highlights") or [], ensure_ascii=False),
            json.dumps(note.get("tags") or [], ensure_ascii=False),
            json.dumps(sources, ensure_ascii=False),
            seed,
            model,
            time.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="探索式生成笔记")
    parser.add_argument("-n", "--count", type=int, default=20)
    parser.add_argument("--agents", type=int, default=4)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    def report(ok, total, title):
        logger.info(f"[{ok}/{total}] {title}")

    result = run(args.count, args.agents, args.verbose, report)
    logger.success(
        f"探索完成：成功 {result['ok']}，失败 {result['fail']}，用时 {result['seconds']}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
