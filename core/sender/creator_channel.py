"""通道 B：creator 页首条消息发送（默认禁用，高风险）。

适用场景：对无会话好友发首条消息（想建立火花但还没聊过）。
方案约束：
- 默认禁用：需 config.allow_first_message = true（UI 显式勾选）；
- 单日 ≤1 条、每次运行 ≤1 人（在 run_send 层强制）；
- 按昵称滚动匹配命中后点击，绝不盲发；
- 命中限流关键词立即停止本轮，不重试。
"""

from __future__ import annotations

import logging
import time

from ..browser import open_browser
from ..config import DEFAULT_ACCOUNT_ID, account_state_path, load_config
from ..guard import detect_rate_limit

logger = logging.getLogger("douyin-cloud-streak")

CREATOR_CHAT_URL = "https://creator.douyin.com/creator-micro/data/following/chat"

FRIENDS_TAB_CANDIDATES = [
    'xpath=//*[@id="sub-app"]/div/div/div[1]/div[2]',
    'xpath=//*[contains(text(),"好友")]',
]

FRIEND_ITEM_SELECTOR = 'xpath=//div[contains(@class, "semi-list-item-body")]'
NAME_SPAN_SELECTOR = 'xpath=.//span[contains(@class, "item-header-name-")]'
CHAT_INPUT_SELECTOR = "xpath=//div[contains(@class, 'chat-input-')]"

SCROLL_JS = """
() => {
    const els = document.querySelectorAll('[class*="semi-list"], #sub-app ul');
    let el = null;
    for (const e of els) { if (e.scrollHeight > e.clientHeight + 10) { el = e; break; } }
    if (!el) {
        const all = [...document.querySelectorAll('div')].filter(
            x => x.scrollHeight > x.clientHeight + 100 && x.clientHeight > 100
        );
        if (all.length) el = all[0];
    }
    if (el && el.scrollTop + el.clientHeight < el.scrollHeight - 10) {
        el.scrollTop += 600;
        return true;
    }
    return false;
}
"""


def send_first_message(
    entry: dict,
    msg_text: str,
    dry_run: bool = False,
    playwright: object | None = None,
    account_id: str | None = None,
) -> tuple[bool, str]:
    """通过 creator 页给无会话好友发送首条消息。

    playwright 参数：复用调用方已启动的 sync playwright 实例。
    返回 (是否成功, 说明)。
    """
    aid = account_id or DEFAULT_ACCOUNT_ID
    nickname = str(entry.get("nickname") or entry.get("display_name") or "").strip()
    if not nickname:
        return False, "台账缺少昵称/显示名，无法定位"
    cfg = load_config(aid)
    max_scrolls = int(cfg.get("creator_max_scrolls", 80) or 80)
    state_file = str(account_state_path(aid))

    try:
        # 复用调用方的 playwright 实例
        if playwright is not None:
            p = playwright
            owns_p = False
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                       "--disable-dev-shm-usage", "--disable-gpu"],
            )
            try:
                context = browser.new_context(
                    storage_state=state_file,
                    viewport={"width": 1366, "height": 900},
                    locale="zh-CN",
                )
                page = context.new_page()
                result = _do_send(page, context, nickname, msg_text, dry_run, max_scrolls)
                return result
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
        else:
            ctx_kwargs = {"viewport": {"width": 1366, "height": 900}, "locale": "zh-CN"}
            with open_browser(state_path=account_state_path(aid), **ctx_kwargs) as (p, browser, context, page):
                return _do_send(page, context, nickname, msg_text, dry_run, max_scrolls)
    except Exception as e:
        logger.error("通道 B 发送异常: %s", e)
        return False, f"通道 B 异常: {e}"


def _do_send(page, context, nickname: str, msg_text: str, dry_run: bool, max_scrolls: int) -> tuple[bool, str]:
    """实际发送逻辑：打开页面 → 定位好友 → 输入消息 → 发送。"""
    page.goto(CREATOR_CHAT_URL, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(10000)

    if "passport" in page.url.lower() or "login" in page.url.lower():
        return False, f"creator 页跳转到登录页（{page.url}）"
    if not any(c["name"].startswith("sessionid") for c in context.cookies()):
        return False, "creator 页未检测到 sessionid cookie，登录态可能已过期"

    # 点击「好友」tab
    for sel in FRIENDS_TAB_CANDIDATES:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=3000)
                break
        except Exception:
            continue
    page.wait_for_timeout(3000)

    # 滚动列表，按昵称匹配并点击
    found = False
    stagnant = 0
    last_names: set[str] = set()
    for _ in range(max_scrolls):
        if detect_rate_limit(page):
            return False, "检测到「操作频繁/验证码」提示，通道 B 停止本轮"

        items = page.locator(FRIEND_ITEM_SELECTOR).all()
        names_now: set[str] = set()
        for it in items:
            try:
                span = it.locator(NAME_SPAN_SELECTOR)
                if span.count() == 0:
                    continue
                name = span.inner_text().strip()
                names_now.add(name)
                if name == nickname:
                    it.click(timeout=5000)
                    found = True
                    break
            except Exception:
                continue
        if found:
            break

        grew = len(names_now) > len(last_names)
        last_names = names_now
        try:
            moved = bool(page.evaluate(SCROLL_JS))
        except Exception:
            moved = False
        page.wait_for_timeout(500)
        if not moved and not grew:
            stagnant += 1
            if stagnant >= 6:
                break
        else:
            stagnant = 0

    if not found:
        return False, f"好友列表未找到「{nickname}」（无会话好友可能不在列表中）"

    try:
        page.wait_for_selector(CHAT_INPUT_SELECTOR, timeout=15000)
    except Exception:
        return False, "未找到聊天输入框"

    if dry_run:
        return True, "dry-run（通道 B 定位成功）"

    if detect_rate_limit(page):
        return False, "发送前检测到「操作频繁/验证码」提示"

    input_box = page.locator(CHAT_INPUT_SELECTOR).first
    input_box.click()
    page.keyboard.type(msg_text, delay=100)
    page.keyboard.press("Enter")
    time.sleep(3)
    return True, "ok"
