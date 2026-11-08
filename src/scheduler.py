import json
import time
import sys
import threading
from pathlib import Path
from typing import Optional, Callable

from loguru import logger

from src.downloader import WikiDownloader
from src.parser import ListParser, parse_detail, detail_to_dict
from src.storage import Storage
from src.config import (
    CHANNEL_CATEGORIES,
    LIST_API,
    LIST_API_PARAMS,
    RAW_DIR,
    OUTPUT_DIR,
    DATA_DIR,
)


class Scheduler:
    def __init__(
        self,
        categories: Optional[list[str]] = None,
        force: bool = False,
        retry_failed: bool = False,
        limit: Optional[int] = None,
        incremental: bool = False,
        sort_by_coverage: bool = False,
        on_progress: Optional[Callable] = None,
        on_log: Optional[Callable] = None,
        stop_event: Optional[threading.Event] = None,
    ):
        self.categories = categories or list(CHANNEL_CATEGORIES.keys())
        self.force = force
        self.retry_failed = retry_failed
        self.limit = limit
        self.incremental = incremental
        self.sort_by_coverage = sort_by_coverage
        self.on_progress = on_progress
        self.on_log = on_log
        self.stop_event = stop_event or threading.Event()
        self.storage = Storage()
        self.downloader = WikiDownloader()
        self._failures: list[tuple[int, str]] = []
        self._total_tasks: int = 0
        self._done_count: int = 0
        self._start_time: float = 0.0

    def _log(self, level: str, msg: str):
        if self.on_log:
            self.on_log(level, msg)
        getattr(logger, level)(msg)

    def _emit_progress(self, cid: int, title: str, cat: str, status: str, text_len: int = 0):
        if self.on_progress:
            self.on_progress({
                "content_id": cid,
                "title": title,
                "category": cat,
                "status": status,
                "text_len": text_len,
                "done": self._done_count,
                "total": self._total_tasks,
                "failures": len(self._failures),
                "elapsed": time.time() - self._start_time,
            })

    def _should_stop(self) -> bool:
        return self.stop_event.is_set()

    def run(self):
        self._start_time = time.time()
        self._log("info", f"调度器启动: categories={self.categories}, force={self.force}")

        try:
            self._fetch_all_lists()
            if self._should_stop():
                self._log("info", "用户中断：停止爬取")
                return
            self._crawl_all_details()

            if self._should_stop():
                self._log("info", "用户中断：爬取已停止")
            elif self._failures:
                self._log("warning", f"共 {len(self._failures)} 个条目失败，已记录到 progress.json")
            else:
                self._log("success", "全量爬取完成，无失败条目")
        finally:
            self.downloader.close()

    def _fetch_all_lists(self):
        existing = self.storage.load_index()
        full_set = set(CHANNEL_CATEGORIES.keys())
        requested_set = set(self.categories)

        if existing and not self.force:
            self._log("info", f"从缓存加载全站索引: {len(existing)} 条")
            return existing

        if self.force and requested_set != full_set and existing:
            self._log("info", f"强制刷新部分分类: {self.categories}")
        else:
            self._log("info", "正在抓取全站索引...")
            existing = []

        all_entries = []
        for cat in self.categories:
            if self._should_stop():
                return existing
            if cat not in CHANNEL_CATEGORIES:
                self._log("warning", f"未知分类: {cat}, 跳过")
                continue
            cfg = CHANNEL_CATEGORIES[cat]
            params = {**LIST_API_PARAMS, "channel_id": cfg["channel_id"]}
            try:
                data = self.downloader.get_json(LIST_API, params=params)
                entries = ListParser.parse_list_api(data, category=cat)
                all_entries.extend(entries)
                self._log("info", f"  {cat}: {len(entries)} 条")
            except Exception as e:
                self._log("error", f"  {cat}: 获取失败 - {e}")

        new_entries = []
        for e in all_entries:
            new_entries.append({
                "content_id": e.content_id,
                "title": e.title,
                "url": e.url,
                "category": e.category,
                "summary": e.summary,
                "icon": e.icon,
                "metadata": e.metadata,
            })

        merged = {e["content_id"]: e for e in existing}
        for e in new_entries:
            merged[e["content_id"]] = e

        entries_dict = list(merged.values())
        self.storage.save_index(entries_dict)
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        self._log("info", f"全站索引: {len(entries_dict)} 条 (合并后)")
        return entries_dict

    def _crawl_all_details(self):
        entries = self.storage.load_index()
        if not entries:
            self._log("error", "索引为空，请先抓取索引")
            return

        full_set = set(CHANNEL_CATEGORIES.keys())
        requested_set = set(self.categories)
        if requested_set != full_set:
            entries = [e for e in entries if e.get("category", "misc") in requested_set]
            self._log("info", f"已按分类过滤: {', '.join(self.categories)} ({len(entries)} 条)")

        progress = self.storage.load_progress()
        completed = set(int(k) for k in progress["completed"].keys())

        if self.retry_failed:
            failed_ids = self.storage.get_failed_ids()
            tasks = [e for e in entries if e["content_id"] in failed_ids]
            self._log("info", f"重试模式: {len(tasks)} 个失败条目")
        elif self.incremental:
            tasks = list(entries)
            self._log("info", f"增量模式: {len(tasks)} 个条目")
        elif self.force:
            tasks = list(entries)
            self._log("info", f"强制模式: {len(tasks)} 个条目")
        else:
            tasks = [e for e in entries if e["content_id"] not in completed]
            self._log("info", f"续爬模式: {len(tasks)} 未爬取 / {len(entries)} 总计")

        if self.limit:
            tasks = tasks[:self.limit]
            self._log("info", f"限制数量: {self.limit}")

        if self.sort_by_coverage and not self.force:
            cid_to_cat = {e["content_id"]: e.get("category", "misc") for e in entries}
            cat_crawled: dict[str, int] = {}
            cat_total: dict[str, int] = {}
            for e in entries:
                c = e.get("category", "misc")
                cat_total[c] = cat_total.get(c, 0) + 1
                cat_crawled[c] = 0
            for cid_s in completed:
                c = cid_to_cat.get(int(cid_s), "")
                if c:
                    cat_crawled[c] = cat_crawled.get(c, 0) + 1
            tasks.sort(key=lambda e: cat_crawled.get(e.get("category", "misc"), 0) / max(cat_total.get(e.get("category", "misc"), 1), 1))
            coverage_summary = ", ".join(
                f"{c}={cat_crawled.get(c,0)}/{cat_total.get(c,1)}"
                for c in sorted(cat_total, key=lambda c: cat_crawled.get(c,0)/max(cat_total.get(c,1),1))[:5]
            )
            self._log("info", f"覆盖率排序: {coverage_summary} (优先未爬分类)")

        if not tasks:
            self._log("success", "没有待爬取的条目，全部完成！")
            return

        self._total_tasks = len(tasks)

        for entry in tasks:
            if self._should_stop():
                break

            cid = entry["content_id"]
            title = entry["title"]
            cat = entry.get("category", "misc")

            if not self.force and not self.retry_failed and not self.incremental:
                if self.storage.is_completed(cid):
                    self._done_count += 1
                    continue

            self._emit_progress(cid, title, cat, "crawling", 0)

            try:
                html = self.downloader.get_content_detail(cid)
                detail = parse_detail(html, cid, entry["url"], cat)
                detail_dict = detail_to_dict(detail)

                text_len = len(detail.raw_text)
                saved_path = self.storage.save_detail(detail_dict, cat)
                self.storage.save_plaintext(cid, detail.title, detail.raw_text)
                self.storage.mark_completed(cid, str(saved_path), detail_dict)
                self._done_count += 1

                self._emit_progress(cid, title, cat, "done", text_len)

            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}"
                self._failures.append((cid, err_msg))
                self.storage.mark_failed(cid, err_msg)
                self._done_count += 1
                self._emit_progress(cid, title, cat, "fail", 0)
                self._log("error", f"[{cid}] {title}: {err_msg}")

        elapsed = time.time() - self._start_time
        self._log("info", f"本轮完成: 成功 {self._done_count - len(self._failures)}, 失败 {len(self._failures)}, 耗时 {elapsed:.0f}s")
