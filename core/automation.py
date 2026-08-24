"""Playwright 自动化：在抖音网页版私信页面给指定好友发送消息。

发送逻辑参考 douyin-cloud-streak（MIT），要点：
- 点击联系人后校验右侧会话确实切换（防止限流时错发给上一个人）；
- 列表点击失败时用搜索框兜底；
- 检测"操作频繁 / 安全验证"等提示，命中即停本轮；
- 发送前清空输入框，发送后校验输入框已清空。
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime

from .browser import open_browser
from . import ledger
from .config import DEFAULT_ACCOUNT_ID, account_state_path, load_config
from .guard import detect_rate_limit
from .msg_builder import build_message
from .runtime import load_runtime, update_runtime
from .sender import creator_channel

logger = logging.getLogger("douyin-cloud-streak")

CHAT_URL = "https://www.douyin.com/chat"
LOGIN_TEXTS = ["扫码登录", "验证码登录", "登录后查看", "登录后即可"]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _screenshot(page, account_id: str | None = None) -> None:
    try:
        path = account_state_path(account_id).with_name("last_error.png")
        page.screenshot(path=str(path), timeout=5000)
        logger.info("已保存页面截图: %s", path)
    except Exception:
        pass


# ── 登录检测 ──────────────────────────────────────────────────────────────


def check_login(page) -> tuple[bool, str]:
    """返回 (是否已登录, 说明)。宁可误报掉线，也不要带着过期登录态硬跑。"""
    url = page.url
    if "login" in url.lower() or "passport" in url.lower():
        return False, f"页面已跳转到登录页（{url}）"

    try:
        qr = page.locator("#animate_qrcode_container")
        if qr.count() and qr.first.is_visible():
            return False, "页面出现扫码登录二维码，登录态已过期"
    except Exception:
        pass

    for text in LOGIN_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            for i in range(min(loc.count(), 3)):
                if loc.nth(i).is_visible():
                    return False, f"页面出现登录提示「{text}」"
        except Exception:
            continue

    cookies = page.context.cookies()
    if not any(c["name"].startswith("sessionid") for c in cookies):
        return False, "未检测到 sessionid Cookie"
    return True, "ok"


# ── 联系人定位 ────────────────────────────────────────────────────────────


def _find_contact(page, name: str):
    """优先按全文精确匹配联系人标题，避免误点其他会话里的消息预览。"""
    exact = page.get_by_text(name, exact=True)
    if exact.count():
        return exact.first
    return page.locator(".conversationConversationItemtitle").filter(has_text=name).first


def _verify_in_conversation(page, name: str) -> bool:
    """右侧会话顶部标题区域（x>300 且 y<100）出现目标昵称才算切换成功。"""
    for exact in (True, False):
        try:
            loc = page.get_by_text(name, exact=exact)
            for i in range(loc.count()):
                try:
                    box = loc.nth(i).bounding_box()
                except Exception:
                    continue
                if box and box.get("x", 0) > 300 and box.get("y", 0) < 100:
                    return True
        except Exception:
            continue
    return False


def _search_and_open(page, name: str) -> bool:
    """搜索兜底：好友不在聊天列表时，用搜索框找到并打开会话。"""
    try:
        box = page.get_by_placeholder("搜索", exact=False).first
        if box.count() == 0:
            return False
        box.click()
        box.fill(name)
        time.sleep(4)
        btn = page.get_by_text("发消息", exact=False).first
        if btn.count():
            btn.click(force=True)
            time.sleep(4)
            return True
        candidate = page.get_by_text(name, exact=True).first
        if candidate.count() == 0:
            candidate = page.get_by_text(name, exact=False).first
        if candidate.count() == 0:
            return False
        candidate.click(force=True)
        time.sleep(3)
        btn = page.get_by_text("发消息", exact=False).first
        if btn.count():
            btn.click(force=True)
            time.sleep(3)
        return True
    except Exception as e:
        logger.info("搜索打开 %s 失败: %s", name, e)
        return False


def _locate_contact(page, name: str) -> bool:
    """尝试点击联系人并校验切换成功，最多重试 5 次。"""
    for attempt in range(5):
        try:
            target = _find_contact(page, name)
            if target.count():
                target.click(force=True, timeout=10000)
                time.sleep(random.uniform(2, 4))
                if _verify_in_conversation(page, name):
                    return True
            else:
                try:
                    page.mouse.move(200, 350)
                    page.mouse.wheel(0, 600)
                except Exception:
                    pass
                time.sleep(1.5)
        except Exception as e:
            logger.info("点击联系人 %s 异常: %s", name, str(e)[:100])
        time.sleep(random.uniform(1, 2))

    if _search_and_open(page, name):
        time.sleep(random.uniform(1, 3))
        return _verify_in_conversation(page, name)
    return False


# ── 消息输入与发送 ────────────────────────────────────────────────────────


def _type_and_send(page, input_box, msg_text: str) -> bool:
    """把文字输入输入框并按 Enter 发送，返回文字是否成功进入输入框。"""
    try:
        input_box.click()
        time.sleep(0.4)
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        time.sleep(0.3)
        page.keyboard.type(msg_text, delay=100)
        time.sleep(0.8)
        cur = input_box.inner_text() or ""
        if msg_text not in cur:
            logger.warning("文字未进入输入框，当前内容: %r", cur[:30])
            return False
        page.keyboard.press("Enter")
        return True
    except Exception as e:
        logger.info("输入/发送异常: %s", str(e)[:100])
        return False


def _wait_input_cleared(input_box, msg_text: str, wait: float = 8) -> bool:
    """消息发出后输入框应不再包含发送文字，以此确认真正发出。"""
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(1)
        try:
            cur = input_box.inner_text() or ""
            if msg_text not in cur:
                return True
        except Exception:
            pass
    return False


def _send_message(page, msg_text: str, dry_run: bool) -> tuple[bool, str]:
    """在当前会话中输入并发送消息，返回 (是否成功, 说明)。"""
    if detect_rate_limit(page):
        return False, "发送前检测到验证提示"

    input_box = page.locator('div[contenteditable="true"]').first
    try:
        if input_box.count() == 0 or input_box.bounding_box() is None:
            return False, "找不到聊天输入框"
        input_box.wait_for(state="visible", timeout=8000)
    except Exception:
        return False, "找不到聊天输入框"

    if dry_run:
        return True, "dry-run"

    if not _type_and_send(page, input_box, msg_text):
        return False, "文字未能输入到输入框"

    if _wait_input_cleared(input_box, msg_text, wait=8):
        return True, "ok"

    # 重试一次
    logger.warning("未检测到消息发出，重试一次")
    if detect_rate_limit(page):
        return False, "重试时检测到验证提示"
    if not _type_and_send(page, input_box, msg_text):
        return False, "重试时文字未能输入"
    if _wait_input_cleared(input_box, msg_text, wait=8):
        return True, "ok"
    return False, "发送后输入框未清空，消息可能未发出"


def send_to_contact(page, name: str, msg_text: str, dry_run: bool) -> tuple[bool, str]:
    """完整流程：定位好友 → 校验切换 → 发送消息。"""
    if not _locate_contact(page, name):
        return False, "未能切换到该好友会话（名字不在聊天列表，或页面结构变化）"
    if detect_rate_limit(page):
        return False, "检测到「操作频繁 / 安全验证」提示"
    return _send_message(page, msg_text, dry_run)


# ── 联系人同步 ────────────────────────────────────────────────────────────

_EXTRACT_JS = """
    () => {
        const out = [];
        const seen = new Set();

        // 每个会话一行：conversationConversationItemwrapper
        const rows = document.querySelectorAll('[class*="conversationConversationItemwrapper"]');

        // 名字是标题节点的直接文本；火花/时间等标签可能嵌在其中，需剔除
        const cleanName = (el) => {
            let direct = "";
            el.childNodes.forEach(n => { if (n.nodeType === 3) direct += n.textContent; });
            let name = direct.trim();
            if (!name) {
                const clone = el.cloneNode(true);
                clone.querySelectorAll(
                    '[class*="TagNextToTitle"], [class*="timeStr"], [class*="streak"], [class*="Streak"]'
                ).forEach(x => x.remove());
                name = (clone.textContent || "").trim();
            }
            return name.replace(/\\s+/g, " ").trim();
        };

        rows.forEach(row => {
            const rect = row.getBoundingClientRect();
            if (rect.height < 30 || rect.width < 100) return;

            // 精确类名优先；未命中时在标题容器内继续找，最后整行清洗兜底
            let finalName = "";
            let titleEl = row.querySelector('.conversationConversationItemtitle');
            const wrap = titleEl ? null : row.querySelector('[class*="Itemtitle"]');
            if (!titleEl && wrap) titleEl = wrap.querySelector('.conversationConversationItemtitle');

            if (titleEl) {
                finalName = cleanName(titleEl);
            } else if (wrap) {
                const c2 = wrap.cloneNode(true);
                c2.querySelectorAll(
                    '[class*="TagNextToTitle"], [class*="timeStr"], [class*="streak"], [class*="Streak"], [class*="badge"]'
                ).forEach(x => x.remove());
                finalName = (c2.textContent || "").replace(/\\s+/g, " ").trim();
            }

            if (!finalName) return;
            if (/^\\d+$/.test(finalName)) return;          // 纯数字：未读数/火花天数误当昵称
            if (/^\\d{1,2}:\\d{2}$/.test(finalName)) return; // 时间
            if (finalName === '消息' || finalName === '私信' || finalName === '朋友私信' || finalName === '通知') return;
            if (finalName.length > 40) return;
            if (seen.has(finalName)) return;
            seen.add(finalName);

            // 火花天数：行内 commonStreak 容器（normalText 为数字）
            let streak = "";
            const st = row.querySelector('[class*="commonStreaknormalText"], [class*="commonStreakstreakContainer"]');
            if (st) streak = (st.textContent || "").trim();

            out.push({ name: finalName, streak: streak });
        });
        return out;
    }
"""


def _open_chat_page(page) -> bool:
    """打开抖音私信页并等待加载，返回是否成功。"""
    for attempt in range(3):
        try:
            page.goto(CHAT_URL, timeout=90000, wait_until="domcontentloaded")
            return True
        except Exception as e:
            logger.info("打开页面失败（第 %s 次）: %s", attempt + 1, str(e)[:80])
            time.sleep(5)
    return False


def _scroll_and_extract(page, collected: list[dict], max_rounds: int = 28) -> None:
    """滚动聊天列表并提取联系人，直到没有新数据。

    列表为虚拟滚动，步长过大会跳过部分行导致火花遗漏，故小步慢滚。
    """
    for _ in range(max_rounds):
        data = page.evaluate(_EXTRACT_JS) or []
        new_items = [x for x in data if x not in collected]
        if new_items:
            collected.extend(new_items)
            stable = 0
        else:
            stable = getattr(_scroll_and_extract, "_stable", 0) + 1
            _scroll_and_extract._stable = stable
            if stable >= 3:
                break
        try:
            page.mouse.move(200, 350)
            page.mouse.wheel(0, 450)
        except Exception:
            pass
        page.wait_for_timeout(1000)


def fetch_chat_contacts(account_id: str | None = None) -> dict:
    """从抖音私信页左侧聊天列表读取联系人（含火花天数），供网页端勾选。"""
    aid = account_id or DEFAULT_ACCOUNT_ID
    result = {"at": _now(), "names": [], "error": None}
    state = account_state_path(aid)
    if not state.exists():
        result["error"] = "该账号尚未上传登录态 state.json"
        return result

    try:
        with open_browser(state_path=state) as (p, browser, context, page):
            if not _open_chat_page(page):
                result["error"] = "无法打开抖音私信页面"
                return result

            page.wait_for_timeout(10000)
            logged, why = check_login(page)
            if not logged:
                result["error"] = why
                return result

            collected: list[dict] = []
            for attempt in range(3):
                try:
                    page.wait_for_selector(".conversationConversationItemtitle", timeout=45000)
                except Exception:
                    logger.info("第 %s 次等待联系人列表超时", attempt + 1)

                _scroll_and_extract._stable = 0
                _scroll_and_extract(page, collected)

                if collected:
                    break
                try:
                    page.reload(wait_until="domcontentloaded", timeout=90000)
                    page.wait_for_timeout(12000)
                except Exception:
                    pass

            result["names"] = collected
            logger.info("已读取聊天列表联系人 %s 个", len(result["names"]))
    except Exception as e:
        logger.error("获取联系人异常: %s", e)
        result["error"] = f"获取联系人异常: {e}"
    return result


# ── 通道选择 ───────────────────────────────────────────────────────────────


def _b_channel_daily(account_id: str | None = None) -> tuple[str, int]:
    """通道 B 今日已发条数：优先 runtime 计数，跨天自动归零。"""
    today = datetime.now().astimezone().date().isoformat()
    rec = load_runtime(account_id).get("b_channel_daily") or {}
    if rec.get("date") != today:
        return today, 0
    return today, int(rec.get("count", 0) or 0)


def compute_pending(cfg: dict | None = None, account_id: str | None = None) -> list[dict]:
    """预测本次运行会真实发送的名单（与 run_send 通道判定一致）。"""
    cfg = cfg or load_config(account_id)
    entries = ledger.get_selected(account_id)
    daily_limit = max(1, int(cfg.get("first_message_daily_limit", 1) or 1))
    _, creator_sent_today = _b_channel_daily(account_id)
    allow_first = bool(cfg.get("allow_first_message"))
    pending: list[dict] = []
    for e in entries:
        if e.get("has_conversation"):
            pending.append({**e, "send_channel": "consumer"})
        elif allow_first and creator_sent_today < daily_limit:
            pending.append({**e, "send_channel": "creator"})
    return pending


# ── 主发送流程 ─────────────────────────────────────────────────────────────


def _send_consumer(page, entry: dict, msg: str, dry_run: bool, result: dict, account_id: str | None = None) -> None:
    """通道 A：consumer 重防护发送。"""
    name = entry["display_name"]
    ok, why = send_to_contact(page, name, msg, dry_run)
    if ok:
        result["ok"].append(name)
        ledger.confirm_join(name, account_id)
        logger.info("[%s] 已发送给 %s：%s", account_id, name, msg if not dry_run else "(干跑)")
    else:
        if entry.get("channel") == "creator":
            result["skipped"].append({
                "name": name,
                "reason": "consumer 定位失败（该好友为 creator-only），已降级跳过",
            })
            ledger.mark_no_consumer_conversation(name, account_id)
        else:
            result["failed"].append({"name": name, "reason": why})
            logger.warning("[%s] 发送给 %s 失败：%s", account_id, name, why)
            if detect_rate_limit(page):
                result["rate_limited"] = True
                logger.warning("[%s] 疑似触发限流，停止本轮", account_id)
    if not dry_run:
        ledger.update_send_result(name, ok, _now(), account_id=account_id)


def _send_creator(entry: dict, msg: str, dry_run: bool, result: dict, p, account_id: str | None = None) -> None:
    """通道 B：creator 首条消息。"""
    name = entry["display_name"]
    cfg = load_config(account_id)
    allow_first = bool(cfg.get("allow_first_message"))
    daily_limit = max(1, int(cfg.get("first_message_daily_limit", 1) or 1))
    today, count = _b_channel_daily(account_id)

    if not allow_first:
        result["skipped"].append({"name": name, "reason": "无会话且未开启「允许首条消息」"})
        return
    if count >= daily_limit:
        result["skipped"].append({
            "name": name,
            "reason": f"今日已发送首条消息 {count}/{daily_limit}",
        })
        return

    ok, why = creator_channel.send_first_message(entry, msg, dry_run, p, account_id)
    if ok:
        result["ok"].append(name)
        logger.info("[%s] 通道 B 已发送给 %s：%s", account_id, name, msg if not dry_run else "(干跑)")
        if not dry_run:
            ledger.update_send_result(name, True, _now(), via_creator=True, account_id=account_id)
            update_runtime(account_id, b_channel_daily={"date": today, "count": count + 1})
    else:
        result["failed"].append({"name": name, "reason": f"通道B: {why}"})
        logger.warning("[%s] 通道 B 发送给 %s 失败：%s", account_id, name, why)
        if "限流" in why or "停止" in why:
            result["rate_limited"] = True
            logger.warning("[%s] 通道 B 触发限流，停止本轮", account_id)


def run_send(dry_run: bool = False, only_names: list[str] | None = None, account_id: str | None = None) -> dict:
    """主入口：从好友台账读取勾选目标，逐个发送。"""
    aid = account_id or DEFAULT_ACCOUNT_ID
    cfg = load_config(aid)
    messages = cfg.get("messages") or ["🔥"]
    max_n = int(cfg.get("max_friends_per_run", 20) or 20)
    gap_min = max(1, int(cfg.get("send_gap_min", 6) or 6))
    gap_max = max(gap_min, int(cfg.get("send_gap_max", 12) or 12))

    result = {
        "at": _now(), "dry_run": bool(dry_run),
        "ok": [], "failed": [], "skipped": [],
        "logged_out": False, "rate_limited": False,
        "account_id": aid,
    }

    state = account_state_path(aid)
    if not state.exists():
        result["failed"].append({"name": "_system", "reason": "该账号尚未上传登录态 state.json"})
        return result

    targets = ledger.get_selected(aid)
    if not targets and cfg.get("friends"):
        stats = ledger.import_config_friends(cfg["friends"], aid)
        logger.info("[%s] 已从 config.friends 迁移进台账：新增 %s 人，勾选 %s 人",
                     aid, stats["added"], stats["selected"])
        targets = ledger.get_selected(aid)
    if only_names is not None:
        targets = [t for t in targets if t.get("display_name") in only_names]
    targets = targets[:max_n] if max_n > 0 else targets

    if not targets:
        logger.info("[%s] 未配置任何好友，跳过发送", aid)
        return result

    try:
        with open_browser(state_path=state) as (p, browser, context, page):
            if not _open_chat_page(page):
                result["failed"].append({"name": "_system", "reason": "无法打开抖音私信页面"})
                return result

            time.sleep(8)
            logged, why = check_login(page)
            if not logged:
                result["logged_out"] = True
                result["failed"].append({"name": "_system", "reason": why})
                _screenshot(page, aid)
                return result

            logger.info("[%s] 待发送好友 %s 人，dry_run=%s", aid, len(targets), dry_run)
            for entry in targets:
                msg = build_message(messages, last_sent_msg=str(entry.get("last_msg", "")))
                if entry.get("has_conversation"):
                    _send_consumer(page, entry, msg, dry_run, result, aid)
                else:
                    _send_creator(entry, msg, dry_run, result, p, aid)

                if result["rate_limited"]:
                    break
                time.sleep(random.uniform(gap_min, gap_max))
    except Exception as e:
        logger.error("[%s] 运行异常: %s", aid, e)
        result["failed"].append({"name": "_system", "reason": f"运行异常: {e}"})
    return result


# 别名兼容
sync_contacts = fetch_chat_contacts
