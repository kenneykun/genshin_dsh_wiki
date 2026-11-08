"""用现有素材生成梗概封面 PNG

做法：拿词条已有的图（用户上传立绘 / 物品图标）当主体，叠上分类渐变底、
标题文字和标签，生成 3:4 的封面图，存到 web/static/covers/<id>.png。

    .venv\\Scripts\\python.exe -m src.make_covers --limit 20
    .venv\\Scripts\\python.exe -m src.make_covers --ids 1290 1360
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sys
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from src.config import OUTPUT_DIR
from web import db as wiki_db

ROOT = Path(__file__).resolve().parent.parent
COVER_DIR = ROOT / "web" / "static" / "covers"
CACHE_DIR = OUTPUT_DIR / "_assets"
FONT_BOLD = Path(r"C:\Windows\Fonts\msyhbd.ttc")
FONT_REGULAR = Path(r"C:\Windows\Fonts\msyh.ttc")

SIZE = (720, 960)

# 分类 → (背景起色, 背景止色, 强调色)
PALETTES = {
    "角色": ((46, 60, 92), (18, 26, 44), (233, 203, 141)),
    "武器": ((58, 48, 70), (24, 20, 34), (214, 178, 224)),
    "圣遗物": ((64, 52, 30), (28, 22, 14), (238, 205, 140)),
    "敌人": ((60, 34, 34), (26, 16, 18), (232, 160, 150)),
    "任务": ((38, 58, 66), (16, 26, 32), (160, 210, 220)),
    "书籍": ((56, 48, 40), (24, 20, 18), (226, 205, 170)),
    "观测考据": ((40, 48, 60), (18, 22, 30), (180, 200, 226)),
    "NPC与商店": ((42, 56, 48), (18, 26, 22), (186, 214, 186)),
    "背包": ((60, 50, 42), (26, 22, 20), (226, 200, 170)),
    "食物": ((66, 46, 38), (28, 20, 18), (238, 190, 160)),
    "洞天": ((40, 56, 52), (16, 26, 24), (180, 220, 200)),
}
DEFAULT_PALETTE = ((48, 48, 56), (22, 22, 28), (214, 214, 224))


# ---------------------------------------------------------------- 素材
def _cover_source(content_id: int, category: str) -> str:
    path = OUTPUT_DIR / category / f"{content_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return ""
    images = [((img or {}).get("src") or "") for img in (data.get("images") or [])]
    # 老模型页会把评论区头像抓进来，这些不是词条素材
    images = [
        src for src in images
        if src.startswith("http") and not src.endswith(".gif")
        and "avatarDefault" not in src and "/avatar/" not in src
    ]
    meta = data.get("api_meta") or {}
    items = {"背包", "食物", "洞天", "名片", "武器", "圣遗物", "敌人", "动物", "装扮"}
    icon = (meta.get("icon") or "").strip()
    if icon.startswith("http") and "avatarDefault" not in icon:
        return icon
    cover = next((src for src in images if "wiki-user-upload" in src), "")
    if not cover and category in items:
        cover = next((src for src in images if "ys-obc" in src), "")
    if not cover:
        cover = next(iter(images), "")
    return cover


def _download(url: str, cache_key: str) -> Optional[Image.Image]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ext = ".png" if ".png" in url.lower() else ".jpg"
    cached = CACHE_DIR / f"{cache_key}{ext}"
    try:
        if cached.exists() and cached.stat().st_size > 1000:
            return Image.open(cached).convert("RGBA")
        resp = httpx.get(url, timeout=30, follow_redirects=True)
        if resp.status_code != 200:
            return None
        cached.write_bytes(resp.content)
        return Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"取图失败 {cache_key}: {type(exc).__name__}")
        return None


# ---------------------------------------------------------------- 绘制
def _gradient(size, top, bottom) -> Image.Image:
    width, height = size
    base = Image.new("RGB", (1, height))
    for y in range(height):
        ratio = y / max(1, height - 1)
        base.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3)))
    return base.resize((width, height), Image.BILINEAR)


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    lines, current = [], ""
    for char in text:
        trial = current + char
        if draw.textlength(trial, font=font) > max_width and current:
            lines.append(current)
            current = char
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def _glow(canvas: Image.Image, center, radius: int, color) -> None:
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.ellipse(
        [center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius],
        fill=(color[0], color[1], color[2], 90),
    )
    canvas.alpha_composite(layer.filter(ImageFilter.GaussianBlur(radius // 2)))


def compose(entry_title: str, note_title: str, hook: str, category: str,
            asset: Optional[Image.Image], tags: list[str]) -> Image.Image:
    top, bottom, accent = PALETTES.get(category, DEFAULT_PALETTE)
    canvas = _gradient(SIZE, top, bottom).convert("RGBA")

    # 装饰：两团柔光
    _glow(canvas, (int(SIZE[0] * 0.78), int(SIZE[1] * 0.22)), 260, accent)
    _glow(canvas, (int(SIZE[0] * 0.2), int(SIZE[1] * 0.82)), 220, accent)

    draw = ImageDraw.Draw(canvas)
    margin = 56

    # 顶部：分类标签
    chip_font = ImageFont.truetype(str(FONT_REGULAR), 30)
    chip_text = f"— {category} —"
    chip_w = draw.textlength(chip_text, font=chip_font)
    draw.text(((SIZE[0] - chip_w) / 2, 58), chip_text, font=chip_font,
              fill=(accent[0], accent[1], accent[2], 235))

    # 主体素材
    if asset is not None:
        art = asset.copy()
        has_alpha = art.mode == "RGBA" and art.getchannel("A").getextrema()[0] < 250
        if art.width < 320:
            scale = min(3.2, 340 / max(1, art.width))
            art = art.resize((int(art.width * scale), int(art.height * scale)), Image.LANCZOS)
            art = art.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))
            if has_alpha:
                # 透明图标：加一层浅色托片
                board_w, board_h = art.width + 56, art.height + 56
                board = Image.new("RGBA", (board_w, board_h), (0, 0, 0, 0))
                ImageDraw.Draw(board).rounded_rectangle(
                    [0, 0, board_w - 1, board_h - 1], radius=36,
                    fill=(255, 255, 255, 26), outline=(255, 255, 255, 60), width=2,
                )
                board.alpha_composite(art, (28, 28))
                box = board
            else:
                # 自带底色的图标：直接切圆角，别叠双框
                mask = Image.new("L", art.size, 0)
                ImageDraw.Draw(mask).rounded_rectangle([0, 0, art.size[0] - 1, art.size[1] - 1], radius=28, fill=255)
                art.putalpha(mask)
                box = art
            x = (SIZE[0] - box.width) // 2
            y = 150 + (int(SIZE[1] * 0.52) - box.height) // 2
            canvas.alpha_composite(box, (x, y))
        else:
            art.thumbnail((int(SIZE[0] * 0.74), int(SIZE[1] * 0.54)), Image.LANCZOS)
            # 底部渐隐，让图和文字融合
            mask = Image.new("L", art.size, 255)
            mdraw = ImageDraw.Draw(mask)
            fade = max(1, int(art.size[1] * 0.28))
            for y in range(art.size[1] - fade, art.size[1]):
                alpha = int(255 * (1 - (y - (art.size[1] - fade)) / fade))
                mdraw.line([(0, y), (art.size[0], y)], fill=alpha)
            art.putalpha(Image.composite(art.getchannel("A"), Image.new("L", art.size, 0), mask))
            x = (SIZE[0] - art.size[0]) // 2
            y = 120 + (int(SIZE[1] * 0.54) - art.size[1]) // 2
            canvas.alpha_composite(art, (x, y))
        draw = ImageDraw.Draw(canvas)

    # 标题
    title_font = ImageFont.truetype(str(FONT_BOLD), 62)
    text = note_title or entry_title
    lines = _wrap(draw, text, title_font, SIZE[0] - margin * 2)[:3]
    y = int(SIZE[1] * 0.63)
    for line in lines:
        width = draw.textlength(line, font=title_font)
        draw.text(((SIZE[0] - width) / 2, y), line, font=title_font, fill=(255, 255, 255, 246))
        y += 78

    # 一句话
    hook_font = ImageFont.truetype(str(FONT_REGULAR), 30)
    if hook:
        hook_lines = _wrap(draw, hook, hook_font, SIZE[0] - margin * 2 - 30)[:2]
        y += 8
        for line in hook_lines:
            width = draw.textlength(line, font=hook_font)
            draw.text(((SIZE[0] - width) / 2, y), line, font=hook_font,
                      fill=(accent[0], accent[1], accent[2], 220))
            y += 42

    # 底部标签
    if tags:
        tag_font = ImageFont.truetype(str(FONT_REGULAR), 26)
        label = "  ".join(f"#{t}" for t in tags[:3])
        width = draw.textlength(label, font=tag_font)
        draw.text(((SIZE[0] - width) / 2, SIZE[1] - 88), label, font=tag_font,
                  fill=(255, 255, 255, 150))

    # 细边框
    draw.rectangle([18, 18, SIZE[0] - 18, SIZE[1] - 18], outline=(255, 255, 255, 26), width=2)
    return canvas.convert("RGB")


# ---------------------------------------------------------------- 主流程
def build(ids: Optional[list[int]] = None, limit: int = 20) -> dict:
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    conn = wiki_db.connect()
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS notes("
        "id INTEGER PRIMARY KEY, title TEXT, hook TEXT, highlights TEXT, tags TEXT,"
        " model TEXT, created_at TEXT);"
    )
    if ids:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT n.*, e.title AS entry_title, e.category FROM notes n "
            f"JOIN entries e ON e.id = n.id WHERE n.id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT n.*, e.title AS entry_title, e.category FROM notes n "
            "JOIN entries e ON e.id = n.id ORDER BY n.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    made, missing = 0, 0
    for row in rows:
        target = COVER_DIR / f"{row['id']}.png"
        note_title = json.loads(json.dumps(row["title"] or ""))
        tags = json.loads(row["tags"] or "[]")
        source = _cover_source(row["id"], row["category"])
        asset = _download(source, f"{row['id']}") if source else None
        if asset is None:
            missing += 1
            logger.warning(f"#{row['id']} {row['entry_title']} 没有可用素材，跳过")
            continue
        image = compose(row["entry_title"], note_title, row["hook"] or "", row["category"], asset, tags)
        image.save(target, optimize=True)
        made += 1
        logger.info(f"#{row['id']} {row['entry_title']} → {target.name}")
    conn.close()
    return {"made": made, "missing": missing}


def build_explore(limit: int = 20) -> dict:
    """给探索式笔记做封面：主体沿用主词条的素材，标题用探索笔记的标题。"""
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    conn = wiki_db.connect()
    try:
        rows = conn.execute(
            "SELECT x.id, x.title, x.hook, x.tags, x.primary_id, e.title AS entry_title, e.category "
            "FROM explore_notes x LEFT JOIN entries e ON e.id = x.primary_id "
            "ORDER BY x.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    made, missing = 0, 0
    for row in rows:
        target = COVER_DIR / f"explore-{row['id']}.png"
        source = _cover_source(row["primary_id"] or 0, row["category"] or "")
        asset = _download(source, f"x{row['id']}") if source else None
        if asset is None:
            missing += 1
            continue
        image = compose(
            row["entry_title"] or "",
            row["title"] or "",
            row["hook"] or "",
            row["category"] or "探索",
            asset,
            json.loads(row["tags"] or "[]"),
        )
        image.save(target, optimize=True)
        made += 1
        logger.info(f"探索#{row['id']} {row['title']} → {target.name}")
    conn.close()
    return {"made": made, "missing": missing}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成梗概封面")
    parser.add_argument("--ids", nargs="*", type=int, default=None)
    parser.add_argument("-l", "--limit", type=int, default=20)
    parser.add_argument("--explore", action="store_true", help="给探索式笔记做封面")
    args = parser.parse_args(argv)
    if args.explore:
        result = build_explore(args.limit)
        logger.success(f"探索封面：成功 {result['made']}，缺素材 {result['missing']}")
        return 0
    result = build(args.ids, args.limit)
    logger.success(f"封面生成：成功 {result['made']}，缺素材 {result['missing']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
