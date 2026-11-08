import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.logger import setup_logging

setup_logging()

from loguru import logger

from src.scheduler import Scheduler
from src.config import CHANNEL_CATEGORIES


def main():
    parser = argparse.ArgumentParser(
        description="原神 Wiki 爬虫 - 全量爬取调度工具",
    )
    parser.add_argument(
        "--categories", "-c",
        nargs="+",
        choices=list(CHANNEL_CATEGORIES.keys()) + ["all"],
        default=["all"],
        help="要爬取的分类 (默认: all)",
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="强制重新爬取所有条目（覆盖进度）",
    )
    parser.add_argument(
        "--retry-failed", "-r",
        action="store_true",
        help="仅重试之前失败的条目",
    )
    parser.add_argument(
        "--limit", "-l",
        type=int,
        default=None,
        help="限制爬取条数（用于测试）",
    )
    parser.add_argument(
        "--incremental", "-i",
        action="store_true",
        help="增量模式：重新检查所有条目，更新有变化的",
    )
    parser.add_argument(
        "--list-index",
        action="store_true",
        help="仅列出索引条目（不爬取详情）",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="显示当前爬取进度",
    )

    args = parser.parse_args()

    if args.list_index:
        from src.storage import Storage
        storage = Storage()
        entries = storage.load_index()
        if not entries:
            from src.downloader import WikiDownloader
            from src.parser import ListParser
            from src.config import LIST_API, LIST_API_PARAMS
            logger.info("索引未缓存，正在从 API 获取...")
            dw = WikiDownloader()
            all_entries = []
            for cat in CHANNEL_CATEGORIES:
                cfg = CHANNEL_CATEGORIES[cat]
                params = {**LIST_API_PARAMS, "channel_id": cfg["channel_id"]}
                data = dw.get_json(LIST_API, params=params)
                entries_list = ListParser.parse_list_api(data, category=cat)
                all_entries.extend(entries_list)
                logger.info(f"  {cat}: {len(entries_list)} 条")
            dw.close()
            entries_dict = []
            seen = set()
            for e in all_entries:
                if e.content_id not in seen:
                    seen.add(e.content_id)
                    entries_dict.append({
                        "content_id": e.content_id,
                        "title": e.title,
                        "url": e.url,
                        "category": e.category,
                        "summary": e.summary,
                        "icon": e.icon,
                        "metadata": e.metadata,
                    })
            storage.save_index(entries_dict)
            entries = entries_dict

        logger.info(f"全站共 {len(entries)} 个条目:")
        for e in entries:
            logger.info(f"  [{e['category']:15}] {e['content_id']:>6}  {e['title']}")
        return

    if args.stats:
        from src.storage import Storage
        storage = Storage()
        progress = storage.load_progress()
        completed = len(progress["completed"])
        failed = len(progress["failed"])
        entries = storage.load_index()
        total = len(entries) or 12749
        logger.info(f"爬取进度: {completed}/{total} 完成, {failed} 失败, {total - completed - failed} 待爬")
        if failed:
            logger.info(f"失败ID: {list(progress['failed'].keys())[:20]}...")
        return

    categories = args.categories
    if "all" in categories:
        categories = list(CHANNEL_CATEGORIES.keys())

    scheduler = Scheduler(
        categories=categories,
        force=args.force,
        retry_failed=args.retry_failed,
        limit=args.limit,
        incremental=args.incremental,
    )
    scheduler.run()


if __name__ == "__main__":
    main()
