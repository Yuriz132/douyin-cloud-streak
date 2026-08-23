"""共享的 Playwright 浏览器启动器。

统一集成：
- 反爬对抗参数与真实 Chrome 指纹；
- playwright_stealth 自动注入（若安装）；
- 中文环境（zh-CN）与 Asia/Shanghai 时区模拟；
- 完善的生命周期管理与异常兜底。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import sync_playwright

from .config import DATA_DIR

logger = logging.getLogger("douyin-cloud-streak")

_STATE_PATH = DATA_DIR / "state.json"

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_COMMON_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
]


def _apply_stealth(page) -> None:
    """尝试注入 stealth 脚本规避常见浏览器自动化指纹检测。"""
    try:
        from playwright_stealth import Stealth
        Stealth().apply_stealth_sync(page)
    except Exception:
        try:
            from playwright_stealth import stealth_sync
            stealth_sync(page)
        except Exception:
            pass


@contextmanager
def open_browser(state_path: Path | str | None = None, headless: bool = True, **ctx_kwargs):
    """启动 Chromium 并返回 (playwright, browser, context, page)。

    用法::

        with open_browser() as (p, browser, context, page):
            page.goto(url)
            ...

    退出 with 块时自动关闭浏览器和 playwright。
    state_path 默认用 data/state.json；若文件不存在则不加载 storage_state。
    """
    target_state = Path(state_path) if state_path else _STATE_PATH
    state_file = str(target_state) if target_state.exists() else None

    p = sync_playwright().start()
    browser = None
    try:
        browser = p.chromium.launch(headless=headless, args=_COMMON_ARGS)
        defaults = {
            "viewport": {"width": 1366, "height": 768},
            "user_agent": _CHROME_UA,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "ignore_https_errors": True,
        }
        if state_file:
            defaults["storage_state"] = state_file
        defaults.update(ctx_kwargs)

        context = browser.new_context(**defaults)
        page = context.new_page()
        _apply_stealth(page)

        yield p, browser, context, page
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        try:
            p.stop()
        except Exception:
            pass
