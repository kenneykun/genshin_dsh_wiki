"""数据更新管理：在网页里跑采集 / 重建索引 / 重建图谱

设计：
- 同一时刻只跑一个任务，任务在后台线程里执行，前端轮询 /api/manage/status 看进度；
- 采集复用 src.crawl_api 的抓取与解析，按 id 或分类批量抓，边抓边记进度；
- 重建索引需要独占 wiki.db，所以先关掉服务端的共享连接，重建后再懒加载；
- 日志走环形缓冲，前端只拿最近 200 行。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

from loguru import logger

from src.api_client import ApiClient, ProgressTracker
from src.api_parser import parse_payload
from src.config import OUTPUT_DIR
from src.storage import INDEX_FILE, Storage
from web import db as wiki_db

JOB_LOG_LIMIT = 200

_state: dict = {"job": None}
_lock = threading.RLock()  # 可重入：start() 持锁时会调用同样加锁的 running()


# ---------------------------------------------------------------- 任务状态
def _new_job(action: str, params: dict) -> dict:
    return {
        "id": f"job-{int(time.time() * 1000)}",
        "action": action,
        "params": params,
        "status": "running",
        "started_at": time.time(),
        "finished_at": None,
        "total": 0,
        "done": 0,
        "ok": 0,
        "fail": 0,
        "message": "",
        "result": None,
        "log": [],
    }


def _log(job: dict, message: str) -> None:
    job["log"].append(f"{time.strftime('%H:%M:%S')} {message}")
    if len(job["log"]) > JOB_LOG_LIMIT:
        del job["log"][: len(job["log"]) - JOB_LOG_LIMIT]


def status() -> dict:
    with _lock:
        job = _state.get("job")
        if not job:
            return {"job": None}
        snapshot = dict(job)
        snapshot["log"] = list(job["log"])
        snapshot["elapsed"] = round((job["finished_at"] or time.time()) - job["started_at"], 1)
        return {"job": snapshot}


def running() -> bool:
    with _lock:
        job = _state.get("job")
        return bool(job and job["status"] == "running")


# ---------------------------------------------------------------- 采集
def _crawl(entries: list[dict], workers: int = 8) -> dict:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    storage = Storage()
    tracker = ProgressTracker(storage, flush_every=20)
    ok = fail = 0
    failures: list[str] = []

    with ApiClient(workers=workers) as client, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for entry in entries:
            futures[pool.submit(_crawl_one, client, storage, tracker, entry)] = entry
        for future in as_completed(futures):
            entry = futures[future]
            try:
                success, detail = future.result()
            except Exception as exc:  # noqa: BLE001
                success, detail = False, f"{type(exc).__name__}: {exc}"
            if success:
                ok += 1
            else:
                fail += 1
                if len(failures) < 10:
                    failures.append(f"{entry['content_id']} {entry.get('title', '')} → {detail}")
            job = _state.get("job")
            if job:
                job["done"] = ok + fail
                job["ok"], job["fail"] = ok, fail
                if (ok + fail) % 20 == 0 or (ok + fail) == len(entries):
                    _log(job, f"进度 {ok + fail}/{len(entries)}（成功 {ok}，失败 {fail}）")

    tracker.flush()
    return {"ok": ok, "fail": fail, "failures": failures}


# 重抓时保留旧文件里的结构化字段（API 通用解析不产出这些）
KEEP_FIELDS = (
    "rarity", "element", "weapon_type", "constellation", "birthday",
    "affiliation", "role", "title_alias", "talents", "constellations",
    "ascension_materials", "cv_voice", "base_atk", "sub_stat",
    "sub_stat_value", "effect_name", "effect_desc", "set_2pc", "set_4pc",
    "pieces", "slot",
)


def _crawl_one(client, storage, tracker, entry: dict) -> tuple[bool, str]:
    cid = entry["content_id"]
    category = entry.get("category") or "其他"
    url = entry.get("url") or f"https://baike.mihoyo.com/ys/obc/content/{cid}/detail"
    try:
        kind, payload = client.fetch(cid)
        detail = parse_payload(kind, payload, cid, url, category)

        # 保留旧文件里的结构化字段，避免重抓把角色/武器的星级、命之座等抹掉
        existing = OUTPUT_DIR / category / f"{cid}.json"
        if existing.exists():
            try:
                old = json.loads(existing.read_text(encoding="utf-8"))
                for key in KEEP_FIELDS:
                    if old.get(key) not in (None, "", 0, [], {}):
                        detail[key] = old[key]
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                pass

        path = storage.save_detail(detail, category)
        storage.save_plaintext(cid, detail.get("title") or entry.get("title", ""), detail.get("raw_text", ""))
        tracker.mark_done(cid, str(path), detail.get("title") or entry.get("title", ""))
        return True, kind
    except Exception as exc:  # noqa: BLE001
        tracker.mark_failed(cid, f"{type(exc).__name__}: {exc}")
        return False, f"{type(exc).__name__}: {exc}"


def _entries_by_ids(ids: list[int]) -> list[dict]:
    index = {int(e["content_id"]): e for e in json.loads(INDEX_FILE.read_text(encoding="utf-8"))}
    out = []
    for cid in ids:
        entry = index.get(cid)
        if entry:
            out.append(entry)
    return out


def _entries_by_category(category: str) -> list[dict]:
    index = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    return [e for e in index if (e.get("category") or "misc") == category]


def _entries_incomplete() -> list[dict]:
    index = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    progress = json.loads((OUTPUT_DIR / "progress.json").read_text(encoding="utf-8"))
    done = {int(k) for k in progress.get("completed", {})}
    return [e for e in index if int(e["content_id"]) not in done]


def _entries_failed() -> list[dict]:
    index = {int(e["content_id"]): e for e in json.loads(INDEX_FILE.read_text(encoding="utf-8"))}
    progress = json.loads((OUTPUT_DIR / "progress.json").read_text(encoding="utf-8"))
    return [index[int(cid)] for cid in progress.get("failed", {}) if int(cid) in index]


# ---------------------------------------------------------------- 重建
def _rebuild_index() -> dict:
    job = _state.get("job")
    job["total"] = 0
    job["done"] = 0

    def report(scanned: int, total: int) -> None:
        if not job:
            return
        job["total"] = total
        job["done"] = scanned
        _log(job, f"读取词条 {scanned}/{total}")

    return wiki_db.build_index(progress=report)


def _rebuild_graph() -> dict:
    from src import build_graph

    code = build_graph.main([])
    return {"exit": code}


# ---------------------------------------------------------------- 任务入口
ACTIONS = {
    "crawl_ids": "按 id 更新词条",
    "crawl_category": "按分类更新",
    "crawl_incomplete": "更新全部缺口",
    "retry_failed": "重试失败条目",
    "rebuild_index": "重建检索索引",
    "rebuild_graph": "重建关联图谱",
    "gen_notes": "批量生成梗概",
}


def start(action: str, params: Optional[dict] = None) -> dict:
    params = params or {}
    with _lock:
        if running():
            return {"error": "已有任务在运行，等它结束再试"}
        if action not in ACTIONS:
            return {"error": f"未知操作：{action}"}
        job = _new_job(action, params)
        _state["job"] = job
    thread = threading.Thread(target=_execute, args=(job,), daemon=True)
    thread.start()
    return {"job_id": job["id"]}


def _execute(job: dict) -> None:
    action = job["action"]
    params = job["params"]
    try:
        _log(job, f"开始：{ACTIONS[action]} {params if params else ''}")

        if action in ("crawl_ids", "crawl_category", "crawl_incomplete", "retry_failed"):
            if action == "crawl_ids":
                ids = [int(x) for x in params.get("ids", []) if str(x).strip()]
                entries = _entries_by_ids(ids)
                if not entries:
                    raise RuntimeError("索引里没有这些 id，先确认 content_id")
            elif action == "crawl_category":
                category = params.get("category") or ""
                entries = _entries_by_category(category)
                if not entries:
                    raise RuntimeError(f"分类「{category}」没有条目")
            elif action == "retry_failed":
                entries = _entries_failed()
                if not entries:
                    raise RuntimeError("没有失败条目")
            else:
                entries = _entries_incomplete()
                if not entries:
                    raise RuntimeError("没有缺口，数据是完整的")

            job["total"] = len(entries)
            _log(job, f"待抓取 {len(entries)} 条")
            result = _crawl(entries, workers=int(params.get("workers") or 8))
            job["result"] = result
            _log(job, f"采集完成：成功 {result['ok']}，失败 {result['fail']}")
            # 采集后自动重建索引，否则页面看到的是旧内容
            _log(job, "重建检索索引…")
            info = _rebuild_index()
            _log(job, f"索引已重建：{info['entries']} 条 / {info['db_mb']}MB")

        elif action == "rebuild_index":
            info = _rebuild_index()
            job["result"] = info
            _log(job, f"索引已重建：{info['entries']} 条 / {info['db_mb']}MB / {info['seconds']}s")

        elif action == "rebuild_graph":
            info = _rebuild_graph()
            job["result"] = info
            _log(job, "关联图谱已重建")

        elif action == "gen_notes":
            from src import gen_notes

            limit = int(params.get("limit") or 100)
            workers = int(params.get("workers") or 8)
            _log(job, f"批量生成梗概：{limit} 条，并发 {workers}")
            job["total"] = limit
            job["done"] = 0

            def report(done: int, total: int, ok: int, fail: int) -> None:
                job["total"] = total
                job["done"] = done
                job["ok"], job["fail"] = ok, fail

            result = gen_notes.generate(limit, workers, params.get("category") or None, report)
            job["result"] = result
            _log(job, f"梗概生成完成：成功 {result['ok']}，失败 {result['fail']}，{result['seconds']}s")

        job["status"] = "done"
        job["message"] = "完成"
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"任务失败: {exc}")
        job["status"] = "failed"
        job["message"] = f"{type(exc).__name__}: {exc}"
        _log(job, f"失败：{job['message']}")
    finally:
        job["finished_at"] = time.time()


# ---------------------------------------------------------------- 数据概况
def overview() -> dict:
    index = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    progress = json.loads((OUTPUT_DIR / "progress.json").read_text(encoding="utf-8"))
    completed = progress.get("completed", {})
    failed = progress.get("failed", {})

    db_path = wiki_db.DB_FILE
    db_mtime = db_path.stat().st_mtime if db_path.exists() else 0
    index_mtime = INDEX_FILE.stat().st_mtime if INDEX_FILE.exists() else 0

    recent = []
    for folder in OUTPUT_DIR.iterdir():
        if not folder.is_dir() or folder.name == "text":
            continue
        for path in folder.glob("*.json"):
            recent.append((path.stat().st_mtime, path.stem, folder.name))
    recent.sort(reverse=True)
    recent_items = [
        {"id": int(cid), "category": cat, "updated": time.strftime("%m-%d %H:%M", time.localtime(mtime))}
        for mtime, cid, cat in recent[:12]
        if cid.isdigit()
    ]

    return {
        "entries": len(index),
        "completed": len(completed),
        "failed": len(failed),
        "missing": max(0, len(index) - len(completed)),
        "index_updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(index_mtime)) if index_mtime else "",
        "db_updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(db_mtime)) if db_mtime else "",
        "db_stale": bool(index_mtime and db_mtime and index_mtime > db_mtime),
        "recent": recent_items,
        "failed_items": list(failed.items())[:10],
        "actions": ACTIONS,
        "running": running(),
        "categories": sorted({(e.get("category") or "misc") for e in index}),
    }
