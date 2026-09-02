"""共享的 Playwright 浏览器启动器。

统一集成：
- 反爬对抗参数与真实 Chrome 指纹；
- playwright_stealth 自动注入（若安装）；
- 中文环境（zh-CN）与 Asia/Shanghai 时区模拟；
- 全局并发信号量：最多 MAX_CONCURRENT_BROWSERS 个浏览器会话同时存在
  （防多账号同时开太多浏览器触发风控）；
- 浏览器实例池（实例按账号复用）：
  所有浏览器任务经 core.executor 的单一工作线程顺序执行，sync Playwright
  实例绑定该线程，可安全复用。同一登录态账号的短任务（同步联系人 / 发送）
  复用同一个 Chromium 实例，避免每次冷启动 launch 的耗时；空闲超时后惰性
  回收，进程退出时统一清理。
- 完善的生命周期管理与异常兜底。
"""

from __future__ import annotations

import logging
import threading
import time
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

# ── 浏览器实例池 ────────────────────────────────────────────────────────
# 池 key = 登录态文件路径（无登录态时用 "__no_state__"），即按账号隔离。
# entry = {"browser", "refs", "last_used"}
# 说明：所有 open_browser 都在 core.executor 的单工作线程内调用，实例只被
# 该线程使用，因此可安全复用；回收也只在工作线程内惰性进行。
#
# Playwright 驱动进程（sync_playwright().start()）为进程内全局单例：sync API
# 的 start() 会在当前线程留下一个运行中的 asyncio loop，同一线程再次 start()
# 必然报 "Playwright Sync API inside the asyncio loop"，因此按 key 复用浏览器
# 的同时，playwright 驱动只能启动一次（官方推荐用法）。
_POOL_LOCK = threading.Lock()
_BROWSER_POOL: dict[str, dict] = {}
_PW = None
_PW_LOCK = threading.Lock()
POOL_IDLE_TIMEOUT = 120.0  # 秒：空闲超时后自动回收实例


def _ensure_playwright():
    """返回进程内唯一的 sync Playwright 驱动（惰性启动一次，线程安全）。"""
    global _PW
    if _PW is not None:
        return _PW
    with _PW_LOCK:
        if _PW is None:
            _PW = sync_playwright().start()
    return _PW


def _reclaim(entry: dict) -> None:
    """关闭并回收一个浏览器实例（幂等）。应在工作线程内调用。

    只关闭浏览器，不停止 Playwright 驱动（全局共享，由 shutdown_pool 统一释放）。
    """
    browser = entry.get("browser")
    if browser:
        try:
            browser.close()
        except Exception:
            pass


def _reap_idle(now: float) -> None:
    """惰性回收所有空闲超时的实例（须在工作线程内调用）。"""
    for key in list(_BROWSER_POOL.keys()):
        e = _BROWSER_POOL.get(key)
        if not e:
            continue
        if e.get("refs", 0) == 0 and now - e.get("last_used", 0) > POOL_IDLE_TIMEOUT:
            _BROWSER_POOL.pop(key, None)
            _reclaim(e)


def _new_pool_entry() -> dict:
    pw = _ensure_playwright()
    browser = pw.chromium.launch(headless=True, args=_COMMON_ARGS)
    return {
        "browser": browser,
        "refs": 0,
        "last_used": time.time(),
    }


def shutdown_pool() -> None:
    """进程退出时回收全部池中实例（须在工作线程内调用，否则跨线程 close 有风险）。"""
    with _POOL_LOCK:
        entries = list(_BROWSER_POOL.values())
        _BROWSER_POOL.clear()
    for e in entries:
        _reclaim(e)
    global _PW
    pw = _PW
    _PW = None
    if pw:
        try:
            pw.stop()
        except Exception:
            pass


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
    """从实例池获取 Chromium 并返回 (playwright, browser, context, page)。

    用法::

        with open_browser() as (p, browser, context, page):
            page.goto(url)
            ...

    退出 with 块时释放引用并归还池（不立即关闭浏览器）；实例空闲超时后由
    池惰性回收。池按登录态文件隔离，同一账号的短任务复用同一 Chromium，
    避免每次冷启动 launch（实例池按账号复用）。

    注意：本函数须在 core.executor 的单工作线程内调用（所有浏览器任务都经
    该线程执行）。sync Playwright 实例绑定创建线程，跨线程复用会崩溃。

    state_path 默认自愈寻找账号目录 data/state.json 或根目录 state.json（默认账号）。
    并发控制：进入时占用一个全局浏览器名额，超过上限会阻塞等待。
    """
    valid_state = Path(state_path) if state_path else get_valid_state_path()
    state_file = str(valid_state) if valid_state and valid_state.exists() else None
    key = state_file or "__no_state__"

    acquire_browser_slot()
    entry = None
    try:
        with _POOL_LOCK:
            _reap_idle(time.time())
            entry = _BROWSER_POOL.get(key)
            if entry and entry.get("browser") and entry["browser"].is_connected():
                entry["refs"] += 1
            else:
                if entry:  # 池中实例已失效，移除并重建
                    _BROWSER_POOL.pop(key, None)
                    _reclaim(entry)
                entry = _new_pool_entry()
                entry["refs"] = 1
                _BROWSER_POOL[key] = entry
        browser = entry["browser"]
        p = _ensure_playwright()

        defaults = {
            "viewport": {"width": 1366, "height": 768},
            "user_agent": _CHROME_UA,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "ignore_https_errors": True,
        }
        defaults.update(ctx_kwargs)

        context = browser.new_context(**defaults)
        if state_file:
            # 仅注入 cookies，不注入 storage_state 里的 localStorage。抖音 `word word` 占位页
            # 常因 localStorage 字段与 cookies 不匹配导致列表接口被拒；仅
            # cookies 注入（参考项目做法）可避免该问题。
            try:
                import json
                _state = json.loads(Path(state_file).read_text(encoding="utf-8"))
                _cookies = _state.get("cookies") or []
                if _cookies:
                    context.add_cookies(_cookies)
            except Exception:
                pass
        page = context.new_page()
        _apply_stealth(page)

        yield p, browser, context, page
    finally:
        with _POOL_LOCK:
            if entry and entry.get("refs", 0) > 0:
                entry["refs"] -= 1
                entry["last_used"] = time.time()
        release_browser_slot()
