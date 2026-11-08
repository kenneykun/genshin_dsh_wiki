from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = PROJECT_ROOT / "data"
SITEMAP_DIR = DATA_DIR / "sitemap"
RAW_DIR = DATA_DIR / "raw"
OUTPUT_DIR = DATA_DIR / "output"
LOG_DIR = PROJECT_ROOT / "logs"

BASE_URL = "https://baike.mihoyo.com"
WIKI_HOME = f"{BASE_URL}/ys/obc/?bbs_presentation_style=no_header&mihoyo_app=true&visit_device=pc"
CONTENT_DETAIL = f"{BASE_URL}/ys/obc/content/{{content_id}}/detail"
CHANNEL_MAP = f"{BASE_URL}/ys/obc/channel/map/{{map_id}}/{{channel_id}}"
DETAIL_PARAMS = "bbs_presentation_style=no_header&mihoyo_app=true&visit_device=mobile"

LIST_API = "https://api-static.mihoyo.com/common/blackboard/ys_obc/v1/home/content/list"
LIST_API_PARAMS = {
    "app_sn": "ys_obc",
    "channel_id": 189,
}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": f"{BASE_URL}/ys/obc/",
    "Origin": BASE_URL,
}

API_HEADERS = {
    **BROWSER_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}

REQUEST_DELAY_MIN = 1.0
REQUEST_DELAY_MAX = 3.0
REQUEST_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_MULTIPLIER = 2.0
RETRY_MIN_WAIT = 2.0
RETRY_MAX_WAIT = 30.0

CHANNEL_CATEGORIES = {
    "角色":       {"map_id": 189, "channel_id": 25},
    "角色逸闻":   {"map_id": 189, "channel_id": 261},
    "武器":       {"map_id": 189, "channel_id": 5},
    "圣遗物":     {"map_id": 189, "channel_id": 218},
    "组织":       {"map_id": 189, "channel_id": 255},
    "空月之歌":   {"map_id": 189, "channel_id": 257},
    "敌人":       {"map_id": 189, "channel_id": 6},
    "地图文本":   {"map_id": 189, "channel_id": 251},
    "成就":       {"map_id": 189, "channel_id": 252},
    "食物":       {"map_id": 189, "channel_id": 21},
    "头像":       {"map_id": 189, "channel_id": 244},
    "背包":       {"map_id": 189, "channel_id": 13},
    "活动":       {"map_id": 189, "channel_id": 105},
    "任务":       {"map_id": 189, "channel_id": 43},
    "动物":       {"map_id": 189, "channel_id": 49},
    "书籍":       {"map_id": 189, "channel_id": 68},
    "冒险家协会": {"map_id": 189, "channel_id": 55},
    "NPC与商店":  {"map_id": 189, "channel_id": 20},
    "秘境":       {"map_id": 189, "channel_id": 54},
    "洞天":       {"map_id": 189, "channel_id": 130},
    "深境螺旋":   {"map_id": 189, "channel_id": 65},
    "名片":       {"map_id": 189, "channel_id": 109},
    "装扮":       {"map_id": 189, "channel_id": 211},
    "教程":       {"map_id": 189, "channel_id": 227},
    "幻想真境剧诗":{"map_id": 189, "channel_id": 249},
    "酒馆挑战":   {"map_id": 189, "channel_id": 260},
    "原神OST":    {"map_id": 80,  "channel_id": 254},
    "观景点":     {"map_id": 190, "channel_id": 253},
    "观测考据":   {"map_id": 190, "channel_id": 62},
    "限时祈愿":   {"map_id": 0,   "channel_id": 226},
    "卡牌图鉴":   {"map_id": 0,   "channel_id": 231},
    "圣遗物图鉴": {"map_id": 194, "channel_id": 197},
}
