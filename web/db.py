"""SQLite 索引：建库 + 检索

中文全文检索用「按字切分 + FTS5」：
- FTS5 自带的 trigram 分词器要 3 个字以上才能命中（"蒙德" 搜不到），而且索引更大；
- 把汉字逐字用空格隔开，unicode61 就能按字建索引，任意长度都能精确匹配，
  实测 1884 万字索引约 51MB、查询毫秒级。
"""
from __future__ import annotations

import json
import random
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from src.config import OUTPUT_DIR
from src.dedupe_sections import dedupe_block
from src.storage import INDEX_FILE

DB_FILE = OUTPUT_DIR / "wiki.db"

CJK_RANGES = (
    (0x3400, 0x4DBF),   # 扩展 A
    (0x4E00, 0x9FFF),   # 基本区
    (0xF900, 0xFAFF),   # 兼容汉字
    (0x20000, 0x2FA1F), # 扩展 B+
)
_CJK_MIN = 0x3400
_CJK_MAX = 0x2FA1F


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    if code < _CJK_MIN or code > _CJK_MAX:
        return False
    return any(lo <= code <= hi for lo, hi in CJK_RANGES)


def segment(text: str) -> str:
    """汉字逐字加空格，其他字符原样保留（拉丁词仍按整词建索引）。"""
    out: list[str] = []
    for ch in text:
        if _is_cjk(ch):
            out.append(" ")
            out.append(ch)
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def build_match_query(raw: str) -> str:
    """把用户输入转成 FTS5 查询：每个词按字切分后作为短语，词之间 AND。"""
    terms = [t for t in raw.replace("\u3000", " ").split(" ") if t]
    if not terms:
        return ""
    parts = []
    for term in terms:
        seg = " ".join(segment(term).split())
        seg = seg.replace('"', "")
        if not seg:
            continue
        parts.append(f'"{seg}"')
    return " AND ".join(parts)


@dataclass
class Hit:
    id: int
    title: str
    category: str
    chars: int


def connect(db_file: Path = DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(db_file, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------- 建库
def build_index(output_dir: Path = OUTPUT_DIR, db_file: Path = DB_FILE, progress=None) -> dict:
    """重建检索索引。progress(scanned, total) 可选，用于把建库进度推给界面。"""
    entries = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    cat_of = {int(e["content_id"]): (e.get("category") or "misc") for e in entries}
    url_of = {int(e["content_id"]): (e.get("url") or "") for e in entries}
    title_of = {int(e["content_id"]): (e.get("title") or "") for e in entries}
    total_json = sum(
        1 for folder in output_dir.iterdir()
        if folder.is_dir() and folder.name != "text"
        for _ in folder.glob("*.json")
    )

    # 原地重建表，而不是删库重建：Windows 上其他进程（网页服务 / MCP 子进程）
    # 持有 wiki.db 句柄时，删除文件会报 WinError 32
    conn = sqlite3.connect(db_file, timeout=30)
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = NORMAL;
        DROP TABLE IF EXISTS fts;
        DROP TABLE IF EXISTS entries;
        CREATE TABLE entries(
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            chars INTEGER NOT NULL DEFAULT 0,
            kind TEXT,
            url TEXT,
            sections INTEGER DEFAULT 0,
            tables INTEGER DEFAULT 0,
            images INTEGER DEFAULT 0
        );
        CREATE INDEX idx_entries_cat ON entries(category, title);
        CREATE VIRTUAL TABLE fts USING fts5(body, content='', tokenize='unicode61');
        """
    )

    rows: list[tuple] = []
    fts_rows: list[tuple] = []
    seen: set[int] = set()
    started = time.time()

    for folder in sorted(p for p in output_dir.iterdir() if p.is_dir()):
        category = folder.name
        if category == "text":
            continue
        for path in folder.glob("*.json"):
            try:
                cid = int(path.stem)
            except ValueError:
                continue
            if cid in seen:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            seen.add(cid)
            body = data.get("raw_text") or ""
            rows.append((
                cid,
                (data.get("title") or title_of.get(cid) or str(cid)).strip(),
                cat_of.get(cid) or category,
                len(body),
                data.get("api_kind") or "legacy",
                url_of.get(cid) or "",
                len(data.get("sections") or {}),
                len(data.get("tables") or []),
                len(data.get("images") or []),
            ))
            fts_rows.append((cid, segment(body)))
            if progress and len(rows) % 2000 == 0:
                progress(len(rows), total_json)

    conn.executemany(
        "INSERT INTO entries(id,title,category,chars,kind,url,sections,tables,images) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.executemany("INSERT INTO fts(rowid, body) VALUES (?, ?)", fts_rows)
    conn.commit()
    conn.execute("INSERT INTO fts(fts) VALUES('optimize')")
    conn.commit()
    conn.close()

    return {
        "entries": len(rows),
        "seconds": round(time.time() - started, 1),
        "db_mb": round(db_file.stat().st_size / 1024 / 1024, 1),
    }


# ---------------------------------------------------------------- 查询
def _snippet(body: str, terms: Iterable[str], width: int = 70) -> str:
    lowered = body
    best = -1
    for term in terms:
        pos = lowered.find(term)
        if pos >= 0 and (best < 0 or pos < best):
            best = pos
    if best < 0:
        return body[: width * 2].replace("\n", " ")
    start = max(0, best - width)
    end = min(len(body), best + width)
    text = body[start:end].replace("\n", " ")
    return ("…" if start else "") + text + ("…" if end < len(body) else "")


def search(
    conn: sqlite3.Connection,
    query: str,
    category: Optional[str] = None,
    limit: int = 40,
) -> list[dict]:
    """标题命中优先，其次正文命中（FTS5 按字索引）。"""
    terms = [t for t in query.split() if t]
    if not terms:
        return []

    results: dict[int, dict] = {}

    if category:
        title_rows = conn.execute(
            "SELECT id,title,category,chars FROM entries "
            "WHERE category=? AND title LIKE ? ORDER BY chars DESC LIMIT ?",
            (category, f"%{terms[0]}%", limit),
        ).fetchall()
    else:
        title_rows = conn.execute(
            "SELECT id,title,category,chars FROM entries "
            "WHERE title LIKE ? ORDER BY chars DESC LIMIT ?",
            (f"%{terms[0]}%", limit),
        ).fetchall()

    for row in title_rows:
        results[row["id"]] = {
            "id": row["id"], "title": row["title"], "category": row["category"],
            "chars": row["chars"], "where": "title", "snippet": "",
        }

    match = build_match_query(query)
    if match:
        sql = ("SELECT e.id, e.title, e.category, e.chars FROM fts "
               "JOIN entries e ON e.id = fts.rowid WHERE fts MATCH ? ORDER BY rank LIMIT ?")
        try:
            body_rows = conn.execute(sql, (match, limit)).fetchall()
        except sqlite3.OperationalError:
            body_rows = []
        for row in body_rows:
            if row["id"] in results:
                continue
            if category and row["category"] != category:
                continue
            results[row["id"]] = {
                "id": row["id"], "title": row["title"], "category": row["category"],
                "chars": row["chars"], "where": "body", "snippet": "",
            }

    ordered = sorted(results.values(), key=lambda r: (r["where"] != "title", -r["chars"]))[:limit]

    # 正文命中补片段：只读一次文件
    for item in ordered:
        if item["where"] == "body":
            path = OUTPUT_DIR / item["category"] / f"{item['id']}.json"
            try:
                body = json.loads(path.read_text(encoding="utf-8")).get("raw_text") or ""
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                body = ""
            item["snippet"] = _snippet(body, terms)
    return ordered


def stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    chars = conn.execute("SELECT COALESCE(SUM(chars),0) FROM entries").fetchone()[0]
    cats = [
        {"name": row["category"], "count": row["count"], "chars": row["chars"]}
        for row in conn.execute(
            "SELECT category, COUNT(*) AS count, SUM(chars) AS chars "
            "FROM entries GROUP BY category ORDER BY count DESC"
        )
    ]
    return {"entries": total, "chars": chars, "categories": cats}


def entries_in(conn: sqlite3.Connection, category: Optional[str], page: int = 1, size: int = 200) -> dict:
    offset = max(0, (page - 1) * size)
    if category:
        rows = conn.execute(
            "SELECT id,title,category,chars FROM entries WHERE category=? "
            "ORDER BY title LIMIT ? OFFSET ?",
            (category, size, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM entries WHERE category=?", (category,)).fetchone()[0]
    else:
        rows = conn.execute(
            "SELECT id,title,category,chars FROM entries ORDER BY chars DESC LIMIT ? OFFSET ?",
            (size, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    return {
        "total": total,
        "page": page,
        "size": size,
        "items": [dict(row) for row in rows],
    }


def _clean_sections(sections) -> dict:
    """读取时兜底去重：站点模板会把同一段渲染两遍，早期抓的数据里还留着。"""
    out: dict[str, str] = {}
    for name, text in (sections or {}).items():
        lines = [line for line in str(text).split("\n") if line.strip()]
        deduped = dedupe_block(lines) if lines else []
        out[name] = "\n".join(deduped)
    return out


def load_entry(conn: sqlite3.Connection, content_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM entries WHERE id=?", (content_id,)).fetchone()
    if not row:
        return None
    path = OUTPUT_DIR / row["category"] / f"{content_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    data["_index_title"] = row["title"]
    data["sections"] = _clean_sections(data.get("sections"))

    siblings = conn.execute(
        "SELECT id,title FROM entries WHERE category=? ORDER BY title", (row["category"],)
    ).fetchall()
    ids = [s["id"] for s in siblings]
    try:
        pos = ids.index(content_id)
    except ValueError:
        pos = -1
    if pos >= 0:
        data["_prev"] = {"id": siblings[pos - 1]["id"], "title": siblings[pos - 1]["title"]} if pos > 0 else None
        data["_next"] = {"id": siblings[pos + 1]["id"], "title": siblings[pos + 1]["title"]} if pos + 1 < len(ids) else None
    data["_links"] = links_for(conn, content_id)
    data["_note"] = notes_for(conn, [content_id]).get(content_id)
    return data


def links_for(conn: sqlite3.Connection, content_id: int) -> dict:
    """关联图谱：本文提到的 / 被谁提到 / 相关阅读。"""

    def rows(sql: str) -> list[dict]:
        try:
            return [dict(r) for r in conn.execute(sql, (content_id,)).fetchall()]
        except sqlite3.OperationalError:  # 还没建图谱
            return []

    mentions = rows(
        "SELECT e.id, e.title, e.category, m.n FROM mentions m JOIN entries e ON e.id=m.dst "
        "WHERE m.src=? ORDER BY m.n DESC, e.chars DESC LIMIT 24"
    )
    mentioned_by = rows(
        "SELECT e.id, e.title, e.category, m.n FROM mentions m JOIN entries e ON e.id=m.src "
        "WHERE m.dst=? ORDER BY m.n DESC, e.chars DESC LIMIT 16"
    )
    related = rows(
        "SELECT e.id, e.title, e.category, r.score, r.reason FROM related r JOIN entries e ON e.id=r.other "
        "WHERE r.id=? ORDER BY r.score DESC LIMIT 12"
    )
    return {"mentions": mentions, "mentioned_by": mentioned_by, "related": related}


FEATURED_CATEGORIES = ("角色", "武器", "圣遗物", "敌人", "组织", "书籍", "观测考据", "角色逸闻")
FEED_CATEGORIES = ("角色", "武器", "圣遗物", "敌人", "任务", "书籍", "观测考据", "NPC与商店")


def explore_notes(conn: sqlite3.Connection, limit: int = 24) -> list[dict]:
    """探索式笔记：模型自己翻资料库写出来的内容，可能横跨多个词条。"""
    try:
        rows = conn.execute(
            "SELECT x.id, x.primary_id, x.title, x.hook, x.highlights, x.tags, x.sources, "
            "x.agent_seed, e.title AS primary_title, e.category "
            "FROM explore_notes x LEFT JOIN entries e ON e.id = x.primary_id "
            "ORDER BY x.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for row in rows:
        cover = ""
        cover_path = OUTPUT_DIR.parent.parent / "web" / "static" / "covers" / f"explore-{row['id']}.png"
        if cover_path.exists():
            cover = f"/static/covers/explore-{row['id']}.png"
        out.append({
            "id": row["id"],
            "primary_id": row["primary_id"],
            "title": row["title"],
            "hook": row["hook"],
            "highlights": json.loads(row["highlights"] or "[]"),
            "tags": json.loads(row["tags"] or "[]"),
            "sources": json.loads(row["sources"] or "[]"),
            "seed": row["agent_seed"],
            "primary_title": row["primary_title"] or "",
            "category": row["category"] or "探索",
            "cover": cover,
        })
    return out


def notes_for(conn: sqlite3.Connection, ids: list[int]) -> dict[int, dict]:
    """批量取梗概卡；notes 表不存在时返回空。"""
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            f"SELECT id, title, hook, highlights, tags FROM notes WHERE id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out = {}
    for row in rows:
        out[row["id"]] = {
            "title": row["title"],
            "hook": row["hook"],
            "highlights": json.loads(row["highlights"] or "[]"),
            "tags": json.loads(row["tags"] or "[]"),
        }
    return out


def _card_from_row(row) -> dict:
    """把一条记录整理成信息流卡片：封面、标题、摘要、热度。"""
    path = OUTPUT_DIR / row["category"] / f"{row['id']}.json"
    cover = ""
    excerpt = ""
    author = ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        data = {}
    images = [
        ((img or {}).get("src") or "")
        for img in (data.get("images") or [])
        if not any(bad in ((img or {}).get("alt") or "") for bad in ("暂无", "占位", "placeholder"))
    ]
    images = [src for src in images if src.startswith("http") and not src.endswith(".gif")]
    meta = data.get("api_meta") or {}
    # wiki 的 ys-obc 图多是模板占位图/水印；用户上传图（wiki-user-upload）才是真立绘。
    # 道具类（背包/食物/武器…）的图标本身就是好封面，所以按分类放宽。
    item_categories = {"背包", "食物", "洞天", "名片", "武器", "圣遗物", "敌人", "动物", "装扮"}
    cover = next((src for src in images if "wiki-user-upload" in src), "")
    if not cover:
        icon = (meta.get("icon") or "").strip()
        cover = icon if "wiki-user-upload" in icon else ""
    if not cover and row["category"] in item_categories:
        cover = next((src for src in images if "ys-obc" in src), "")
    if not cover:
        cover = next((src for src in images if "ys-obc" not in src), "")
    for heading, content in (data.get("sections") or {}).items():
        if heading in ("_intro",) or heading.startswith("页签"):
            continue
        for line in str(content).split("\n"):
            text = line.strip()
            if len(text) < 24 or text == heading:
                continue
            if text.count("*") >= 3 or text.count("；") >= 3:  # 数值/材料清单，不适合当摘要
                continue
            excerpt = text[:90] + ("…" if len(text) > 90 else "")
            break
        if excerpt:
            break
    author = (meta.get("author") or "").strip()
    return {
        "id": row["id"],
        "title": row["title"],
        "category": row["category"],
        "chars": row["chars"],
        "cover": cover,
        "excerpt": excerpt,
        "author": author,
        "heat": row["heat"],
    }


def feed(conn: sqlite3.Connection, category: Optional[str] = None,
         offset: int = 0, size: int = 24, mode: str = "hot") -> dict:
    """信息流：瀑布流卡片。mode=hot 时从高分池里随机抽，刷新有新内容。"""
    size = max(6, min(48, size))
    base = (
        "SELECT e.id, e.title, e.category, e.chars, "
        "COALESCE(n.indegree,0) AS degree, "
        "COALESCE(n.indegree,0) + e.chars / 400.0 AS heat "
        "FROM entries e LEFT JOIN nodes n ON n.id = e.id "
    )
    try:
        if category:
            rows = conn.execute(
                base + "WHERE e.category = ? ORDER BY heat DESC, e.chars DESC LIMIT ? OFFSET ?",
                (category, size, offset),
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM entries WHERE category=?", (category,)).fetchone()[0]
        elif mode == "new":
            rows = conn.execute(
                base + "WHERE e.chars >= 300 ORDER BY e.id DESC LIMIT ? OFFSET ?", (size, offset)
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM entries WHERE chars>=300").fetchone()[0]
        else:
            pool = conn.execute(
                base + "WHERE e.chars >= 300 ORDER BY heat DESC LIMIT 700"
            ).fetchall()
            if not pool:
                pool = conn.execute(base + "ORDER BY e.chars DESC LIMIT 200").fetchall()
            picked = random.sample(pool, min(size, len(pool)))
            picked.sort(key=lambda r: -r["heat"])
            rows = picked
            total = len(pool)
    except sqlite3.OperationalError:
        rows = conn.execute(
            "SELECT id, title, category, chars, 0 AS degree, chars / 400.0 AS heat FROM entries "
            "ORDER BY chars DESC LIMIT ? OFFSET ?", (size, offset)
        ).fetchall()
        total = size

    cards = [_card_from_row(row) for row in rows]
    note_map = notes_for(conn, [card["id"] for card in cards])
    for card in cards:
        card["note"] = note_map.get(card["id"])

    return {
        "items": cards,
        "size": size,
        "offset": offset,
        "total": total,
        "categories": list(FEED_CATEGORIES),
        "noted": sum(1 for card in cards if card["note"]),
    }


def featured(conn: sqlite3.Connection, limit: int = 12) -> list[dict]:
    """首页推荐：按分类取「被引用最多」的词条，避免被任务/成就这类检索枢纽霸榜。"""
    limit = max(1, min(36, limit))
    per_cat = max(1, limit // len(FEATURED_CATEGORIES) + 1)
    items: list[dict] = []
    try:
        for category in FEATURED_CATEGORIES:
            rows = conn.execute(
                "SELECT e.id, e.title, e.category, e.chars, COALESCE(n.indegree, 0) AS indegree "
                "FROM entries e LEFT JOIN nodes n ON n.id = e.id "
                "WHERE e.category = ? AND e.chars >= 400 "
                # 被引多=枢纽条目，字数多=深度条目，两者加权，避免全是低星材料/短条目
                "ORDER BY COALESCE(n.indegree, 0) + e.chars / 400.0 DESC LIMIT ?",
                (category, per_cat),
            ).fetchall()
            items.extend(dict(r) for r in rows)
    except sqlite3.OperationalError:  # 图谱未建时退化为按字数
        rows = conn.execute(
            "SELECT id, title, category, chars, 0 AS indegree FROM entries ORDER BY chars DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # 轮转交错：角色/武器/圣遗物… 各取一条，视觉上更像推荐位
    items.sort(key=lambda it: -it["indegree"])
    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(item["category"], []).append(item)
    mixed: list[dict] = []
    while len(mixed) < limit and any(grouped.values()):
        for category in FEATURED_CATEGORIES:
            bucket = grouped.get(category)
            if bucket:
                mixed.append(bucket.pop(0))
                if len(mixed) >= limit:
                    break
    return mixed[:limit]


def random_entry(conn: sqlite3.Connection, category: Optional[str] = None) -> Optional[int]:
    sql = "SELECT id FROM entries"
    params: tuple = ()
    if category:
        sql += " WHERE category=?"
        params = (category,)
    sql += " ORDER BY RANDOM() LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return row["id"] if row else None
