"""生成条目关联图谱：正文互相提及 → 引用关系 → 关联推荐

产出写入 data/output/wiki.db 的附加表：
    mentions(src, dst, n)        A 的正文提到 B 几次
    related(id, other, score, reason)  每个词条的关联推荐（12 条以内）
    nodes(id, indegree, keywords)      被引用次数与关键词

用法：
    .venv\\Scripts\\python.exe -m src.build_graph --limit 500   # 抽样试跑
    .venv\\Scripts\\python.exe -m src.build_graph             # 全量
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

from loguru import logger

from src.config import OUTPUT_DIR
from web.db import DB_FILE

# 标题太泛、或本身就是噪声的，不参与匹配
SKIP_TITLES = {
    "暂无", "默认标题", "无", "名称", "描述", "目录", "攻略", "教程", "任务",
    "活动", "敌人", "角色", "武器", "食物", "材料", "地图", "图片", "位置",
    "编者注", "补充说明", "基础信息", "详细信息", "数据表", "图集", "语音",
}
MIN_TITLE_LEN = 2
MAX_MENTIONS_PER_ENTRY = 40
GENERIC_INDEGREE = 300  # 被 300 个以上词条提到，视为公共消耗品/通用名词


class AhoCorasick:
    """纯 Python AC 自动机：一次扫描找出正文里所有标题命中。"""

    __slots__ = ("goto", "fail", "out")

    def __init__(self):
        self.goto: list[dict[str, int]] = [{}]
        self.fail: list[int] = [0]
        self.out: list[list[int]] = [[]]

    def add(self, word: str, key: int) -> None:
        node = 0
        for ch in word:
            nxt = self.goto[node].get(ch)
            if nxt is None:
                self.goto.append({})
                self.fail.append(0)
                self.out.append([])
                nxt = len(self.goto) - 1
                self.goto[node][ch] = nxt
            node = nxt
        self.out[node].append(key)

    def build(self) -> None:
        queue = deque(self.goto[0].values())
        while queue:
            node = queue.popleft()
            for ch, nxt in self.goto[node].items():
                queue.append(nxt)
                f = self.fail[node]
                while f and ch not in self.goto[f]:
                    f = self.fail[f]
                self.fail[nxt] = self.goto[f].get(ch, 0)
                if self.fail[nxt] == nxt:
                    self.fail[nxt] = 0
                self.out[nxt].extend(self.out[self.fail[nxt]])

    def find(self, text: str):
        goto, fail, out = self.goto, self.fail, self.out
        node = 0
        for ch in text:
            while node and ch not in goto[node]:
                node = fail[node]
            node = goto[node].get(ch, 0)
            if out[node]:
                yield from out[node]


def load_entries(limit: int | None = None) -> list[dict]:
    rows = []
    for path in OUTPUT_DIR.rglob("*.json"):
        if path.name in ("index.json", "progress.json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        title = (data.get("title") or "").strip()
        cid = data.get("content_id")
        if not title or not isinstance(cid, int):
            continue
        rows.append({
            "id": cid,
            "title": title,
            "category": path.parent.name,
            "chars": len(data.get("raw_text") or ""),
            "text": data.get("raw_text") or "",
        })
        if limit and len(rows) >= limit:
            break
    return rows


def build_lookup(entries: list[dict]) -> tuple[AhoCorasick, dict[int, list[int]]]:
    """标题 → 词条 id。同名标题按「优先分类」挑一个代表，避免把提及全算到重复条目上。"""
    priority = {
        "角色": 0, "武器": 1, "圣遗物": 2, "敌人": 3, "组织": 4, "书籍": 5,
        "任务": 6, "教程": 7, "食物": 8, "动物": 9, "NPC与商店": 10,
        "洞天": 11, "背包": 12, "名片": 13, "成就": 14, "地图文本": 15,
    }
    by_title: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        title = e["title"]
        if len(title) < MIN_TITLE_LEN or title in SKIP_TITLES:
            continue
        if title.isdigit():
            continue
        by_title[title].append(e)

    automaton = AhoCorasick()
    title_to_ids: dict[int, list[int]] = {}
    for key, (title, items) in enumerate(sorted(by_title.items())):
        items.sort(key=lambda e: (priority.get(e["category"], 99), -e["chars"]))
        automaton.add(title, key)
        title_to_ids[key] = [e["id"] for e in items[:2]]
    automaton.build()
    return automaton, title_to_ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建条目关联图谱")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 条（试跑）")
    args = parser.parse_args(argv)

    started = time.time()
    entries = load_entries(args.limit)
    logger.info(f"载入 {len(entries)} 条词条")
    automaton, title_to_ids = build_lookup(entries)
    logger.info(f"标题自动机节点 {len(automaton.goto)}，可匹配标题 {len(title_to_ids)}")

    mentions: dict[int, Counter] = {}
    for i, entry in enumerate(entries, 1):
        counter: Counter = Counter()
        for key in automaton.find(entry["text"]):
            for target in title_to_ids[key]:
                if target != entry["id"]:
                    counter[target] += 1
        mentions[entry["id"]] = counter
        if i % 1000 == 0:
            logger.info(f"  匹配进度 {i}/{len(entries)}  用时 {time.time() - started:.0f}s")

    total_links = sum(len(c) for c in mentions.values())
    logger.info(f"匹配完成：{total_links} 条提及关系，用时 {time.time() - started:.0f}s")

    # ---------------- 关联推荐 ----------------
    indegree: Counter = Counter()
    citers: dict[int, set[int]] = defaultdict(set)   # 谁提到我
    for src, counter in mentions.items():
        for dst in counter:
            indegree[dst] += 1
            citers[dst].add(src)

    # 通用消耗品（原石 / 摩拉 / 冒险阅历…）几乎每页都提，用来做关联只会把结果带偏
    generic = {dst for dst, n in indegree.items() if n >= GENERIC_INDEGREE}
    logger.info(f"识别到 {len(generic)} 个通用词条（被引 ≥ {GENERIC_INDEGREE}），不参与关联推荐")

    cat_members: dict[str, list[int]] = defaultdict(list)
    for entry in entries:
        if entry["id"] not in generic:
            cat_members[entry["category"]].append(entry["id"])
    for members in cat_members.values():
        members.sort(key=lambda cid: -indegree.get(cid, 0))

    related: dict[int, list[tuple[int, float, str]]] = {}
    for entry in entries:
        cid = entry["id"]
        seeds = [dst for dst, _ in mentions[cid].most_common(25) if dst not in generic]
        scores: Counter = Counter()
        for seed in seeds:
            weight = 1.0 / (1.0 + len(citers[seed]) ** 0.5)   # 冷门提及更有区分度
            for other in citers[seed]:
                if other != cid and other not in generic:
                    scores[other] += weight
        picked: list[tuple[int, float, str]] = []
        seen: set[int] = set()
        for other, score in scores.most_common(12):
            picked.append((other, round(score, 3), "co-mention"))
            seen.add(other)

        # 共引：引用同一批目标的条目
        co_cited: Counter = Counter()
        for other_src in citers.get(cid, set()):
            for dst in mentions.get(other_src, {}):
                if dst != cid and dst not in generic and dst not in seen:
                    co_cited[dst] += 1
        for other, score in co_cited.most_common(6):
            if len(picked) >= 12:
                break
            if other not in seen:
                picked.append((other, round(float(score), 3), "co-cited"))
                seen.add(other)

        # 兜底：同分类里被引最多的条目，保证每个词条都有拓展阅读入口
        if len(picked) < 6:
            for other in cat_members.get(entry["category"], []):
                if other == cid or other in seen:
                    continue
                picked.append((other, 0.0, "same-category"))
                seen.add(other)
                if len(picked) >= 6:
                    break
        related[cid] = picked[:12]

    # ---------------- 写库 ----------------
    conn = sqlite3.connect(DB_FILE)
    conn.executescript(
        """
        DROP TABLE IF EXISTS mentions;
        DROP TABLE IF EXISTS related;
        DROP TABLE IF EXISTS nodes;
        CREATE TABLE mentions(src INTEGER, dst INTEGER, n INTEGER, PRIMARY KEY (src, dst));
        CREATE TABLE related(id INTEGER, other INTEGER, score REAL, reason TEXT, PRIMARY KEY (id, other));
        CREATE TABLE nodes(id INTEGER PRIMARY KEY, indegree INTEGER, mentions_out INTEGER);
        CREATE INDEX idx_mentions_dst ON mentions(dst);
        CREATE INDEX idx_related_id ON related(id);
        """
    )
    conn.executemany(
        "INSERT INTO mentions(src, dst, n) VALUES (?,?,?)",
        [(src, dst, n) for src, counter in mentions.items() for dst, n in counter.most_common(MAX_MENTIONS_PER_ENTRY)],
    )
    conn.executemany(
        "INSERT INTO related(id, other, score, reason) VALUES (?,?,?,?)",
        [(cid, other, score, reason) for cid, items in related.items() for other, score, reason in items],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO nodes(id, indegree, mentions_out) VALUES (?,?,?)",
        [(e["id"], indegree.get(e["id"], 0), len(mentions.get(e["id"], {}))) for e in entries],
    )
    conn.commit()
    conn.close()

    top = indegree.most_common(10)
    logger.success(
        f"图谱完成：{total_links} 条提及 / {sum(len(v) for v in related.values())} 条关联，"
        f"总用时 {time.time() - started:.0f}s"
    )
    logger.info(f"被提及最多：{top[:5]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
