"""双通道 JSON → 与旧版一致的结构化数据

两种模型都先归一成 HTML，再复用 src.parser 的既有解析器，
这样输出 schema 与已爬取的 1574 条完全一致，viewer 无需改动。
"""
from __future__ import annotations

import html
import json
import re
from typing import Iterable

from bs4 import BeautifulSoup, NavigableString, Tag

from src.parser import NOISE_SECTIONS, NoiseFilter, detail_to_dict, parse_detail

# 纯机器键名的值（图标地址、内部 id 之类）不进正文
_META_KEYS = {
    "icon_url", "icon", "image", "img", "url", "link", "background", "bg",
    "color", "id", "pos", "layout", "layout_", "style", "position",
    "icon_position", "is_poped", "is_customize_name", "is_abstract",
    "is_show_switch", "switch", "component_id", "parent_group_id",
    "module_group_id", "tab_id", "tmplKey", "partKey", "moduleName",
    "without_border", "cover", "common_img", "gold_img", "header_img_url",
}
_MACHINE_TOKEN = re.compile(r"^[A-Za-z0-9_\-./:%]+$")


def _emit_value(value, out: list[str]) -> None:
    """把组件 data 里的任意嵌套结构摊平成可读 HTML 片段。"""
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        # 纯数字多为布局/编号，不进正文
        return
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return
        if "<" in text and ">" in text:
            out.append(text)  # 已经是 HTML 片段
        elif not _MACHINE_TOKEN.match(text):
            out.append(f"<p>{html.escape(text)}</p>")
        return
    if isinstance(value, list):
        for item in value:
            _emit_value(item, out)
        return
    if isinstance(value, dict):
        if "value" in value:
            key = value.get("key")
            if isinstance(key, str) and key.strip():
                out.append(f"<p><b>{html.escape(key.strip())}</b></p>")
            _emit_value(value.get("value"), out)
            for k, v in value.items():
                if k in ("key", "value") or k in _META_KEYS:
                    continue
                _emit_value(v, out)
            return
        for k, v in value.items():
            if k in _META_KEYS:
                continue
            _emit_value(v, out)


def _component_html(component: dict) -> str:
    data = component.get("data")
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return ""
        if text.startswith("{") or text.startswith("["):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return f'<div class="obc-tmpl-part">{text}</div>'
        else:
            return f'<div class="obc-tmpl-part">{text}</div>'
    out: list[str] = []
    _emit_value(data, out)
    return "\n".join(out)


def old_payload_to_html(payload: dict) -> tuple[str, dict]:
    """旧模型：data.content.contents[].text 本身就是站点 HTML。"""
    content = payload.get("content") or {}
    title = (content.get("title") or "").strip()
    blocks: list[str] = []
    for block in content.get("contents") or []:
        name = (block.get("name") or "").strip()
        text = block.get("text") or ""
        if name:
            blocks.append(f"<h2>{html.escape(name)}</h2>")
        blocks.append(text)
    meta = {
        "kind": "old",
        "title": title,
        "version": content.get("version"),
        "author": content.get("author_name"),
        "editor": content.get("editor_name"),
        "mtime": content.get("mtime"),
        "summary": content.get("summary"),
        "icon": content.get("icon"),
    }
    return f"<h1>{html.escape(title)}</h1>\n" + "\n".join(blocks), meta


def new_payload_to_html(payload: dict) -> tuple[str, dict]:
    """新模型：data.page.modules[].components[].data 是 JSON 字符串。"""
    page = payload.get("page") or {}
    title = (page.get("name") or "").strip()
    blocks: list[str] = []
    for module in page.get("modules") or []:
        name = (module.get("name") or "").strip()
        if name:
            blocks.append(f"<h2>{html.escape(name)}</h2>")
        for component in module.get("components") or []:
            chunk = _component_html(component)
            if chunk:
                blocks.append(chunk)
    meta = {
        "kind": "new",
        "title": title,
        "version": page.get("version"),
        "menu": page.get("menu_name"),
        "desc": page.get("desc"),
        "icon": page.get("icon_url"),
        "header_img": page.get("header_img_url"),
    }
    return f"<h1>{html.escape(title)}</h1>\n" + "\n".join(blocks), meta


def payload_to_html(kind: str, payload: dict) -> tuple[str, dict]:
    if kind == "new":
        return new_payload_to_html(payload)
    return old_payload_to_html(payload)


_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_BLOCK_TAGS = {
    "p", "div", "li", "ul", "ol", "tr", "table", "td", "th", "dd", "dt",
    "blockquote", "pre", "section", "article", "figure", "figcaption", "br",
}


def _sections_from_soup(soup: BeautifulSoup) -> dict:
    """按标题切分章节，段落内联文本合并成一行。

    与 src.parser.DetailParser._extract_sections 等价，但：
    1. 用 isinstance 判定节点类型，避免 bs4 版本差异；
    2. 段落内的多个内联节点合并，避免正文被切成碎片；
    3. 同名标题加序号，避免后出现的同名标题覆盖前面的正文。
    """
    sections: dict[str, str] = {}
    heading = "_intro"
    lines: list[str] = []
    pending: list[str] = []

    def flush_pending():
        if not pending:
            return
        text = NoiseFilter.clean_line(" ".join(pending))
        pending.clear()
        if text and not NoiseFilter.is_noise(text):
            lines.append(text)

    def flush_line():
        flush_pending()
        if not lines:
            return
        # 站点模板会把同一段信息渲染两遍（PC/移动端），整段重复时折掉
        n = len(lines)
        for parts in (2, 3):
            if n % parts:
                continue
            size = n // parts
            if all(lines[i] == lines[size + i] for i in range(size)):
                del lines[size:]
                break
        key = heading
        dup = 2
        while key in sections:  # 同名标题（如地图卡片重复物品名）不能互相覆盖
            key = f"{heading} ({dup})"
            dup += 1
        sections[key] = "\n".join(lines)
        lines.clear()

    for node in soup.descendants:
        if isinstance(node, Tag):
            if node.name in _HEADING_TAGS:
                flush_line()
                heading = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
            elif node.name in _BLOCK_TAGS:
                flush_pending()
        elif isinstance(node, NavigableString) and node.strip():
            text = NoiseFilter.clean_line(str(node))
            if text:
                pending.append(text)
    flush_line()

    return {
        k: v for k, v in sections.items()
        if k not in NOISE_SECTIONS and v.strip()
    }


def parse_payload(
    kind: str,
    payload: dict,
    content_id: int,
    url: str,
    category: str = "",
) -> dict:
    """返回与 src.parser.detail_to_dict 相同 schema 的 dict。"""
    from src.parser import DetailParser

    doc_html, meta = payload_to_html(kind, payload)
    soup = BeautifulSoup(doc_html, "lxml")
    for tag in soup(["script", "style", "nav", "noscript", "meta", "link"]):
        tag.decompose()

    sections = _sections_from_soup(soup)
    raw_text = "\n\n".join(
        f"## {h}\n{c}" if h != "_intro" else c
        for h, c in sections.items()
    )
    return {
        "content_id": content_id,
        "title": (meta.get("title") or "").strip() or str(content_id),
        "url": url,
        "category": category,
        "sections": sections,
        "raw_text": raw_text,
        "images": DetailParser._extract_images(soup),
        "tables": DetailParser._extract_tables(soup),
        "api_kind": kind,
        "api_meta": meta,
    }


def iter_texts(obj) -> Iterable[str]:
    """调试用：遍历结构里所有字符串。"""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_texts(item)
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from iter_texts(value)
