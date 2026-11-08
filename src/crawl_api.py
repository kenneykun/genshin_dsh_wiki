"""观测枢全量采集（双通道 API，无浏览器）

示例：
    .venv\\Scripts\\python.exe -m src.crawl_api --limit 50          # 抽样验证
    .venv\\Scripts\\python.exe -m src.crawl_api                    # 续爬全部缺口
    .venv\\Scripts\\python.exe -m src.crawl_api -c 敌人 教程         # 只爬指定分类
    .venv\\Scripts\\python.exe -m src.crawl_api --force -c 角色      # 强制重爬
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from loguru import logger
from tqdm import tqdm

from src.api_client import ApiClient, ProgressTracker
from src.api_parser import parse_payload
from src.config import BASE_URL
from src.storage import Storage


def build_tasks(
    storage: Storage,
    tracker: ProgressTracker,
    categories: Optional[list[str]] = None,
    skip_categories: Optional[list[str]] = None,
    force: bool = False,
    retry_failed: bool = False,
    limit: Optional[int] = None,
) -> list[dict]:
    entries = storage.load_index()
    if not entries:
        raise SystemExit("index.json 为空，先跑一次索引抓取")

    if categories:
        wanted = set(categories)
        entries = [e for e in entries if e.get("category", "misc") in wanted]
        logger.info(f"按分类过滤: {', '.join(categories)} → {len(entries)} 条")

    if skip_categories:
        skip = set(skip_categories)
        entries = [e for e in entries if e.get("category", "misc") not in skip]
        logger.info(f"跳过分类: {', '.join(skip_categories)} → 剩 {len(entries)} 条")

    progress = tracker.progress
    completed = {int(k) for k in progress["completed"]}
    failed = {int(k) for k in progress["failed"]}

    if retry_failed:
        tasks = [e for e in entries if e["content_id"] in failed]
    elif force:
        tasks = list(entries)
    else:
        tasks = [e for e in entries if e["content_id"] not in completed]

    if limit:
        tasks = tasks[:limit]
    return tasks


def crawl_one(client: ApiClient, storage: Storage, tracker: ProgressTracker, entry: dict) -> tuple[bool, str]:
    cid = entry["content_id"]
    category = entry.get("category") or "其他"
    url = entry.get("url") or f"{BASE_URL}/ys/obc/content/{cid}/detail"
    try:
        kind, payload = client.fetch(cid)
        detail = parse_payload(kind, payload, cid, url, category)
        path = storage.save_detail(detail, category)
        storage.save_plaintext(cid, detail.get("title") or entry.get("title", ""), detail.get("raw_text", ""))
        tracker.mark_done(cid, str(path), detail.get("title") or entry.get("title", ""))
        return True, kind
    except Exception as exc:  # noqa: BLE001 - 单条失败不能中断批量
        tracker.mark_failed(cid, f"{type(exc).__name__}: {exc}")
        logger.debug(f"[{cid}] {entry.get('title')}: {type(exc).__name__}: {exc}")
        return False, f"{type(exc).__name__}: {exc}"


def run(args) -> int:
    storage = Storage()
    tracker = ProgressTracker(storage, flush_every=args.flush_every)
    tasks = build_tasks(
        storage,
        tracker,
        categories=args.categories,
        skip_categories=args.skip_cats,
        force=args.force,
        retry_failed=args.retry_failed,
        limit=args.limit,
    )
    if not tasks:
        logger.success("没有待采集条目，全部完成")
        return 0

    logger.info(f"待采集 {len(tasks)} 条，并发 {args.workers}")
    started = time.time()
    ok = fail = 0
    fail_examples: list[str] = []

    with ApiClient(workers=args.workers) as client, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(crawl_one, client, storage, tracker, e): e for e in tasks}
        with tqdm(total=len(tasks), unit="条", ncols=90, disable=args.quiet) as bar:
            for fut in as_completed(futures):
                entry = futures[fut]
                success, info = fut.result()
                if success:
                    ok += 1
                else:
                    fail += 1
                    if len(fail_examples) < 10:
                        fail_examples.append(f"{entry['content_id']} {entry.get('title')} → {info}")
                bar.update(1)
                if (ok + fail) % 200 == 0:
                    bar.set_postfix_str(f"ok={ok} fail={fail}")

    tracker.flush()
    elapsed = time.time() - started
    rate = (ok + fail) / elapsed if elapsed else 0
    logger.info(
        f"完成 {ok + fail} 条 / 成功 {ok} / 失败 {fail} / 用时 {elapsed:.1f}s "
        f"({rate:.1f} 条每秒)，接口统计 {client.stats}"
    )
    if fail_examples:
        logger.warning("失败样例:\n  " + "\n  ".join(fail_examples))
    return 0 if fail == 0 else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="观测枢全量采集（双通道 JSON API）")
    parser.add_argument("-c", "--categories", nargs="+", help="限定分类，例如 -c 敌人 教程")
    parser.add_argument("--skip-cats", nargs="+", default=[], help="跳过分类，例如 --skip-cats 角色 武器 圣遗物")
    parser.add_argument("-l", "--limit", type=int, help="只处理前 N 条（抽样验证用）")
    parser.add_argument("--workers", type=int, default=8, help="并发线程数，默认 8")
    parser.add_argument("--force", action="store_true", help="忽略已完成，全部重爬")
    parser.add_argument("--retry-failed", action="store_true", help="只重试失败条目")
    parser.add_argument("--flush-every", type=int, default=40, help="每 N 条落盘一次进度")
    parser.add_argument("--quiet", action="store_true", help="不显示进度条")
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
