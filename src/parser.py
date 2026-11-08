import json
import re
from typing import Optional
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from loguru import logger


NOISE_PATTERNS = [
    re.compile(r"首页\s*>"),
    re.compile(r"攻略\s*>"),
    re.compile(r"观测枢"),
    re.compile(r"旅行者创作平台"),
    re.compile(r"数据加载中"),
    re.compile(r"COPYRIGHT"),
    re.compile(r"©"),
    re.compile(r"意见反馈"),
    re.compile(r"内容问题"),
    re.compile(r"为你推荐"),
    re.compile(r"相关推荐"),
    re.compile(r"查看更多"),
    re.compile(r"展开全部"),
    re.compile(r"收起"),
    re.compile(r"举报"),
    re.compile(r"分享"),
    re.compile(r"已收藏"),
    re.compile(r"米游社"),
    re.compile(r"渝ICP备"),
    re.compile(r"渝公网安备"),
    re.compile(r"网络文化经营许可证"),
    re.compile(r"适龄提示"),
    re.compile(r"原神wiki$"),
    re.compile(r"^折叠$"),
    re.compile(r"^展开$"),
    re.compile(r"^展开目录$"),
    re.compile(r"^全部评论"),
    re.compile(r"^评论\s*\d*$"),
    re.compile(r"^加载中[.…]*$"),
    re.compile(r"没有更多评论"),
    re.compile(r"暂时还没有评论"),
    re.compile(r"加载错误.*重试"),
    re.compile(r"^确认提交$"),
    re.compile(r"^提交$"),
    re.compile(r"^打开App$"),
    re.compile(r"^清空$"),
    re.compile(r"不超过\d+MB"),
    re.compile(r"仅支持上传"),
    re.compile(r"图片支持.*格式"),
]

NOISE_SECTIONS = {
    "目录", "搜索历史", "建议与反馈", "反馈内容",
    "_intro",
}


@dataclass
class WikiEntry:
    content_id: int
    title: str
    url: str
    category: str = ""
    summary: str = ""
    icon: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class WikiDetail:
    content_id: int
    title: str
    url: str
    category: str = ""
    sections: dict = field(default_factory=dict)
    raw_text: str = ""
    images: list = field(default_factory=list)
    tables: list = field(default_factory=list)


@dataclass
class CharacterDetail(WikiDetail):
    rarity: int = 0
    element: str = ""
    weapon_type: str = ""
    constellation: str = ""
    birthday: str = ""
    affiliation: str = ""
    role: str = ""
    title_alias: str = ""
    talents: list = field(default_factory=list)
    constellations: list = field(default_factory=list)
    ascension_materials: list = field(default_factory=list)
    cv_voice: dict = field(default_factory=dict)


@dataclass
class WeaponDetail(WikiDetail):
    rarity: int = 0
    weapon_type: str = ""
    base_atk: int = 0
    sub_stat: str = ""
    sub_stat_value: str = ""
    effect_name: str = ""
    effect_desc: str = ""
    ascension_materials: list = field(default_factory=list)


@dataclass
class ArtifactDetail(WikiDetail):
    rarity: int = 0
    slot: str = ""
    set_2pc: str = ""
    set_4pc: str = ""
    pieces: list = field(default_factory=list)


class ListParser:

    @staticmethod
    def parse_list_api(json_data: dict, category: str = "") -> list[WikiEntry]:
        entries = []
        data = json_data.get("data", {})

        def _walk(obj):
            if isinstance(obj, dict):
                cid = obj.get("content_id")
                title = obj.get("title")
                if cid and title:
                    entries.append(WikiEntry(
                        content_id=cid,
                        title=title,
                        url=f"https://baike.mihoyo.com/ys/obc/content/{cid}/detail",
                        category=category,
                        summary=obj.get("summary", ""),
                        icon=obj.get("icon", ""),
                        metadata={"ext": obj.get("ext", "")},
                    ))
                for v in obj.values():
                    _walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    _walk(item)

        _walk(data)
        logger.info(f"API [{category}] 解析出 {len(entries)} 个条目")
        return entries


class NoiseFilter:

    @staticmethod
    def is_noise(text: str) -> bool:
        text = text.strip()
        if not text:
            return True
        if len(text) <= 1:
            return True
        for pattern in NOISE_PATTERNS:
            if pattern.search(text):
                return True
        return False

    @staticmethod
    def clean_line(line: str) -> str:
        line = re.sub(r"\s+", " ", line).strip()
        return line


class DetailParser:

    @staticmethod
    def _extract_images(soup: BeautifulSoup) -> list:
        images = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src", "")
            alt = img.get("alt", "")
            if src and src.startswith("http"):
                images.append({"src": src, "alt": alt})
        return images

    @staticmethod
    def _extract_tables(soup: BeautifulSoup) -> list:
        tables = []
        for table in soup.find_all("table"):
            rows = []
            for tr in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if cells and any(c for c in cells if c):
                    rows.append(cells)
            if rows:
                tables.append(rows)
        return tables

    @staticmethod
    def _extract_sections(soup: BeautifulSoup, title: str = "") -> dict:
        sections = {}
        current_heading = "_intro"
        current_lines: list[str] = []
        skip_section = False

        for el in soup.descendants:
            if el.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                if current_lines:
                    sections[current_heading] = "\n".join(current_lines)
                current_heading = re.sub(r"\s+", " ", el.get_text(strip=True))
                skip_section = current_heading in NOISE_SECTIONS
                current_lines = []
            elif el.name is None and el.strip():
                if skip_section:
                    continue
                t = NoiseFilter.clean_line(el.strip())
                if t and not NoiseFilter.is_noise(t):
                    current_lines.append(t)

        if current_lines and not skip_section:
            sections[current_heading] = "\n".join(current_lines)
        elif current_heading in sections:
            pass

        return {k: v for k, v in sections.items() if k not in NOISE_SECTIONS and v.strip()}

    @staticmethod
    def _raw_text_from_sections(sections: dict) -> str:
        return "\n\n".join(
            f"## {h}\n{c}" if h != "_intro" else c
            for h, c in sections.items()
        )

    @classmethod
    def parse_rendered(cls, html: str, content_id: int, url: str, category: str = "") -> WikiDetail:
        soup = BeautifulSoup(html, "lxml")

        for tag in soup(["script", "style", "nav", "noscript", "meta", "link"]):
            tag.decompose()

        title = ""
        h1_tag = soup.find("h1")
        if h1_tag:
            title = h1_tag.get_text(strip=True)
        if not title and hasattr(soup, "title") and soup.title:
            title = soup.title.get_text(strip=True)

        sections = cls._extract_sections(soup, title)
        raw_text = cls._raw_text_from_sections(sections)
        images = cls._extract_images(soup)
        tables = cls._extract_tables(soup)

        return WikiDetail(
            content_id=content_id,
            title=title,
            url=url,
            category=category,
            sections=sections,
            raw_text=raw_text,
            images=images,
            tables=tables,
        )

    @classmethod
    def parse(cls, html: str, content_id: int, url: str, category: str = "") -> WikiDetail:
        return cls.parse_rendered(html, content_id, url, category)


class CharacterParser(DetailParser):

    @classmethod
    def parse(cls, html: str, content_id: int, url: str, category: str = "characters") -> CharacterDetail:
        soup = BeautifulSoup(html, "lxml")

        for tag in soup(["script", "style", "nav", "noscript", "meta", "link"]):
            tag.decompose()

        title = ""
        h1_tag = soup.find("h1")
        if h1_tag:
            title = h1_tag.get_text(strip=True)

        char_detail = CharacterDetail(
            content_id=content_id,
            title=title,
            url=url,
            category=category,
        )

        base_info = soup.select_one(".obc-tmpl-roleBaseInfo")
        if base_info:
            rate_icons = base_info.select(".obc-tmpl__rate-icon")
            if rate_icons:
                char_detail.rarity = len(rate_icons)

            for item in base_info.select(".obc-tmp-character__item"):
                key_el = item.select_one(".obc-tmp-character__key")
                val_el = item.select_one(".obc-tmp-character__value")
                if key_el and val_el:
                    k = key_el.get_text(strip=True)
                    v = val_el.get_text(strip=True)
                    if "生日" in k:
                        char_detail.birthday = v
                    elif "所属" in k:
                        char_detail.affiliation = v
                    elif "定位" in k:
                        char_detail.role = v
                        m = re.match(r"(火|水|风|雷|冰|岩|草)元素", v)
                        if m:
                            char_detail.element = m.group(1)
                    elif "武器" in k:
                        char_detail.weapon_type = v
                    elif "命之座" in k:
                        char_detail.constellation = v
                    elif "称号" in k:
                        char_detail.title_alias = v

        ascension = soup.select_one(".obc-tmpl-roleAscension")
        if ascension:
            materials = []
            for row in ascension.select("tr"):
                cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                if cells and any(c for c in cells if c):
                    materials.append(cells)
            char_detail.ascension_materials = materials

        talent_section = soup.select_one(".obc-tmpl-roleTalent")
        if talent_section:
            talents = []
            for table in talent_section.find_all("table"):
                rows = []
                for tr in table.find_all("tr"):
                    cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                    if cells:
                        rows.append(cells)
                if rows:
                    talents.append(rows)
            char_detail.talents = talents

        collapse_panels = soup.select(".obc-tmpl-collapsePanel, .obc-tmpl-fold")
        constellations = []
        for panel in collapse_panels:
            title_el = panel.select_one(".obc-tmpl-fold__title")
            if title_el:
                const_title = title_el.get_text(strip=True)
                content_el = panel.select_one(".obc-tmpl-fold__content, .obc-tmpl-fold__body")
                const_text = ""
                if content_el:
                    const_text = re.sub(r"\s+", " ", content_el.get_text(strip=True))
                constellations.append({"name": const_title, "description": const_text})
        char_detail.constellations = constellations

        voice_section = soup.select_one(".obc-tmpl-roleVoice")
        if voice_section:
            voice_data = {}
            for item in voice_section.select(".obc-tmpl-character__voice-item"):
                content_el = item.select_one(".obc-tmpl-character__voice-content")
                label_el = item.select_one('[class*="title"], [class*="name"]')
                if content_el:
                    vtext = re.sub(r"\s+", " ", content_el.get_text(strip=True))
                    vlabel = label_el.get_text(strip=True) if label_el else ""
                    voice_data[vlabel or f"voice_{len(voice_data)}"] = vtext
            char_detail.cv_voice = voice_data

        sections = cls._extract_sections(soup, title)
        sections = {k: v for k, v in sections.items() if not NoiseFilter.is_noise(k)}
        char_detail.sections = sections
        char_detail.raw_text = cls._raw_text_from_sections(sections)
        char_detail.images = cls._extract_images(soup)
        char_detail.tables = cls._extract_tables(soup)

        return char_detail


class WeaponParser(DetailParser):

    @classmethod
    def parse(cls, html: str, content_id: int, url: str, category: str = "weapons") -> WeaponDetail:
        soup = BeautifulSoup(html, "lxml")

        for tag in soup(["script", "style", "nav", "noscript", "meta", "link"]):
            tag.decompose()

        title = ""
        h1_tag = soup.find("h1")
        if h1_tag:
            title = h1_tag.get_text(strip=True)

        weapon = WeaponDetail(
            content_id=content_id,
            title=title,
            url=url,
            category=category,
        )

        equipment_base = soup.select_one(".obc-tmpl-equipmentBaseInfo")
        if equipment_base:
            rate_icons = equipment_base.select(".obc-tmpl__rate-icon")
            if rate_icons:
                weapon.rarity = len(rate_icons)

            for row in equipment_base.select("tr"):
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                combined = "".join(cells)
                if "装备类型" in combined:
                    m = re.search(r"装备类型[：:]\s*(.+)", combined)
                    if m:
                        weapon.weapon_type = m.group(1).strip()

        for table in soup.select("table"):
            for tr in table.select("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                combined = "".join(cells)
                if not combined:
                    continue
                if "基础攻击力" in combined and not weapon.base_atk:
                    m = re.search(r"基础攻击力[：:]\s*(\d+)", combined)
                    if m:
                        weapon.base_atk = int(m.group(1))
                    sub_m = re.search(r"(?:基础攻击力.*?)(暴击伤害|暴击率|元素充能效率|元素精通)\s*[：:]?\s*([\d.]+%?)", combined)
                    if sub_m:
                        weapon.sub_stat = sub_m.group(1)
                        weapon.sub_stat_value = sub_m.group(2)
                if "精炼" in combined and (not weapon.effect_desc or len(combined) > len(weapon.effect_desc)):
                    effect_match = re.search(r"[)）]阶\s*(.+?)(?:生命值|攻击力|防御力|元素)", combined)
                    if effect_match:
                        weapon.effect_name = effect_match.group(1).strip()[:30]
                    weapon.effect_desc = combined[:200]

        sections = cls._extract_sections(soup, title)
        sections = {k: v for k, v in sections.items() if not NoiseFilter.is_noise(k)}
        weapon.sections = sections
        weapon.raw_text = cls._raw_text_from_sections(sections)
        weapon.images = cls._extract_images(soup)
        weapon.tables = cls._extract_tables(soup)

        return weapon


class ArtifactParser(DetailParser):

    @classmethod
    def parse(cls, html: str, content_id: int, url: str, category: str = "artifacts") -> ArtifactDetail:
        soup = BeautifulSoup(html, "lxml")

        for tag in soup(["script", "style", "nav", "noscript", "meta", "link"]):
            tag.decompose()

        title = ""
        h1_tag = soup.find("h1")
        if h1_tag:
            title = h1_tag.get_text(strip=True)

        artifact = ArtifactDetail(
            content_id=content_id,
            title=title,
            url=url,
            category=category,
        )

        rich_info = soup.select_one(".obc-tmpl-richBaseInfo")
        if rich_info:
            rate_icons = rich_info.select(".obc-tmpl__rate-icon")
            if rate_icons:
                artifact.rarity = len(rate_icons)

        for table in soup.select("table"):
            for tr in table.select("tr")[:6]:
                cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if len(cells) >= 2:
                    key_cell = cells[0]
                    val_cell = cells[1]
                    if "稀有度" in key_cell and artifact.rarity == 0:
                        stars = re.findall(r"(\d)星", val_cell)
                        if stars:
                            artifact.rarity = max(int(s) for s in stars)
                    if "2件套" in key_cell and "效果" in key_cell and not artifact.set_2pc:
                        artifact.set_2pc = val_cell
                    if "4件套" in key_cell and "效果" in key_cell and not artifact.set_4pc:
                        artifact.set_4pc = val_cell
            if artifact.rarity and artifact.set_2pc and artifact.set_4pc:
                break

        piece_names_seen = set()
        pieces = []
        all_rows = list(soup.select("tr"))
        for i, tr in enumerate(all_rows):
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if len(cells) >= 2:
                val_cell = cells[1] if cells[1] else cells[0]
                for pname in ("生之花", "死之羽", "时之沙", "空之杯", "理之冠"):
                    if pname in val_cell and pname not in piece_names_seen:
                        piece_names_seen.add(pname)
                        desc = ""
                        if i + 1 < len(all_rows):
                            next_cells = [td.get_text(strip=True) for td in all_rows[i + 1].find_all(["td", "th"])]
                            next_combined = "".join(next_cells)
                            if "描述" in next_combined or "故事" in next_combined:
                                dm = re.search(r"(?:描述|故事)[：:]\s*(.+)", next_combined)
                                if dm:
                                    desc = dm.group(1)[:300]
                        pieces.append({"name": pname, "description": desc})
                        break
        artifact.pieces = pieces

        sections = cls._extract_sections(soup, title)
        sections = {k: v for k, v in sections.items() if not NoiseFilter.is_noise(k)}
        artifact.sections = sections
        artifact.raw_text = cls._raw_text_from_sections(sections)
        artifact.images = cls._extract_images(soup)
        artifact.tables = cls._extract_tables(soup)

        return artifact


def parse_detail(html: str, content_id: int, url: str, category: str = "") -> WikiDetail:
    parser_map = {
        "characters": CharacterParser,
        "weapons": WeaponParser,
        "artifacts": ArtifactParser,
    }
    parser = parser_map.get(category, DetailParser)
    return parser.parse(html, content_id, url, category)


def detail_to_dict(detail: WikiDetail) -> dict:
    d = {
        "content_id": detail.content_id,
        "title": detail.title,
        "url": detail.url,
        "category": detail.category,
        "sections": detail.sections,
        "raw_text": detail.raw_text,
        "images": detail.images,
        "tables": detail.tables,
    }
    if isinstance(detail, CharacterDetail):
        d.update({
            "rarity": detail.rarity,
            "element": detail.element,
            "weapon_type": detail.weapon_type,
            "constellation": detail.constellation,
            "birthday": detail.birthday,
            "affiliation": detail.affiliation,
            "role": detail.role,
            "title_alias": detail.title_alias,
            "talents": detail.talents,
            "constellations": detail.constellations,
            "ascension_materials": detail.ascension_materials,
            "cv_voice": detail.cv_voice,
        })
    elif isinstance(detail, WeaponDetail):
        d.update({
            "rarity": detail.rarity,
            "weapon_type": detail.weapon_type,
            "base_atk": detail.base_atk,
            "sub_stat": detail.sub_stat,
            "sub_stat_value": detail.sub_stat_value,
            "effect_name": detail.effect_name,
            "effect_desc": detail.effect_desc,
            "ascension_materials": detail.ascension_materials,
        })
    elif isinstance(detail, ArtifactDetail):
        d.update({
            "rarity": detail.rarity,
            "slot": detail.slot,
            "set_2pc": detail.set_2pc,
            "set_4pc": detail.set_4pc,
            "pieces": detail.pieces,
        })
    return d
