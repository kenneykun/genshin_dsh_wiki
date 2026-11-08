"""把早期 Playwright 渲染留下的旧格式条目，升级成 API 解析结果（保留结构化字段）

背景：角色/武器/圣遗物 三类是早期用 Playwright 渲染整页 DOM 再解析的，
正文里混进了站点的导航文字（"打开App/最近搜索/词条贡献者"之类），
但它们带 rarity/element/talents 等结构化字段，是 API 通用解析拿不到的。

做法：重新走 API 拿干净正文，再把旧文件里的结构化字段合并进来。

用法：
    .venv\\Scripts\\python.exe -m src.migrate_legacy --dry-run
    .venv\\Scripts\\python.exe -m src.migrate_legacy
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from loguru import logger

from src.api_client import ApiClient, ProgressTracker
from src.api_parser import parse_payload
from src.config import OUTPUT_DIR
from src.storage import Storage

# 需要从旧文件继承的结构化字段
KEEP_FIELDS = (
    "rarity", "element", "weapon_type", "constellation", "birthday",
    "affiliation", "role", "title_alias", "talents", "constellations",
    "ascension_materials", "cv_voice", "base_atk", "sub_stat",
    "sub_stat_value", "effect_name", "effect_desc", "set_2pc", "set_4pc",
    "pieces", "slot",
)


def is_legacy(data: dict) -> bool:
    return data.get("api_kind") in (None, "legacy")


def migrate_one(client: ApiClient, storage: Storage, tracker: ProgressTracker, path: Path) -> str:
    old = json.loads(path.read_text(encoding="utf-8"))
    cid = int(old.get("content_id") or path.stem)
    category = old.get("category") or path.parent.name
    url = old.get("url") or ""

    kind, payload = client.fetch(cid)
    fresh = parse_payload(kind, payload, cid, url, category)

    carried = [k for k in KEEP_FIELDS if old.get(k) not in (None, "", 0, [], {})]
    for key in carried:
        fresh[key] = old[key]
    fresh["migrated_from"] = "legacy-render"

    storage.save_detail(fresh, category)
    storage.save_plaintext(cid, fresh.get("title") or old.get("title", ""), fresh.get("raw_text", ""))
    tracker.mark_done(cid, str(path), fresh.get("title") or old.get("title", ""))
    return f"{cid} 保留字段 {len(carried)} 个"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="升级旧格式条目")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)

    targets: list[Path] = []
    for folder in sorted(p for p in OUTPUT_DIR.iterdir() if p.is_dir()):
        if folder.name == "text":
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if is_legacy(data):
                targets.append(path)

    if not targets:
        logger.success("没有需要迁移的旧条目")
        return 0

    logger.info(f"发现 {len(targets)} 条旧格式条目{'（试运行）' if args.dry_run else ''}")
    if args.dry_run:
        for p in targets[:10]:
            logger.info(f"  {p.parent.name}/{p.name}")
        return 0

    storage = Storage()
    tracker = ProgressTracker(storage, flush_every=40)
    ok = fail = 0
    with ApiClient(workers=args.workers) as client, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(migrate_one, client, storage, tracker, p): p for p in targets}
        for fut in as_completed(futures):
            path = futures[fut]
            try:
                info = fut.result()
                ok += 1
                logger.debug(f"  {path.parent.name}/{path.name}: {info}")
            except Exception as exc:  # noqa: BLE001
                fail += 1
                logger.warning(f"  {path.parent.name}/{path.name} 失败: {type(exc).__name__}: {exc}")

    tracker.flush()
    logger.success(f"迁移完成: 成功 {ok} / 失败 {fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
