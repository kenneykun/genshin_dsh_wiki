"""原神观测枢本地资料库 · MCP 服务器

把 data/output 下的 13,936 条词条（1884 万字）暴露成 MCP 工具，
其他 AI 项目（Claude Desktop / Cursor / DSH / 自研 agent）接上即可检索与阅读。

    .venv\\Scripts\\python.exe -m mcp_server.server          # stdio（推荐给桌面客户端）
    .venv\\Scripts\\python.exe -m mcp_server.server --http   # streamable HTTP（默认 :8322）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

from loguru import logger
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from src.dedupe_sections import dedupe_block
from src.storage import INDEX_FILE, PROGRESS_FILE
from web import db as wiki_db

print = logger.info  # 禁止往 stdout 打印（MCP 协议占用 stdout）

PROJECT_ROOT = Path(__file__).resolve().parent.parent
READ_ONLY = ToolAnnotations(readOnlyHint=True)

server = MCPServer(
    name="genshin-wiki",
    title="原神观测枢本地资料库",
    version="1.0.0",
    instructions=(
        "本地镜像的原神观测枢（baike.mihoyo.com）全站文本：角色、武器、圣遗物、敌人、书籍、任务、"
        "地图文本、NPC、成就、考据等 30 个分类，共 13,936 条、约 1884 万字。\n\n"
        "使用建议：\n"
        "1. 不确定词条名字时先用 wiki_search 全文检索（中文按字索引，2 个字也能命中）；\n"
        "2. 已知条目 id 时用 wiki_get_entry 读结构化内容（章节/表格/图片），长条目会自动截断，"
        "再用 wiki_get_text 分段读全文；\n"
        "3. 需要了解覆盖范围时用 wiki_dataset_info。\n"
        "所有数据只读，内容中文。"
    ),
)


# ---------------------------------------------------------------- 数据访问
_conn: Optional[sqlite3.Connection] = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        if not wiki_db.DB_FILE.exists():
            raise RuntimeError(
                f"检索索引不存在：{wiki_db.DB_FILE}。"
                "请先在本项目运行 `python -m web.build_db`（约 13 秒）。"
            )
        _conn = wiki_db.connect()
    return _conn


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if limit and len(text) > limit:
        return text[:limit], True
    return text, False


def _table_md(rows: list[list[str]], limit_rows: int = 60) -> str:
    if not rows:
        return ""
    rows = rows[:limit_rows]
    head, *body = rows
    width = max(len(r) for r in rows)
    head = list(head) + [""] * (width - len(head))
    lines = ["| " + " | ".join(str(c or "") for c in head) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    for row in body:
        cells = list(row) + [""] * (width - len(row))
        lines.append("| " + " | ".join(str(c or "").replace("|", "\\|") for c in cells) + " |")
    return "\n".join(lines)


def _entry_markdown(data: dict, max_chars: int = 12000) -> tuple[str, bool]:
    lines: list[str] = []
    title = data.get("title") or data.get("_index_title") or ""
    lines.append(f"# {title}")
    meta = [f"id={data.get('content_id')}", f"分类={data.get('category')}",
            f"字数={len(data.get('raw_text') or '')}"]
    for key, label in (("rarity", "稀有度"), ("element", "元素"), ("weapon_type", "武器类型"),
                       ("constellation", "命之座"), ("birthday", "生日"), ("affiliation", "所属")):
        if data.get(key):
            meta.append(f"{label}={data[key]}")
    lines.append("> " + " · ".join(meta))

    extra = []
    for key, label in (("set_2pc", "2 件套"), ("set_4pc", "4 件套"), ("effect_name", "特效"),
                       ("effect_desc", "特效说明")):
        if data.get(key):
            extra.append(f"- **{label}**：{data[key]}")
    if extra:
        lines.append("\n".join(extra))

    for heading, content in (data.get("sections") or {}).items():
        if not content or not content.strip():
            continue
        # 站点模板的整段重复在入库时已清理，这里再兜一层
        body_lines = dedupe_block([line for line in str(content).split("\n") if line.strip()])
        lines.append(f"\n## {heading}")
        lines.extend(body_lines)

    if data.get("pieces"):
        lines.append("\n## 圣遗物部件")
        for piece in data["pieces"]:
            lines.append(f"- **{piece.get('name', '')}**：{piece.get('description', '')}")

    if data.get("constellations"):
        lines.append("\n## 命之座")
        for i, c in enumerate(data["constellations"], 1):
            lines.append(f"{i}. **{c.get('name', '')}**：{c.get('description', '')}")

    if data.get("cv_voice"):
        lines.append("\n## 语音台词")
        for label, text in data["cv_voice"].items():
            lines.append(f"- `{label}`：{text}")

    tables = data.get("tables") or []
    if tables:
        lines.append(f"\n## 数据表（{len(tables)} 张）")
        for table in tables[:6]:
            md = _table_md(table)
            if md:
                lines.append(md)
        if len(tables) > 6:
            lines.append(f"（其余 {len(tables) - 6} 张表省略）")

    images = data.get("images") or []
    if images:
        lines.append(f"\n## 图集（{len(images)}）")
        lines.append("\n".join(f"- {img.get('src', '')}" for img in images[:8]))
        if len(images) > 8:
            lines.append(f"（其余 {len(images) - 8} 张省略）")

    text = "\n".join(lines)
    clipped = _truncate(text, max_chars)
    if clipped[1]:
        return clipped[0] + f"\n\n…（内容较长，已截断到 {max_chars} 字；用 wiki_get_text 分段读取全文，或用 wiki_get_section 取单节）", True
    return text, False


def _resolve_id(value: Any) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text.isdigit():
        raise ValueError(f"content_id 必须是数字，收到 {value!r}")
    return int(text)


# ---------------------------------------------------------------- 工具
@server.tool(
    title="全文检索",
    description=(
        "在全部词条里做中文全文检索（标题 + 正文，按字索引，任意长度关键词都能命中）。"
        "返回：id、标题、分类、字数、命中位置与上下文片段。用返回的 id 调 wiki_get_entry 读全文。"
    ),
    annotations=READ_ONLY,
)
def wiki_search(query: str, category: str | None = None, limit: int = 10) -> str:
    query = (query or "").strip()
    if not query:
        return "请提供检索关键词，例如：蒙德、钟离、深渊、元素反应。"
    limit = max(1, min(50, int(limit)))
    hits = wiki_db.search(conn(), query, category=category, limit=limit)
    if not hits:
        scope = f"（分类：{category}）" if category else ""
        return f"没有找到与「{query}」相关的内容{scope}。可以换更短的关键词，或用 wiki_list_categories 看看有哪些分类。"
    lines = [f"共 {len(hits)} 条结果（query={query!r}）："]
    for i, hit in enumerate(hits, 1):
        where = "标题" if hit["where"] == "title" else "正文"
        lines.append(f"{i}. **{hit['title']}**  `id={hit['id']}` · {hit['category']} · {hit['chars']} 字 · {where}命中")
        if hit.get("snippet"):
            lines.append(f"   > {hit['snippet']}")
    lines.append("\n读取详情：wiki_get_entry(id=数字)")
    return "\n".join(lines)


@server.tool(
    title="读取词条",
    description=(
        "按 id 读取一个词条的结构化正文（Markdown：章节、表格、图片、结构化字段）。"
        "长词条会被截断到 max_chars，再配合 wiki_get_text / wiki_get_section 读剩余部分。"
    ),
    annotations=READ_ONLY,
)
def wiki_get_entry(id: int, max_chars: int = 12000) -> str:
    try:
        content_id = _resolve_id(id)
    except ValueError as exc:
        return str(exc)
    data = wiki_db.load_entry(conn(), content_id)
    if data is None:
        return f"没有找到 id={content_id} 的条目。可用 wiki_search 检索标题，或用 wiki_browse 浏览分类。"
    text, _ = _entry_markdown(data, max(1000, int(max_chars)))
    nav = []
    if data.get("_prev"):
        nav.append(f"上一篇：{data['_prev']['title']} (id={data['_prev']['id']})")
    if data.get("_next"):
        nav.append(f"下一篇：{data['_next']['title']} (id={data['_next']['id']})")
    if data.get("url"):
        nav.append(f"原站：{data['url']}")
    if nav:
        text += "\n\n---\n" + " · ".join(nav)
    return text


@server.tool(
    title="分段读取正文",
    description=(
        "按偏移读取词条的纯文本正文，用于长条目（任务、书籍、考据）分段阅读。"
        "返回 next_offset 表示还有后续内容；为 null 表示已读完。"
    ),
    annotations=READ_ONLY,
)
def wiki_get_text(id: int, offset: int = 0, max_chars: int = 8000) -> str:
    try:
        content_id = _resolve_id(id)
    except ValueError as exc:
        return str(exc)
    data = wiki_db.load_entry(conn(), content_id)
    if data is None:
        return f"没有找到 id={content_id} 的条目。"
    text = data.get("raw_text") or ""
    offset = max(0, int(offset))
    max_chars = max(500, min(30000, int(max_chars)))
    chunk = text[offset:offset + max_chars]
    next_offset = offset + len(chunk) if offset + len(chunk) < len(text) else None
    head = f"# {data.get('title')}（全文 {len(text)} 字，本段 {offset}-{offset + len(chunk)}）\n\n"
    tail = f"\n\n---\nnext_offset={next_offset}" if next_offset is not None else "\n\n---\n（全文读完）"
    return head + chunk + tail


@server.tool(
    title="读取单个章节",
    description="读取词条中某个章节的完整内容。先用 wiki_get_entry 看到章节名，再精确取某节。",
    annotations=READ_ONLY,
)
def wiki_get_section(id: int, heading: str) -> str:
    try:
        content_id = _resolve_id(id)
    except ValueError as exc:
        return str(exc)
    data = wiki_db.load_entry(conn(), content_id)
    if data is None:
        return f"没有找到 id={content_id} 的条目。"
    sections = data.get("sections") or {}
    for name, content in sections.items():
        if heading in name:
            return f"# {data.get('title')} · {name}\n\n{content}"
    available = "、".join(list(sections.keys())[:20]) or "（无章节）"
    return f"没有找到标题包含「{heading}」的章节。可用章节：{available}"


@server.tool(
    title="关联条目",
    description=(
        "返回词条的关联网络：本文提到的条目、引用了本文的条目、以及算法推荐的延伸阅读。"
        "用于顺着人物/地点/组织的关系链做考据，或读完一条后继续读相关条目。"
    ),
    annotations=READ_ONLY,
)
def wiki_related(id: int, limit: int = 8) -> str:
    try:
        content_id = _resolve_id(id)
    except ValueError as exc:
        return str(exc)
    c = conn()
    data = wiki_db.load_entry(c, content_id)
    if data is None:
        return f"没有找到 id={content_id} 的条目。"
    links = data.get("_links") or {}
    limit = max(1, min(30, int(limit)))
    reason_label = {"co-mention": "共同提及", "co-cited": "被同时引用", "same-category": "同类"}

    lines = [f"# {data.get('title')} · 关联网络"]
    mentions = links.get("mentions") or []
    if mentions:
        lines.append(f"\n## 本文提及（{len(mentions)}）")
        lines.extend(
            f"- {m['title']}（{m['category']}，提到 {m['n']} 次）`id={m['id']}`"
            for m in mentions[:limit]
        )
    mentioned_by = links.get("mentioned_by") or []
    if mentioned_by:
        lines.append(f"\n## 被这些条目提及（{len(mentioned_by)}）")
        lines.extend(f"- {m['title']}（{m['category']}）`id={m['id']}`" for m in mentioned_by[:limit])
    related = links.get("related") or []
    if related:
        lines.append("\n## 延伸阅读")
        lines.extend(
            f"- {m['title']}（{m['category']}，{reason_label.get(m['reason'], m['reason'])}）`id={m['id']}`"
            for m in related[:limit]
        )
    if len(lines) == 1:
        return f"{data.get('title')} 还没有关联数据；可先运行 `python -m src.build_graph` 生成图谱。"
    lines.append("\n读取：wiki_get_entry(id=数字)")
    return "\n".join(lines)


@server.tool(
    title="分类列表",
    description="列出全部 30 个分类及各自条目数、字数，用于确定检索范围。",
    annotations=READ_ONLY,
)
def wiki_list_categories() -> str:
    stats = wiki_db.stats(conn())
    lines = [f"共 {stats['entries']} 条 / {stats['chars']} 字 / {len(stats['categories'])} 个分类："]
    for cat in stats["categories"]:
        lines.append(f"- **{cat['name']}**：{cat['count']} 条，{cat['chars']} 字")
    lines.append("\n按分类浏览：wiki_browse(category=\"敌人\")")
    return "\n".join(lines)


@server.tool(
    title="浏览分类条目",
    description="按分类列出条目（可按标题过滤、按标题或字数排序）。返回 id 供 wiki_get_entry 使用。",
    annotations=READ_ONLY,
)
def wiki_browse(
    category: str | None = None,
    query: str | None = None,
    page: int = 1,
    page_size: int = 30,
    sort: str = "title",
) -> str:
    page = max(1, int(page))
    page_size = max(1, min(200, int(page_size)))
    c = conn()
    items = wiki_db.entries_in(c, category, page, page_size)["items"]
    if query:
        needle = query.strip()
        items = [it for it in items if needle in it["title"]]
    if sort == "chars":
        items.sort(key=lambda it: -it["chars"])
    if not items:
        return f"分类「{category or '全部'}」第 {page} 页没有条目。"
    lines = [f"{category or '全部条目'} · 第 {page} 页 · {len(items)} 条："]
    for it in items:
        lines.append(f"- `id={it['id']}` **{it['title']}**（{it['chars']} 字）")
    lines.append("\n读取详情：wiki_get_entry(id=数字)")
    return "\n".join(lines)


@server.tool(
    title="随机词条",
    description="随机取一条（可限定分类）。适合做发散性考据或抽查数据。",
    annotations=READ_ONLY,
)
def wiki_random(category: str | None = None) -> str:
    c = conn()
    cid = wiki_db.random_entry(c, category)
    if cid is None:
        return "没有可用条目。"
    data = wiki_db.load_entry(c, cid)
    if data is None:
        return f"随机到 id={cid}，但文件缺失。"
    return _entry_markdown(data, 4000)[0]


@server.tool(
    title="资料库概况",
    description="返回数据规模、分类分布、采集进度与索引新鲜度，用于判断数据是否够用、是否需要重建索引。",
    annotations=READ_ONLY,
)
def wiki_dataset_info() -> str:
    try:
        stats = wiki_db.stats(conn())
        db_mtime = wiki_db.DB_FILE.stat().st_mtime
    except RuntimeError as exc:
        return str(exc)
    lines = [
        "# 资料库概况",
        f"- 条目：{stats['entries']}",
        f"- 正文字数：{stats['chars']}",
        f"- 分类：{len(stats['categories'])}",
        f"- 索引文件：{wiki_db.DB_FILE.name}，更新于 {time.strftime('%Y-%m-%d %H:%M', time.localtime(db_mtime))}",
    ]
    if PROGRESS_FILE.exists():
        progress = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        completed = len(progress.get("completed", {}))
        failed = len(progress.get("failed", {}))
        lines.append(f"- 采集进度：已完成 {completed} 条，失败 {failed} 条（{progress.get('last_update', '')}）")
    if INDEX_FILE.exists():
        index = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        lines.append(f"- 全站索引：{len(index)} 条")
        missing = [e for e in index if not (PROJECT_ROOT / "data" / "output" / (e.get("category") or "misc") / f"{e['content_id']}.json").exists()]
        if missing:
            lines.append(f"- 尚未采集：{len(missing)} 条（多为已下架词条）")
    if INDEX_FILE.exists() and INDEX_FILE.stat().st_mtime > db_mtime:
        lines.append("- ⚠️ index.json 比检索索引新，建议运行 `python -m web.build_db` 重建索引")
    lines.append(f"- 数据目录：{PROJECT_ROOT / 'data' / 'output'}")
    return "\n".join(lines)


@server.tool(
    title="重建检索索引",
    description="数据更新后重建 SQLite 全文索引（约 13 秒）。重建后本进程重新连接。",
)
def wiki_rebuild_index() -> str:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
    info = wiki_db.build_index()
    return (f"索引已重建：{info['entries']} 条，耗时 {info['seconds']} 秒，"
            f"体积 {info['db_mb']} MB → {wiki_db.DB_FILE}")


# ---------------------------------------------------------------- 资源
@server.resource("wiki://index", name="分类索引", mime_type="text/markdown",
                 description="全部分类及条目数，可作为检索入口")
def resource_index() -> str:
    return wiki_list_categories()


@server.resource("wiki://entry/{content_id}", name="词条", mime_type="text/markdown",
                 description="按 id 读取词条正文（Markdown）")
def resource_entry(content_id: str) -> str:
    try:
        cid = int(content_id)
    except ValueError:
        return f"content_id 必须是数字，收到 {content_id!r}"
    data = wiki_db.load_entry(conn(), cid)
    if data is None:
        return f"没有找到 id={cid} 的条目。"
    return _entry_markdown(data, 20000)[0]


# ---------------------------------------------------------------- 提示词
@server.prompt(title="词条考据分析", description="把某条词条整理成考据分析素材")
def analyze_entry(id: str) -> str:
    data = wiki_db.load_entry(conn(), _resolve_id(id))
    if data is None:
        return f"没有找到 id={id} 的条目。"
    text = _entry_markdown(data, 8000)[0]
    return (
        f"以下是从原神观测枢本地资料库取出的词条，请基于**原文**做考据分析：\n\n"
        f"1. 先概括该词条涉及的设定与关系网；\n"
        f"2. 标注原文里可支撑结论的具体句子；\n"
        f"3. 指出数据中缺失或存疑的部分（不要编造）。\n\n"
        f"---\n\n{text}"
    )


# ---------------------------------------------------------------- 入口
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="原神观测枢资料库 MCP 服务器")
    parser.add_argument("--http", action="store_true", help="用 streamable HTTP 传输（默认 stdio）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8322)
    args = parser.parse_args(argv)

    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{message}</level>")

    if not wiki_db.DB_FILE.exists():
        logger.warning(f"检索索引不存在：{wiki_db.DB_FILE}（首次调用工具前先运行 python -m web.build_db）")

    logger.info(f"genshin-wiki MCP 启动（{'streamable-http' if args.http else 'stdio'}）")
    if args.http:
        server.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
