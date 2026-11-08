"""清除正文里的整段重复（站点模板会同时渲染 PC / 移动端两套结构）

规则：某章节的行序列若由自身重复 2 或 3 次组成（如 A B C | A B C），只保留一份。
幂等，可反复执行。

用法：
    .venv\\Scripts\\python.exe -m src.dedupe_sections --dry-run
    .venv\\Scripts\\python.exe -m src.dedupe_sections
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from loguru import logger

from src.config import OUTPUT_DIR


def dedupe_block(lines: list[str], max_prefix: int = 3) -> list[str]:
    """整段重复检测：允许开头有 0-3 行标题回显（如章节名本身）。"""
    for prefix in range(0, min(max_prefix + 1, len(lines))):
        body = lines[prefix:]
        n = len(body)
        for parts in (2, 3):
            if n < parts * 2 or n % parts:
                continue
            size = n // parts
            if all(body[i] == body[size + i] for i in range(size)):
                return dedupe_block(lines[:prefix] + body[:size], max_prefix)
    return lines


def clean_section(text: str) -> tuple[str, bool]:
    lines = [line for line in str(text).split("\n")]
    # 保留空行结构：先按非空行去重，再还原
    stripped = [line.strip() for line in lines]
    nonempty = [line for line in stripped if line]
    if len(nonempty) < 4:
        return text, False
    deduped = dedupe_block(nonempty)
    if len(deduped) == len(nonempty):
        return text, False
    return "\n".join(deduped), True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清除正文整段重复")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    scanned = changed = 0
    sections_fixed = 0
    chars_saved = 0

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

        before_total = len(data.get("raw_text") or "")
        sections = data["sections"]
        fixed_here = 0
        for name, text in list(sections.items()):
            new_text, fixed = clean_section(text)
            if fixed:
                sections[name] = new_text
                fixed_here += 1
        if not fixed_here:
            continue

        data["sections"] = sections
        data["raw_text"] = "\n\n".join(
            f"## {name}\n{text}" if name != "_intro" else text
            for name, text in sections.items()
        )
        after_total = len(data["raw_text"])
        chars_saved += max(0, before_total - after_total)
        changed += 1
        sections_fixed += fixed_here
        if not args.dry_run:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    prefix = "[DRY-RUN] " if args.dry_run else ""
    logger.success(
        f"{prefix}扫描 {scanned} 个文件，{changed} 个有重复，修正 {sections_fixed} 个章节，"
        f"减少 {chars_saved:,} 字"
    )
    if changed and not args.dry_run:
        logger.info("记得重建索引：python -m web.build_db")
    return 0


if __name__ == "__main__":
    sys.exit(main())
