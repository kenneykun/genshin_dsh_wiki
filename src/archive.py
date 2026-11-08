"""一键归档：把项目快照到备份盘，可选压缩成 zip

用法：
    .venv\\Scripts\\python.exe -m src.archive                     # 快照到 F:\\backup\\yuanshen-wiki-<时间戳>
    .venv\\Scripts\\python.exe -m src.archive --no-zip            # 只快照不压缩
    .venv\\Scripts\\python.exe -m src.archive --target D:\\bak     # 换个备份盘
    .venv\\Scripts\\python.exe -m src.archive --name yuanshen-wiki-20261108 --reuse
    .venv\\Scripts\\python.exe -m src.archive --zip-only F:\\backup\\yuanshen-wiki-20261108

细节：
- 归档前先 WAL checkpoint，保证 wiki.db 单文件自洽（服务开着也能跑）
- 排除 __pycache__ / *_web.log / *.pyc 等可再生内容
- 生成 MANIFEST.txt（含关键文件 SHA256）与 ARCHIVE.md（恢复说明）
- 校验：文件数、关键文件哈希、SQLite 完整性（只读 immutable 方式打开副本）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "data" / "output"
DEFAULT_TARGET = Path(r"F:\backup")

EXCLUDE_DIRS = {"__pycache__", ".pip-tmp", ".pip-cache", ".pytest_cache", ".git"}
EXCLUDE_FILES = {"_web.log", "_MANIFEST.txt", "_ARCHIVE.md"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}

KEY_FILES = [
    "data/output/wiki.db",
    "data/output/index.json",
    "data/output/progress.json",
    "README.md",
    "requirements.txt",
    "web/server.py",
    "mcp_server/server.py",
    "src/build_graph.py",
]


def human(n: float) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def checkpoint_db() -> None:
    db = OUTPUT_DIR / "wiki.db"
    if not db.exists():
        logger.warning("wiki.db 不存在，跳过 checkpoint")
        return
    try:
        conn = sqlite3.connect(db)
        result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        conn.close()
        logger.info(f"wiki.db checkpoint: {result}")
    except sqlite3.DatabaseError as exc:
        logger.warning(f"checkpoint 失败（可忽略，复制时 WAL 会一起带走）：{exc}")


def copy_tree(source: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    excludes = [f"/XD", *EXCLUDE_DIRS, "/XF", *EXCLUDE_FILES, "*.pyc", "*.pyo", "/R:1", "/W:1"]
    cmd = [
        "robocopy", str(source), str(dest), "/E", "/NFL", "/NDL", "/NJH", "/NP", "/MT:8",
        *[str(e) for e in excludes],
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
        if proc.returncode >= 8:
            logger.error(f"robocopy 失败（{proc.returncode}）：{proc.stdout[-500:]}")
            raise RuntimeError("robocopy")
        logger.info(f"robocopy 完成（exit={proc.returncode}）")
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        logger.warning(f"robocopy 不可用（{exc}），改用 shutil")

        def ignore(_dir, names):
            return [n for n in names
                    if n in EXCLUDE_DIRS or n in EXCLUDE_FILES or Path(n).suffix in EXCLUDE_SUFFIXES]

        shutil.copytree(source, dest, dirs_exist_ok=True, ignore=ignore)

    return sum(1 for _ in dest.rglob("*") if _.is_file())


def tree_stats(root: Path) -> dict:
    files = [p for p in root.rglob("*") if p.is_file()]
    return {
        "files": len(files),
        "bytes": sum(p.stat().st_size for p in files),
        "json": len(list((root / "data" / "output").rglob("*.json"))),
        "text": len(list((root / "data" / "output" / "text").rglob("*.txt"))),
    }


def db_stats(root: Path) -> dict:
    db = root / "data" / "output" / "wiki.db"
    if not db.exists():
        return {}
    uri = f"file:{db.as_posix()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        data = {"integrity": conn.execute("PRAGMA integrity_check").fetchone()[0]}
        for table in ("entries", "mentions", "related"):
            try:
                data[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                data[table] = 0
        try:
            data["chars"] = conn.execute("SELECT COALESCE(SUM(chars),0) FROM entries").fetchone()[0]
        except sqlite3.OperationalError:
            data["chars"] = 0
    finally:
        conn.close()
    return data


def _write_text(path: Path, text: str) -> None:
    """写文件；直接写失败时依次尝试 PowerShell 复制，最后只告警不中断归档。"""
    try:
        path.write_text(text, encoding="utf-8")
        return
    except PermissionError:
        pass

    tmp = PROJECT_ROOT / f"_archive_{path.name}"
    tmp.write_text(text, encoding="utf-8")
    commands = [
        ["powershell", "-NoProfile", "-Command",
         f"Copy-Item -LiteralPath '{tmp}' -Destination '{path}' -Force"],
        ["cmd", "/c", "copy", "/y", str(tmp), str(path)],
    ]
    for cmd in commands:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0 and path.exists():
                tmp.unlink(missing_ok=True)
                return
        except OSError:
            continue
    logger.warning(f"无法写入 {path.name}，已留在工作区：{tmp}（手动复制即可）")


def write_docs(root: Path, stats: dict, db: dict) -> None:
    total = human(stats["bytes"])
    lines = [
        "原神观测枢本地资料库 · 归档清单",
        "=" * 46,
        f"归档时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"源目录：  {PROJECT_ROOT}",
        f"归档目录：{root}",
        "",
        f"文件总数：{stats['files']:,}",
        f"总体积：  {total}",
        f"条目 JSON：{stats['json']:,} 个",
        f"纯文本：  {stats['text']:,} 个",
        "",
        "数据规模",
        "-" * 46,
        f"词条：    {db.get('entries', 0):,}",
        f"正文字数：{db.get('chars', 0):,}",
        f"引用边：  {db.get('mentions', 0):,}",
        f"推荐边：  {db.get('related', 0):,}",
        f"SQLite 完整性：{db.get('integrity', 'n/a')}",
        "",
        "关键文件 SHA256",
        "-" * 46,
    ]
    for rel in KEY_FILES:
        path = root / rel
        if path.exists():
            lines.append(f"{sha256(path)}  {rel}  ({human(path.stat().st_size)})")
    lines += [
        "",
        "校验方式：",
        "  Get-FileHash <file> -Algorithm SHA256 对比上方摘要；",
        "  wiki.db 可在只读方式下打开：sqlite3.connect('file:<path>?mode=ro&immutable=1', uri=True)",
        "  .venv 一并归档，路径内直接调用 .venv\\Scripts\\python.exe 可运行。",
    ]
    _write_text(root / "MANIFEST.txt", "\n".join(lines))
    _write_text(root / "ARCHIVE.md", 
        f"""# 原神观测枢本地资料库 · 归档

归档时间：{time.strftime('%Y-%m-%d %H:%M:%S')}
源目录：`{PROJECT_ROOT}`

| 项 | 数量 / 大小 |
|---|---|
| 文件总数 | {stats['files']:,} |
| 总体积 | {total} |
| 词条 | {db.get('entries', 0):,} 条 / {db.get('chars', 0):,} 字 |
| 条目 JSON | {stats['json']:,} 个 |
| 纯文本 | {stats['text']:,} 个 |
| 关联图谱 | 提及 {db.get('mentions', 0):,} / 推荐 {db.get('related', 0):,} |

## 恢复运行

```powershell
# 1) 复制回工作目录（MCP/DSH 插件里写的是绝对路径，搬家后改 cordis.patch.yml 重装）
# 2) 若 .venv 不可用
python -m venv .venv
.venv\\Scripts\\python.exe -m pip install -r requirements.txt

# 3) 启动网页
.venv\\Scripts\\python.exe -m web.server --open

# 4) 重建索引 / 图谱
.venv\\Scripts\\python.exe -m web.build_db
.venv\\Scripts\\python.exe -m src.build_graph
```

MCP 接入见 `mcp_server/README.md`；DSH 插件包在 `plugins/dsh-wiki-mcp/`。
""",
    )


def make_zip(root: Path) -> Path:
    zip_path = root.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    try:
        proc = subprocess.run(
            ["tar", "-a", "-c", "-f", str(zip_path), "-C", str(root.parent), root.name],
            capture_output=True, text=True, timeout=3600,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[-300:])
    except (FileNotFoundError, subprocess.SubprocessError, RuntimeError) as exc:
        logger.warning(f"tar 打包不可用（{exc}），改用 zipfile（较慢）")
        shutil.make_archive(str(root), "zip", root_dir=str(root.parent), base_dir=root.name)
    return zip_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="项目一键归档")
    parser.add_argument("--target", default=str(DEFAULT_TARGET), help="备份根目录")
    parser.add_argument("--name", default=None, help="归档目录名（默认带时间戳）")
    parser.add_argument("--reuse", action="store_true", help="目标已存在时覆盖刷新")
    parser.add_argument("--no-zip", action="store_true", help="不生成 zip")
    parser.add_argument("--zip-only", default=None, help="只打包已存在的归档目录")
    args = parser.parse_args(argv)

    if args.zip_only:
        root = Path(args.zip_only)
        if not root.is_dir():
            logger.error(f"目录不存在：{root}")
            return 1
        zip_path = make_zip(root)
        logger.success(f"已打包：{zip_path}（{human(zip_path.stat().st_size)}）")
        return 0

    name = args.name or f"yuanshen-wiki-{time.strftime('%Y%m%d-%H%M')}"
    root = Path(args.target) / name
    if root.exists() and not args.reuse:
        logger.error(f"目标已存在：{root}（加 --reuse 覆盖，或换 --name）")
        return 1

    started = time.time()
    logger.info(f"归档 → {root}")
    checkpoint_db()
    copy_tree(PROJECT_ROOT, root)

    stats = tree_stats(root)
    db = db_stats(root)
    write_docs(root, stats, db)

    logger.info(
        f"快照 {stats['files']:,} 个文件 / {human(stats['bytes'])}；"
        f"词条 {db.get('entries', 0):,}，完整性 {db.get('integrity')}"
    )
    if db.get("integrity") not in (None, "ok"):
        logger.error("SQLite 完整性校验失败，请检查归档")

    if not args.no_zip:
        zip_path = make_zip(root)
        logger.success(f"已打包：{zip_path}（{human(zip_path.stat().st_size)}）")

    logger.success(f"归档完成，用时 {time.time() - started:.0f}s → {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


