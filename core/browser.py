"""共享的 Playwright 浏览器启动器。

统一集成：
- 反爬对抗参数与真实 Chrome 指纹；
- playwright_stealth 自动注入（若安装）；
- 中文环境（zh-CN）与 Asia/Shanghai 时区模拟；
- 全局并发信号量：最多 MAX_CONCURRENT_BROWSERS 个浏览器会话同时存在
  （参考「抖音自动续火花 2.1」的会话池并发上限，防多账号同时开太多浏览器触发风控）；
- 完善的生命周期管理与异常兜底。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import sync_playwright

from .accounts import acquire_browser_slot, release_browser_slot
from .config import account_state_path, get_valid_state_path

logger = logging.getLogger("douyin-cloud-streak")

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
    state_path 默认自愈寻找账号目录 data/state.json 或根目录 state.json（默认账号）。
    并发控制：进入时占用一个全局浏览器名额，超过上限会阻塞等待。
    """
    valid_state = Path(state_path) if state_path else get_valid_state_path()
    state_file = str(valid_state) if valid_state and valid_state.exists() else None

    acquire_browser_slot()
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
        release_browser_slot()
