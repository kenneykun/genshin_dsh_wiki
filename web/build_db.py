"""构建 Web 前端用的 SQLite 索引

用法：
    .venv\\Scripts\\python.exe -m web.build_db
"""
from __future__ import annotations

import sys

from loguru import logger

from web.db import DB_FILE, build_index


def main() -> int:
    logger.info("开始扫描 data/output 并建库…")
    info = build_index()
    logger.success(
        f"索引完成: {info['entries']} 条 / {info['seconds']}s / {info['db_mb']}MB → {DB_FILE}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
