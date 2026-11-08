import random
import time
import asyncio
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential
from loguru import logger

from src.config import (
    BROWSER_HEADERS,
    API_HEADERS,
    REQUEST_DELAY_MIN,
    REQUEST_DELAY_MAX,
    REQUEST_TIMEOUT,
    MAX_RETRIES,
    RETRY_MULTIPLIER,
    RETRY_MIN_WAIT,
    RETRY_MAX_WAIT,
)


class RateLimiter:
    def __init__(self, min_delay: float = REQUEST_DELAY_MIN, max_delay: float = REQUEST_DELAY_MAX):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._last_request_time = 0.0

    def wait(self):
        elapsed = time.monotonic() - self._last_request_time
        delay = random.uniform(self.min_delay, self.max_delay)
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request_time = time.monotonic()


class WikiDownloader:
    def __init__(
        self,
        min_delay: float = REQUEST_DELAY_MIN,
        max_delay: float = REQUEST_DELAY_MAX,
        timeout: float = REQUEST_TIMEOUT,
        use_browser: bool = True,
    ):
        self.limiter = RateLimiter(min_delay, max_delay)
        self.timeout = timeout
        self.use_browser = use_browser
        self._api_client: Optional[httpx.Client] = None
        self._playwright = None
        self._browser = None
        self._browser_ready = False

    def _build_api_client(self) -> httpx.Client:
        return httpx.Client(
            headers=API_HEADERS.copy(),
            timeout=self.timeout,
            follow_redirects=True,
            http2=True,
        )

    @property
    def api_client(self) -> httpx.Client:
        if self._api_client is None:
            self._api_client = self._build_api_client()
        return self._api_client

    def _init_browser(self):
        if self._browser_ready:
            return

        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()

        import sys
        edge_paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        if sys.platform != "win32":
            edge_paths = []

        import os
        edge_path = None
        for p in edge_paths:
            if os.path.exists(p):
                edge_path = p
                break

        if edge_path:
            self._browser = self._playwright.chromium.launch(
                executable_path=edge_path,
                headless=True,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
            )
            logger.info(f"浏览器已启动: {edge_path}")
        else:
            self._browser = self._playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-gpu"],
            )
            logger.info("浏览器已启动 (chromium)")

        self._browser_ready = True

    def close(self):
        if self._api_client is not None:
            self._api_client.close()
            self._api_client = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
            self._browser_ready = False
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    @retry(
        stop=stop_after_attempt(MAX_RETRIES),
        wait=wait_exponential(multiplier=RETRY_MULTIPLIER, min=RETRY_MIN_WAIT, max=RETRY_MAX_WAIT),
        reraise=True,
    )
    def get_json(self, url: str, params: Optional[dict] = None) -> dict:
        self.limiter.wait()
        logger.debug(f"GET (API) {url} params={params}")
        response = self.api_client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    def _deep_expand(self, page) -> None:
        """点击所有展开/折叠按钮，释放被隐藏的内容"""
        try:
            for name_selector in ["text=展开", "text=折叠"]:
                for btn in page.query_selector_all(name_selector):
                    try:
                        inner = btn.inner_text()
                        if len(inner) > 20:
                            continue
                        btn.click(timeout=1_000)
                        page.wait_for_timeout(500)
                    except Exception:
                        pass

            for el in page.query_selector_all(".obc-tmpl-fold"):
                try:
                    inner = el.inner_text()
                    if "展开" in inner or "折叠" in inner:
                        el.click(timeout=1_000)
                        page.wait_for_timeout(500)
                except Exception:
                    pass

            page.wait_for_timeout(1500)
        except Exception as e:
            logger.warning(f"展开异常: {e}")

    def render_html(self, url: str, wait_for: str = "networkidle") -> str:
        """Playwright 渲染 SPA → 深度展开 → 提取完整 DOM"""
        if not self.use_browser:
            raise RuntimeError("浏览器模式未启用")

        self._init_browser()
        self.limiter.wait()

        logger.debug(f"RENDER {url}")
        page = self._browser.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            selector_list = [
                ".obc-tmpl-content", ".obc-tmpl-part", ".obc-tmpl-module",
                ".obc-tmpl-roleBaseInfo", ".obc-tmpl-equipmentBaseInfo",
                ".obc-tmpl-richBaseInfo", ".obc-tmpl-multiTable", "h1",
            ]
            content_found = False
            for sel in selector_list:
                try:
                    page.wait_for_selector(sel, timeout=10_000)
                    content_found = True
                    logger.debug(f"  content via {sel}")
                    break
                except Exception:
                    continue

            if not content_found:
                try:
                    page.wait_for_load_state("load", timeout=30_000)
                except Exception:
                    pass

            self._deep_expand(page)

            html = page.content()
            if html and len(html) > 2000:
                if any(kw in html for kw in ("角色待上线", "数据加载中", "敬请期待")):
                    raise RuntimeError("占位页面（未上线）")
                return html

            raise RuntimeError(f"无法获取有效内容: {url}")
        finally:
            page.close()

    def get_content_detail(self, content_id: int) -> str:
        from src.config import CONTENT_DETAIL, DETAIL_PARAMS

        url = CONTENT_DETAIL.format(content_id=content_id)
        url = f"{url}?{DETAIL_PARAMS}"
        return self.render_html(url)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
