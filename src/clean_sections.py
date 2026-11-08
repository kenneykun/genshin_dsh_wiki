"""清理旧渲染遗留的站点导航噪声，并把 category 字段对齐到目录名

站点页面里的导航文字（"打开App" / "米哈游官方社区" / "词条贡献者" …）
在早期 Playwright 整页渲染时混进了正文；这里按行剔除，并丢弃清空后的章节。
同时把 JSON 里的 category 写成所属目录名（index.json 的分类口径）。

用法：
    .venv\\Scripts\\python.exe -m src.clean_sections --dry-run
    .venv\\Scripts\\python.exe -m src.clean_sections
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from loguru import logger

from src.config import OUTPUT_DIR

# 站点外壳文字（整行等于或包含这些内容即丢弃）
NOISE_EXACT = {
    "html", "米哈游官方社区", "打开App", "打开APP", "观测·攻略", "观测·Wiki",
    "七圣召唤", "最近搜索", "清空", "词条贡献者：", "词条贡献者", "更新记录",
    "目录", "建议与反馈", "搜索历史", "反馈内容", "返回顶部", "©米哈游",
}
NOISE_PARTS = ("米哈游官方社区", "词条贡献者", "打开App", "打开APP", "观测·攻略")
_BARE_ID = re.compile(r"^\d{7,}$")  # 7 位以上独立数字基本是文章 id（生命值之类的数值会小得多）


def is_noise_line(line: str) -> bool:
    text = line.strip()
    if not text:
        return False  # 空行单独处理
    if text in NOISE_EXACT:
        return True
    if any(part in text for part in NOISE_PARTS) and len(text) <= 20:
        return True
    if _BARE_ID.match(text):
        return True
    return False


def clean_text(text: str) -> tuple[str, int]:
    removed = 0
    kept: list[str] = []
    for line in str(text).split("\n"):
        if line.strip() and is_noise_line(line):
            removed += 1
            continue
        kept.append(line)
    # 去掉首尾空行
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept), removed


def rebuild_raw_text(sections: dict) -> str:
    return "\n\n".join(
        f"## {name}\n{content}" if name != "_intro" else content
        for name, content in sections.items()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理正文噪声并对齐分类字段")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    files_changed = 0
    lines_removed = 0
    cat_fixed = 0
    sections_dropped = 0
    scanned = 0

    for path in OUTPUT_DIR.rglob("*.json"):
        if path.name in ("index.json", "progress.json"):
            continue
        scanned += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict) or not data.get("sections"):
            continue

        changed = False
        folder = path.parent.name

        if data.get("category") != folder:
            data["category"] = folder
            cat_fixed += 1
            changed = True

        new_sections = {}
        for name, content in data["sections"].items():
            cleaned, removed = clean_text(content)
            if removed:
                lines_removed += removed
                changed = True
            if cleaned.strip():
                new_sections[name] = cleaned
            else:
                sections_dropped += 1
                changed = True

        if changed:
            data["sections"] = new_sections
            data["raw_text"] = rebuild_raw_text(new_sections)
            files_changed += 1
            if not args.dry_run:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    prefix = "[DRY-RUN] " if args.dry_run else ""
    logger.success(
        f"{prefix}扫描 {scanned} 个文件：改动 {files_changed} 个，"
        f"删除噪声行 {lines_removed}，丢弃空章节 {sections_dropped}，修正分类字段 {cat_fixed}"
    )
    if files_changed and not args.dry_run:
        logger.info("记得重建索引：python -m web.build_db")
    return 0


if __name__ == "__main__":
    sys.exit(main())
