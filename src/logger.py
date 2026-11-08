"""统一日志模块 —— 爬取 & 浏览日志持久化

使用方式：
    from src.logger import setup_logging, get_log_path, read_recent_logs

    # 程序入口调用一次
    setup_logging()

    # 之后所有模块正常使用 loguru
    from loguru import logger
    logger.info("xxx")

日志文件：
    logs/app.log      — 全量 (INFO+)
    logs/crawl.log    — 爬取专用 (filter: name=src.scheduler)
    logs/viewer.log   — 浏览专用 (filter: name=viewer)
"""
import sys
from pathlib import Path
from typing import Optional

from loguru import logger as loguru_logger

from src.config import LOG_DIR

LOG_DIR.mkdir(parents=True, exist_ok=True)

_initialized = False


def crawl_filter(record: dict) -> bool:
    """只保留调度器 & 下载器产生的日志"""
    name = record.get("name", "")
    return name.startswith("src.scheduler") or name.startswith("src.downloader")


def viewer_filter(record: dict) -> bool:
    """只保留 viewer 产生的日志"""
    return record.get("name", "").startswith("viewer")


def setup_logging(console: bool = True):
    """配置 loguru 全局 sink（幂等，重复调用无害）"""
    global _initialized
    if _initialized:
        return

    loguru_logger.remove()

    log_format = (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
        "{level: <7} | "
        "{name}:{function}:{line} | "
        "{message}"
    )

    loguru_logger.add(
        LOG_DIR / "app.log",
        format=log_format,
        level="DEBUG",
        rotation="10 MB",
        retention="30 days",
        encoding="utf-8",
        enqueue=True,
    )

    loguru_logger.add(
        LOG_DIR / "crawl.log",
        format=log_format,
        level="DEBUG",
        rotation="10 MB",
        retention="30 days",
        encoding="utf-8",
        enqueue=True,
        filter=crawl_filter,
    )

    loguru_logger.add(
        LOG_DIR / "viewer.log",
        format=log_format,
        level="DEBUG",
        rotation="10 MB",
        retention="30 days",
        encoding="utf-8",
        enqueue=True,
        filter=viewer_filter,
    )

    if console:
        loguru_logger.add(
            sys.stderr,
            format="{time:HH:mm:ss} | {level:<7} | {message}",
            level="INFO",
            colorize=True,
        )

    _initialized = True


def get_log_path(name: str = "app") -> Path:
    return LOG_DIR / f"{name}.log"


def read_recent_logs(name: str = "app", lines: int = 500) -> list[str]:
    """读取最近 N 行日志（供 viewer 展示）"""
    path = get_log_path(name)
    if not path.exists():
        return ["(日志文件不存在)"]
    try:
        with open(path, encoding="utf-8") as f:
            all_lines = f.readlines()
            return [l.rstrip("\n") for l in all_lines[-lines:]]
    except Exception as e:
        return [f"(读取日志失败: {e})"]


def init_viewer_logger():
    """viewer 专用：以 'viewer' 为 name 记录浏览操作"""
    return loguru_logger.bind(name="viewer")
