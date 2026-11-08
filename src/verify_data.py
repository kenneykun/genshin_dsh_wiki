"""数据完整性/质量校验

用法：
    .venv\\Scripts\\python.exe -m src.verify_data            # 覆盖率 + 各分类正文字数
    .venv\\Scripts\\python.exe -m src.verify_data --thin 80  # 另外列出正文过短的条目
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from src.config import OUTPUT_DIR
from src.storage import INDEX_FILE, PROGRESS_FILE


def main() -> int:
    parser = argparse.ArgumentParser(description="校验采集结果")
    parser.add_argument("--thin", type=int, default=0, help="列出正文少于 N 字的条目")
    args = parser.parse_args()

    entries = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    progress = json.loads(PROGRESS_FILE.read_text(encoding="utf-8")) if PROGRESS_FILE.exists() else {}
    failed = progress.get("failed", {})

    by_cat: dict[str, list[dict]] = {}
    for e in entries:
        by_cat.setdefault(e.get("category") or "misc", []).append(e)

    print(f"{'分类':<10}{'索引':>6}{'有数据':>7}{'缺':>4}{'中位字数':>10}{'总字数':>12}")
    total = have = 0
    thin_rows: list[tuple[str, int, int, str]] = []
    total_chars = 0

    for cat, items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        lengths, missing = [], 0
        for e in items:
            cid = e["content_id"]
            path = OUTPUT_DIR / cat / f"{cid}.json"
            if not path.exists():
                missing += 1
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                missing += 1
                continue
            n = len(data.get("raw_text") or "")
            lengths.append(n)
            total_chars += n
            if args.thin and n < args.thin:
                thin_rows.append((cat, cid, n, data.get("title", "")))
        total += len(items)
        have += len(lengths)
        median = int(statistics.median(lengths)) if lengths else 0
        print(f"{cat:<10}{len(items):>6}{len(lengths):>7}{missing:>4}{median:>10}{sum(lengths):>12,}")

    print(f"\n合计: 索引 {total} / 有数据 {have} / 缺 {total - have} ({have / total * 100:.2f}%)")
    print(f"正文总字数 {total_chars:,}；失败记录 {len(failed)} 条")
    if failed:
        for cid, info in list(failed.items())[:10]:
            print(f"   失败 {cid}: {info.get('error', '')[:80]}")

    if args.thin:
        print(f"\n正文 < {args.thin} 字: {len(thin_rows)} 条")
        for cat, cid, n, title in thin_rows[:30]:
            print(f"   {cat:<10}{cid:<9}{n:>5}  {title}")

    return 0 if have == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
