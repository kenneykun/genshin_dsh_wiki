"""输出目录归一：把早期英文分类目录并入中文分类，重复项移入 _legacy 存档

背景：项目早期分类名是英文（characters/weapons/...），后来统一成中文（角色/武器/...），
导致同一批条目存在两套目录；viewer 按 index.json 的 category 取目录，英文目录里的数据等于不可见。

规则：
- 以 index.json 的 category 为准，把文件搬到对应中文目录；
- 目标目录已有同名文件时，旧文件移到 data/_legacy/（不删除，可回滚）；
- index 里查不到的 content_id，一并移入 _legacy。
"""
from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from pathlib import Path

from loguru import logger

from src.config import DATA_DIR, OUTPUT_DIR
from src.storage import INDEX_FILE, PROGRESS_FILE

LEGACY_DIR = DATA_DIR / "_legacy"
KNOWN_CN = {
    "角色", "角色逸闻", "武器", "圣遗物", "组织", "空月之歌", "敌人", "地图文本",
    "成就", "食物", "头像", "背包", "活动", "任务", "动物", "书籍", "冒险家协会",
    "NPC与商店", "秘境", "洞天", "深境螺旋", "名片", "装扮", "教程",
    "幻想真境剧诗", "酒馆挑战", "原神OST", "观景点", "观测考据", "限时祈愿",
    "卡牌图鉴", "圣遗物图鉴",
}


def load_category_map() -> dict[int, str]:
    entries = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    return {int(e["content_id"]): e.get("category") or "misc" for e in entries}


def normalize(dry_run: bool = False) -> dict:
    cat_map = load_category_map()
    moved = Counter()
    legacy = Counter()
    skipped = Counter()

    for folder in sorted(p for p in OUTPUT_DIR.iterdir() if p.is_dir()):
        name = folder.name
        if name == "text" or name in KNOWN_CN:
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                cid = int(path.stem)
            except ValueError:
                skipped[name] += 1
                continue
            target_cat = cat_map.get(cid)
            if target_cat in KNOWN_CN and target_cat != name:
                target = OUTPUT_DIR / target_cat / path.name
                target.parent.mkdir(exist_ok=True)
                if target.exists():
                    dest = LEGACY_DIR / name / path.name
                    if not dry_run:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(path), str(dest))
                    legacy[name] += 1
                else:
                    if not dry_run:
                        shutil.move(str(path), str(target))
                    moved[f"{name}→{target_cat}"] += 1
            else:
                dest = LEGACY_DIR / name / path.name
                if not dry_run:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(dest))
                legacy[name] += 1

    # 清掉空目录
    if not dry_run:
        for folder in sorted(p for p in OUTPUT_DIR.iterdir() if p.is_dir()):
            if folder.name in ("text",) or folder.name in KNOWN_CN:
                continue
            if not any(folder.iterdir()):
                folder.rmdir()

    # 重写 progress 里的 path（历史上是 D:\program\... 的过期路径）
    if not dry_run and PROGRESS_FILE.exists():
        progress = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        fixed = 0
        for cid, info in progress.get("completed", {}).items():
            cat = cat_map.get(int(cid))
            if not cat:
                continue
            want = OUTPUT_DIR / cat / f"{cid}.json"
            if want.exists() and info.get("path") != str(want):
                info["path"] = str(want)
                fixed += 1
        progress["last_update"] = progress.get("last_update", "")
        PROGRESS_FILE.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"progress.json 路径已修正 {fixed} 条")

    return {"moved": moved, "legacy": legacy, "skipped": skipped}


def main() -> int:
    parser = argparse.ArgumentParser(description="输出目录归一（英文目录 → 中文分类）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不落盘")
    args = parser.parse_args()

    result = normalize(dry_run=args.dry_run)
    prefix = "[DRY-RUN] " if args.dry_run else ""
    logger.info(f"{prefix}搬移统计: {dict(result['moved'])}")
    if result["legacy"]:
        logger.info(f"{prefix}移入 _legacy（目标已存在或 index 无记录）: {dict(result['legacy'])}")
    if result["skipped"]:
        logger.info(f"{prefix}跳过非数字文件名: {dict(result['skipped'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
