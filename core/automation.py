"""Playwright 自动化：在抖音网页版私信页面给指定好友发送消息。

发送逻辑参考 douyin-cloud-streak（MIT），要点：
- 点击联系人后校验右侧会话确实切换（防止限流时错发给上一个人）；
- 列表点击失败时用搜索框兜底；
- 检测"操作频繁 / 安全验证"等提示，命中即停本轮；
- 发送前清空输入框，发送后校验输入框已清空。
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from datetime import datetime
from urllib.parse import urlparse

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


def _norm_name(s) -> str:
    """昵称归一化：NBSP/零宽字符剔除 + trim，用于精确比对防止错发。"""
    return str(s or "").replace("\u00a0", " ").replace("\u200b", "").strip()


# 火花天数字段名候选（接口层）：抖音后端字段名比前端混淆类名稳定得多，
# 私信左侧列表的火花数字即由 im/user/info 等接口渲染，直接读接口字段最通用。
_STREAK_KEY_HINTS = ("streak", "keep_fire", "keepfire", "spark", "fire", "interact", "continuous")
_streak_logged_keys: set[str] = set()


def _probe_streak(item) -> int:
    """从 im/user/info 会话好友项中启发式探测火花天数字段（跨版本通用）。

    策略：字段名含火花语义关键词（streak/fire/spark/interact/continuous/keep），
    且值为 0~9999 的整数（或可解析字符串）即视为火花天数。
    返回 0 表示未识别或确无火花（0 同时代表无火花，语义一致）。
    首次命中某字段名时打印日志，便于开源用户反馈/校准。
    """
    if not isinstance(item, dict):
        return 0
    for k, v in item.items():
        kl = str(k).lower()
        if not any(h in kl for h in _STREAK_KEY_HINTS):
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and 0 <= int(v) <= 9999:
            if k not in _streak_logged_keys:
                _streak_logged_keys.add(k)
                logger.info("接口探测到火花字段 %s=%s", k, v)
            return int(v)
        if isinstance(v, str):
            m = re.search(r"\d{1,4}", v)
            if m and 0 <= int(m.group()) <= 9999:
                if k not in _streak_logged_keys:
                    _streak_logged_keys.add(k)
                    logger.info("接口探测到火花字段 %s=%r", k, v)
                return int(m.group())
    return 0


# 会话页火花文案（进入会话后顶部提示，如 "已连续 81 天，一起续火花吧"）。
# 用"连续/持续"前缀限定，避免误匹配左侧列表行的 "🔥N"/"N天" 简短显示。
_STREAK_PAGE_JS = """
() => {
    const t = (document.body ? document.body.innerText || "" : "").replace(/\\s+/g, " ");
    const pats = [
        /已连续\\s*(\\d{1,4})\\s*天/,
        /连续\\s*(\\d{1,4})\\s*天/,
        /持续\\s*(\\d{1,4})\\s*天/,
    ];
    for (const p of pats) {
        const m = t.match(p);
        if (m) return m[1];
    }
    return "";
}
"""


def _probe_streak_from_page(page) -> int:
    """从已打开的会话详情页提取当前好友的真实火花天数（发送校准兜底）。"""
    try:
        res = page.evaluate(_STREAK_PAGE_JS)
        if res and re.fullmatch(r"\d{1,4}", str(res)):
            days = int(res)
            if 1 <= days <= 9999:
                logger.info("会话页校准火花天数: %s", days)
                return days
    except Exception:
        pass
    return 0


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

    try:
        cookies = page.context.cookies()
        if not any(str(c.get("name", "")).startswith("sessionid") for c in cookies):
            return False, "未检测到 sessionid Cookie"
    except Exception as e:
        return False, f"读取登录 Cookie 失败: {e}"
    return True, "ok"


# ── 联系人定位 ────────────────────────────────────────────────────────────


_TITLE_SELECTOR = '.conversationConversationItemtitle, [class*="Itemtitle"] .conversationConversationItemtitle'


def _list_title_matches(page, name: str) -> int:
    """统计左侧聊天列表中标题与 name 完全相等的会话行数。

    同名会话 >1 时无法唯一定位，继续发送必然错发给其中一人，
    必须在发送前拦截。只统计 x<400 的左侧区域，排除右侧会话头/消息气泡干扰。
    """
    target = _norm_name(name)
    count = 0
    try:
        titles = page.locator(_TITLE_SELECTOR)
        n = titles.count()
        for i in range(min(n, 200)):
            el = titles.nth(i)
            try:
                if _norm_name(el.inner_text()) != target:
                    continue
                box = el.bounding_box()
            except Exception:
                continue
            if box and box.get("x", 9999) < 400:
                count += 1
    except Exception:
        pass
    return count


def _find_contact(page, name: str):
    """优先按全文精确匹配联系人标题，避免误点其他会话里的消息预览。"""
    exact = page.get_by_text(name, exact=True)
    if exact.count():
        return exact.first
    return page.locator(".conversationConversationItemtitle").filter(has_text=name).first


def _verify_in_conversation(page, name: str) -> bool:
    """右侧会话顶部标题区域（x>300 且 y<100）出现与目标全等的昵称才算切换成功。

    必须全等比较：子串匹配会把「小美」误验证到「小美酱」的会话上，导致错发。
    """
    target = _norm_name(name)
    for exact in (True, False):
        try:
            loc = page.get_by_text(name, exact=exact)
            for i in range(loc.count()):
                try:
                    el = loc.nth(i)
                    box = el.bounding_box()
                    text = _norm_name(el.inner_text())
                except Exception:
                    continue
                if (
                    box and box.get("x", 0) > 300 and box.get("y", 0) < 100
                    and text == target
                ):
                    return True
        except Exception:
            continue
    return False


def _search_and_open(page, name: str) -> bool:
    """搜索兜底：好友不在聊天列表时，用搜索框找到并打开会话。"""
    try:
        # 只认左侧联系人面板内的搜索框（x<400）：顶部全局搜索框 fill 后会
        # 跳离 /chat 页，导致本轮剩余好友全部在错误页面上定位失败。
        cands = page.get_by_placeholder("搜索", exact=False)
        box = None
        for i in range(min(cands.count(), 5)):
            el = cands.nth(i)
            b = el.bounding_box()
            if b and b.get("x", 9999) < 400:
                box = el
                break
        if box is None:
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
            # 过滤掉包含“搜”的节点，防止点击到全局搜索大按钮导致跳出私信页
            cands = page.get_by_text(name, exact=False)
            candidate = None
            for i in range(cands.count()):
                el = cands.nth(i)
                text = el.inner_text() or ""
                if "搜" not in text:
                    candidate = el
                    break
            if candidate is None:
                candidate = cands.first
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


def _quick_logged_out(page) -> bool:
    """轻量登录态检测：二维码弹窗或登录提示文本出现即视为掉线。

    供定位/发送循环高频调用，不做 Cookie 校验（毫秒级返回）。
    登录态中途失效时页面会骨架化列表并弹扫码弹窗，继续重试只会产出
    "Element is not visible" 空转与假成功，必须尽早短路。
    """
    try:
        qr = page.locator("#animate_qrcode_container")
        if qr.count() and qr.first.is_visible():
            return True
    except Exception:
        pass
    for text in LOGIN_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            for i in range(min(loc.count(), 3)):
                if loc.nth(i).is_visible():
                    return True
        except Exception:
            continue
    return False


def _locate_contact(page, name: str) -> tuple[bool, str]:
    """尝试点击联系人并校验切换成功，结合 JS 强力滚动遍历列表。返回(是否成功, 原因)。"""
    dup = _list_title_matches(page, name)
    if dup > 1:
        return False, f"聊天列表存在 {dup} 个同名会话「{name}」，为避免错发已跳过，请先在抖音内备注区分。"

    # 找人前强制将虚拟列表滚回顶部，避免错过上方的好友
    try:
        reset_js = _SCROLL_LIST_JS.replace("el.scrollTop = before + step;", "el.scrollTop = 0;")
        page.evaluate(reset_js, 0)
        time.sleep(0.5)
    except Exception:
        pass

    for attempt in range(80):
        if attempt % 5 == 0 and _quick_logged_out(page):
            return False, "登录态已过期（页面出现扫码登录弹窗），请重新扫码后再发送"
        try:
            target = _find_contact(page, name)
            if target.count():
                target.click(force=True, timeout=10000)
                time.sleep(random.uniform(2, 4))
                if _verify_in_conversation(page, name):
                    return True, "ok"
            else:
                moved = False
                try:
                    r = page.evaluate(_SCROLL_LIST_JS, 450) or {}
                    moved = bool(r.get("moved"))
                except Exception:
                    pass
                if not moved:
                    try:
                        page.mouse.move(200, 350)
                        page.mouse.wheel(0, 450)
                    except Exception:
                        pass
                time.sleep(0.5)
        except Exception as e:
            logger.info("点击联系人 %s 异常: %s", name, str(e)[:100])
        time.sleep(random.uniform(0.5, 1))

    if _search_and_open(page, name):
        time.sleep(random.uniform(1, 3))
        if _verify_in_conversation(page, name):
            return True, "ok"
    return False, "未能切换到该好友会话（名字不在聊天列表，或页面结构变化）"


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


class MessageSendTracker:
    """监听抖音私信底层的 /v1/message/send 响应包，提取官方投递收据与精准状态码。"""

    # 只认"发送消息"接口本身：path 锚定结尾，避免 imapi 域名下 conv/sync/ack 等
    # 长轮询响应被误当成本次发送回执（误收据会覆盖真实拦截结论 → 假成功）。
    _SEND_PATH_RE = re.compile(r"(?:v1/message/send|imapi\d*/(?:message/send|send_message))/?$")

    def __init__(self, page):
        self.page = page
        self.last_receipt: dict | None = None
        self._armed_at = 0.0
        self._handler = self._on_response
        try:
            self.page.on("response", self._handler)
        except Exception:
            pass

    def _on_response(self, resp):
        try:
            path = urlparse(resp.url or "").path
            if not self._SEND_PATH_RE.search(path):
                return
            if resp.status != 200:
                return
            data = resp.json()
            if not isinstance(data, dict):
                return
            status_code = data.get("status_code")
            # 官方状态码缺失的响应一律丢弃：默认 0 会把无关响应翻译成"发送成功"
            if status_code is None:
                return
            nested = data.get("data") or {}
            if not isinstance(nested, dict):
                nested = {}
            msg_id = nested.get("server_message_id") or nested.get("message_id")
            status_msg = str(data.get("status_msg") or data.get("message") or "ok")
            self.last_receipt = {
                "status_code": status_code,
                "server_message_id": str(msg_id) if msg_id else "",
                "status_msg": status_msg,
                "time": time.time(),
            }
        except Exception:
            pass

    def reset(self):
        """进入新一轮尝试前调用：清空旧收据，并记录武装时间。

        只接受武装时间之后的收据——上一轮 attempt 的迟到回包不会污染本轮判定。
        """
        self.last_receipt = None
        self._armed_at = time.time()

    def pop_recent(self, within_seconds: float = 8.0) -> dict | None:
        rec = self.last_receipt
        if (
            rec
            and rec["time"] >= self._armed_at
            and (time.time() - rec["time"]) <= within_seconds
        ):
            self.last_receipt = None
            return rec
        return None

    def close(self):
        try:
            self.page.remove_listener("response", self._handler)
        except Exception:
            pass


def _receipt_verdict(receipt: dict | None) -> tuple[bool, str] | None:
    """把网络收据翻译成 (成功?, 原因)；receipt 为空返回 None 表示无收据。"""
    if receipt is None:
        return None
    code = receipt.get("status_code", 0)
    if code == 0:
        msg_id = receipt.get("server_message_id")
        return True, f"ok (msg_id: {msg_id})" if msg_id else "ok"
    if code == 1:
        return False, "发送失败：对方不在单聊会话中或已被解散"
    if code == 3:
        return False, f"发送失败：文案触发抖音安全审核拦截 ({receipt.get('status_msg')})"
    if code == 5:
        return False, "发送失败：已被对方拉黑或对方已注销"
    return False, f"发送失败：服务端返回错误 {code} ({receipt.get('status_msg')})"


def _count_msg_bubble(page, msg_text: str, input_top: float) -> int:
    """统计右侧会话消息区内与 msg_text 全等的可见元素数。

    位置过滤排除两类干扰：左侧列表预览（x<300）与底部输入框（y>=input_top）。
    用于发送前后对比：数量增加 = 消息气泡真的出现在会话里 = 端到端已发出。
    """
    n = 0
    try:
        loc = page.get_by_text(msg_text, exact=True)
        for i in range(min(loc.count(), 10)):
            try:
                box = loc.nth(i).bounding_box()
            except Exception:
                continue
            if box and box.get("x", 0) > 300 and 60 < box.get("y", 0) < input_top - 6:
                n += 1
    except Exception:
        pass
    return n


def _settle_send(input_box, tracker: MessageSendTracker | None, msg_text: str,
                 bubble_before: int = 0, wait: float = 8, grace: float = 3.0,
                 page=None) -> tuple[bool, str]:
    """综合网络收据、输入框状态与消息区新气泡给出最终判定。

    - 有收据：以收据 status_code 为准（服务端权威结论）；
    - 无收据：回退弱判定——输入框已清空时，若传入 page 则必须在会话消息区
      核实到"发送前没有、发送后出现"的消息气泡，才算端到端发出。
      登录态被服务端作废时页面不弹二维码，所有发送 API 静默失败，
      输入框却会因 DOM 重排被清空——历史上仅凭"输入框清空"曾连续
      两轮产出全部假成功，本核验即为最终防线。
    """
    cleared = _wait_input_cleared(input_box, msg_text, wait=wait)
    deadline = time.time() + (grace if cleared else 0)
    while True:
        if tracker:
            v = _receipt_verdict(tracker.pop_recent(within_seconds=15))
            if v is not None:
                return v
        if time.time() >= deadline:
            break
        time.sleep(0.5)
    if cleared:
        if page is None:
            # 单元测试/无页面环境：退化为输入框清空弱判定
            return True, "ok(弱判定:无服务端回执)"
        input_top = 9999.0
        try:
            b = input_box.bounding_box()
            if b:
                input_top = float(b.get("y", 9999))
        except Exception:
            pass
        bubble_after = _count_msg_bubble(page, msg_text, input_top)
        if bubble_after > bubble_before:
            logger.info("未捕获服务端回执，但消息区已核实到新增气泡（%s→%s），判定发送成功",
                        bubble_before, bubble_after)
            return True, "ok(无回执,已核实新气泡)"
        logger.warning(
            "发送未捕获服务端回执，且消息区未出现新气泡（发送前 %s / 发送后 %s），判定为未发出"
            "——常见原因：登录态已被服务端作废（页面未弹二维码），请重新扫码",
            bubble_before, bubble_after,
        )
        return False, "无服务端回执且会话中未核实到新消息，判定未发出（请重新扫码登录后重试）"
    return False, "发送后输入框未清空，消息可能未发出"


def _send_message(page, msg_text: str, dry_run: bool, tracker: MessageSendTracker | None = None) -> tuple[bool, str]:
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

    last_why = "未知原因"
    for attempt in (1, 2):
        if tracker:
            tracker.reset()
        # 记录发送前消息区已有气泡数，用于发送后对比核实新气泡（端到端验证）
        input_top = 9999.0
        try:
            b = input_box.bounding_box()
            if b:
                input_top = float(b.get("y", 9999))
        except Exception:
            pass
        bubble_before = _count_msg_bubble(page, msg_text, input_top)
        if not _type_and_send(page, input_box, msg_text):
            last_why = "文字未能输入到输入框"
            if attempt == 1:
                logger.warning("第 %s 次发送失败：%s，重试一次", attempt, last_why)
                continue
            return False, last_why

        ok, why = _settle_send(input_box, tracker, msg_text, bubble_before=bubble_before, page=page)
        if ok:
            return True, why
        last_why = why
        logger.warning("第 %s 次发送未确认成功：%s", attempt, why)
        if detect_rate_limit(page):
            return False, "重试时检测到验证提示"
        time.sleep(random.uniform(1.5, 3))
    return False, last_why


def send_to_contact(page, name: str, msg_text: str, dry_run: bool, tracker: MessageSendTracker | None = None) -> tuple[bool, str]:
    """完整流程：定位好友 → 校验切换 → 发送消息。"""
    _dismiss_dialogs(page)
    ok, why = _locate_contact(page, name)
    if not ok:
        return False, why
    if detect_rate_limit(page):
        return False, "检测到「操作频繁 / 安全验证」提示"
    return _send_message(page, msg_text, dry_run, tracker=tracker)


# ── 联系人同步 ────────────────────────────────────────────────────────────

_EXTRACT_JS = r"""
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
                    '[class*="TagNextToTitle"], [class*="timeStr"], [class*="streak"], [class*="Streak"], [class*="badge"]'
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

            // 火花天数：混淆类名会随抖音前端重新构建而变化，采用多级兜底：
            // ① 精确类名 → ② 模糊 treak 类名 → ③ 遍历 className 正则 /streak/i
            // → ④ 整行文本 "N天" 模式（负向断言排除"3天前"这类相对时间标签）
            let streak = "";
            const pickDigits = (el) => {
                if (!el) return "";
                const m = (el.textContent || "").match(/(\\d{1,4})\\s*天/);
                return m ? m[1] : "";
            };
            let st = row.querySelector('[class*="commonStreaknormalText"]');
            if (!st) st = row.querySelector('[class*="treak"]');
            if (!st) {
                for (const el of row.querySelectorAll('[class]')) {
                    const cn = typeof el.className === 'string' ? el.className : '';
                    if (/streak/i.test(cn)) { st = el; break; }
                }
            }
            streak = pickDigits(st);
            if (!streak) {
                const m = (row.textContent || "").match(/(\\d{1,4})\\s*天(?!前)/);
                if (m) streak = m[1];
            }
            if (!streak) {
                // ⑤ 火焰 emoji 紧邻数字（新版 DOM 常显示为 "🔥 N天"/"🔥N"）
                const m = (row.textContent || "").match(/[🔥⚡]\\s*(\\d{1,4})(?:\\s*天)?(?!\\d)/);
                if (m) streak = m[1];
            }
            if (!streak) {
                // ⑥ 火焰 SVG 图标就近取数（新版 DOM 常用内联 svg 火焰，数字跟在其后）
                const fire = row.querySelector(
                    'svg[class*="fire"], svg[class*="flame"], [class*="fire"] svg, [class*="flame"] svg'
                );
                if (fire) {
                    let node = fire.parentElement;
                    for (let k = 0; node && k < 3; k++, node = node.parentElement) {
                        const m = (node.textContent || "").match(/(\d{1,4})\s*天/);
                        if (m) { streak = m[1]; break; }
                    }
                    // ⑦ 火焰容器内纯数字（"81" / "81天"，无其他文字），防抓未读数
                    if (!streak) {
                        node = fire.parentElement;
                        for (let k = 0; node && k < 3; k++, node = node.parentElement) {
                            const txt = (node.textContent || "").replace(/\s+/g, "");
                            const m = txt.match(/^(\d{1,4})(?:天)?$/);
                            if (m) { streak = m[1]; break; }
                        }
                    }
                }
            }

            // 头像：行内第一个非火花图标的 img（头像无 class，位于行首 50x50）
            let avatar = "";
            const avImg = row.querySelector('img');
            if (avImg) {
                let asrc = avImg.getAttribute('src') || avImg.src || "";
                if (asrc.startsWith('//')) asrc = 'https:' + asrc;
                if (!asrc.includes('flame_icon')) avatar = asrc;
            }

            out.push({ name: finalName, streak: streak, avatar: avatar });
        });

        // 触底检测与滚动高度记录
        let atBottom = false;
        let scrollTop = 0;
        try {
            const scroller = document.querySelector(
                '.conversationConversationListwrapper, [class*="conversationList"], [class*="chatList"], [class*="ContactList"], [class*="contactList"]'
            );
            const el = scroller && scroller.scrollHeight > scroller.clientHeight ? scroller : document.scrollingElement;
            if (el) {
                scrollTop = el.scrollTop || 0;
                atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 8;
            } else {
                atBottom = true;
            }
        } catch (e) {}

        return { items: out, atBottom: atBottom, scrollTop: scrollTop };
    }
"""

# 只保留语义明确无害的文案。绝不盲点"确定/确认/关闭"——这些按钮同样出现在
# 退出登录、删除会话、清空记录等危险确认框上，误点轻则掉线中断整轮，重则误删数据。
# 关不掉弹窗只是本轮采集失败（fail-safe）；误点危险按钮是 fail-deadly。
_DISMISS_TEXTS = ["我知道了", "知道了", "稍后再说", "不再提示"]
_DISMISS_SELECTORS = [
    ".semi-modal-close",
    'button[aria-label="Close"]',
    'button[aria-label="关闭"]',
    '[class*="close-icon"]',
    '[class*="modalClose"]',
    '[class*="dialog-close"]',
]


def _dismiss_dialogs(page) -> bool:
    """主动检测并点消全屏通知/协议/提示弹窗，防止遮挡私信列表。"""
    dismissed = False
    for text in _DISMISS_TEXTS:
        try:
            loc = page.get_by_text(text, exact=True)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=1500)
                dismissed = True
                page.wait_for_timeout(500)
        except Exception:
            pass
    for sel in _DISMISS_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=1500)
                dismissed = True
                page.wait_for_timeout(500)
        except Exception:
            pass
    return dismissed


def _open_chat_page(page) -> bool:
    """打开抖音私信页并等待加载，返回是否成功。

    导航失败重试 3 次、单次超时放宽，
    并等会话列表容器出现（条件等待替代固定 sleep），降低网络抖动导致的"同步失败"。
    """
    for attempt in range(3):
        try:
            page.goto(CHAT_URL, timeout=60000, wait_until="domcontentloaded")
            _dismiss_dialogs(page)
            # 等会话列表容器出现即视为加载完成（条件等待，替代固定 sleep）
            try:
                page.wait_for_selector(
                    ".conversationConversationListwrapper, [class*='conversationList'], "
                    "[class*='chatList'], [class*='conversationItem']",
                    timeout=12000,
                )
            except Exception:
                pass
            return True
        except Exception as e:
            logger.info("打开页面失败（第 %s 次）: %s", attempt + 1, str(e)[:80])
            if attempt < 2:
                time.sleep(3)
    return False


_PAGE_DIAG_JS = """
    () => {
        const cand = ['.conversationConversationItemwrapper', '.conversationConversationItemtitle',
            '.conversationConversationListwrapper', "[class*='conversationItem']",
            "[class*='chatList']", "[class*='conversationList']", "[class*='ContactList']"];
        const counts = {};
        for (const s of cand) counts[s] = document.querySelectorAll(s).length;
        const classes = [...new Set(
            [...document.querySelectorAll('[class]')]
              .map(e => typeof e.className === 'string' ? e.className : '')
              .filter(c => /conversation|chat/i.test(c))
        )].slice(0, 30);
        let text = '';
        try { text = (document.body ? document.body.innerText : '').slice(0, 200); } catch (e) {}
        return { url: location.href, title: document.title, body: text, counts: counts, classes: classes };
    }
"""


def _collect_diag(page) -> dict:
    """收集 /chat 页面现场诊断信息，供同步失败时定位是「未登录/未渲染/风控」哪种。"""
    out = {"url": "", "title": "", "body": "", "counts": {}, "classes": []}
    try:
        js = page.evaluate(_PAGE_DIAG_JS)
    except Exception as e:
        out["evaluate_error"] = str(e)[:120]
    if isinstance(js, dict):
        out.update(js)
        if not isinstance(out.get("counts"), dict):
            out["counts"] = {}
        if not isinstance(out.get("classes"), list):
            out["classes"] = []
    return out


_SCROLL_LIST_JS = """
    (step) => {
        let el = null;
        // 精确类名优先（.conversationConversationListwrapper），
        // 未命中再模糊匹配，最后兜底左侧面板内第一个明显可滚动的容器
        const cand = document.querySelector(
            '.conversationConversationListwrapper, [class*="conversationList"], [class*="chatList"], [class*="ContactList"], [class*="contactList"]'
        );
        if (cand && cand.scrollHeight > cand.clientHeight) {
            el = cand;
        } else {
            // 兜底：左侧面板内第一个明显可滚动的容器（x<400 排除右侧会话区）
            const all = [...document.querySelectorAll('div')].filter(
                x => x.scrollHeight > x.clientHeight + 100 && x.clientHeight > 200 &&
                     x.getBoundingClientRect().left < 400
            );
            if (all.length) el = all[0];
        }
        if (!el) return { moved: false, atBottom: true };
        const before = el.scrollTop;
        el.scrollTop = before + step;
        const moved = el.scrollTop > before;
        return {
            moved: moved,
            atBottom: el.scrollTop + el.clientHeight >= el.scrollHeight - 8,
        };
    }
"""


def _wait_real_list(page, timeout_ms: int = 40000) -> bool:
    """等待真实会话列表挂载（骨架屏占位不算）。

    抖音 /chat 冷启动时左侧先渲染骨架屏（占位文本为连续的 "word"），
    会话接口 200 返回后前端才挂载真实会话项。此前固定等待 8 秒常在
    骨架屏阶段就开始提取，导致"会话列表未渲染"误报。真实列表项
    （conversationItem/title 类名）与骨架屏类名不同，出现即代表渲染完成。
    """
    try:
        page.wait_for_selector(
            ".conversationConversationItemtitle, [class*='conversationItem']",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


def _scroll_and_extract(page, collected: list[dict], max_rounds: int = 80) -> None:
    """滚动聊天列表并提取联系人，直到没有新数据或列表触底。

    列表为虚拟滚动，步长过大会跳过部分行导致火花遗漏，故小步快滚。
    滚动优先用 JS 直接改容器 scrollTop：mouse.wheel 依赖悬停坐标，
    坐标被弹窗遮挡时滚动静默失效，stuck 判定提前退出，
    只采到首屏渲染的约 20 行——即"只能发前 20 人"的根因。

    加速技巧：
    - 已见昵称用 set 去重（O(1) 判断），替代旧版 O(n²) 全量 in 扫描；
    - 连续无新数据的轮数阈值降低（稳定 3 轮即停），同时以 scrollTop 停止
      作为"到底"硬信号，避免在已加载完的短列表上空转；
    - 每轮等待缩短，滚动步长自适应：底部剩余不足时取剩余距离，避免无效滚动。
    """
    _dismiss_dialogs(page)
    seen = {c.get("name") for c in collected}
    stable = 0
    last_bottom = False

    for _ in range(max_rounds):
        res = page.evaluate(_EXTRACT_JS) or {}
        data = res.get("items") or []
        new_items = []
        for x in data:
            name = x.get("name")
            if name and name not in seen:
                seen.add(name)
                new_items.append(x)
        if new_items:
            collected.extend(new_items)
            stable = 0
        else:
            stable += 1

        at_bottom = bool(res.get("atBottom"))
        # 触底是硬信号，立即收手；未触底则连续 3 轮无新数据才停
        if at_bottom or stable >= 3:
            break
        last_bottom = at_bottom or last_bottom

        moved = False
        try:
            r = page.evaluate(_SCROLL_LIST_JS, 700) or {}
            moved = bool(r.get("moved"))
        except Exception:
            moved = False
        if not moved:
            # JS 定位不到容器或已到底时回退鼠标滚轮（最多一次，避免无效空转）
            try:
                page.mouse.move(200, 350)
                page.mouse.wheel(0, 700)
            except Exception:
                pass
        # 虚拟列表渲染需要时间，但步长增大后等待可略减
        page.wait_for_timeout(250)


def fetch_chat_contacts(account_id: str | None = None) -> dict:
    """从抖音私信页左侧聊天列表读取联系人（含火花天数），供网页端勾选。

    加速思路：监听 `aweme/v1/web/im/user/info`
    接口响应作为昵称数据源（接口一次返回大批会话好友，且不依赖 DOM 结构），
    DOM 滚动提取用于火花天数与头像。两者取并集，接口快、DOM 兜底。
    """
    aid = account_id or DEFAULT_ACCOUNT_ID
    result = {"at": _now(), "names": [], "error": None}
    state = account_state_path(aid)
    if not state.exists():
        result["error"] = "该账号尚未上传登录态 state.json"
        return result

    api_names: list[dict] = []
    api_seen: set[str] = set()
    im_hits: list[str] = []
    _user_info_keys_printed = {"done": False}

    def _on_im_any(resp):
        """记录所有 aweme/v1/web/im/* 响应（含失败状态码），便于诊断接口路径是否变化或被拒。"""
        try:
            u = resp.url
            if "aweme/v1/web/im" in u and len(im_hits) < 30:
                im_hits.append(f"{resp.status} {u[:140]}")
        except Exception:
            pass

    def _on_api_user_info(resp):
        """拦截 im/user/info 接口，收集会话好友昵称/备注，并探测火花天数字段。"""
        try:
            if "aweme/v1/web/im/user/info" not in resp.url:
                return
            if resp.status != 200:
                return
            data = resp.json()
            items = data.get("data", []) or []
            if isinstance(items, dict):
                items = items.get("user_list") or items.get("list") or items.get("users") or []
            if not isinstance(items, list):
                items = []
            if items and not _user_info_keys_printed["done"]:
                _user_info_keys_printed["done"] = True
                first = items[0]
                if isinstance(first, dict):
                    logger.info(
                        "im/user/info 返回字段: %s",
                        sorted(k for k in first.keys() if not k.startswith("_")),
                    )
            for item in items:
                if not isinstance(item, dict):
                    continue
                nick = _norm_name(item.get("remark_name") or item.get("nickname") or "")
                if not nick or nick in api_seen:
                    continue
                api_seen.add(nick)
                api_names.append({"name": nick, "streak": _probe_streak(item), "avatar": ""})
        except Exception:
            pass

    try:
        with open_browser(state_path=state) as (p, browser, context, page):
            page.on("response", _on_im_any)
            page.on("response", _on_api_user_info)

            if not _open_chat_page(page):
                result["error"] = "无法打开抖音私信页面"
                return result

            # _open_chat_page 内部已条件等待会话列表容器，此处条件等待真实
            # 会话项挂载（骨架屏 "word" 占位不算，冷启动慢时可等 40 秒）。
            real_rendered = _wait_real_list(page, 40000)
            if not real_rendered:
                logger.info("等待真实联系人列表超时（骨架屏未消失）")
            logged, why = check_login(page)
            if not logged:
                result["error"] = why
                return result

            collected: list[dict] = []
            if os.environ.get("DOUYIN_DEBUG_DOM") == "1":
                try:
                    from .config import account_dir
                    dbg = account_dir(aid) / "debug_dom.png"
                    page.locator("body").screenshot(path=str(dbg), timeout=8000)
                    logger.info("已保存调试截图: %s", dbg)
                except Exception as e:
                    logger.info("调试截图失败: %s", str(e)[:80])
            _scroll_and_extract(page, collected)

            if not collected and api_names:
                # DOM 未提取到时，接口数据作为兜底（火花/头像随后由台账回填）
                collected = list(api_names)
                logger.info("DOM 提取为空，改用接口数据 %s 条", len(collected))

            if not collected:
                # 恢复①：整页重载，让 SPA 重新挂载会话列表
                try:
                    page.reload(wait_until="domcontentloaded", timeout=45000)
                    _wait_real_list(page, 25000)
                    _scroll_and_extract(page, collected)
                except Exception:
                    pass

                # 恢复②：直接打开 /chat 时 SPA 冷启动异常（仅渲染空壳、不挂载列表），
                # 先落地首页建立会话上下文，再回到 /chat 让列表正常挂载
                if not collected and not api_names:
                    try:
                        page.goto("https://www.douyin.com/", timeout=45000, wait_until="domcontentloaded")
                        page.wait_for_timeout(4000)
                        _dismiss_dialogs(page)
                        page.goto(CHAT_URL, timeout=45000, wait_until="domcontentloaded")
                        _wait_real_list(page, 25000)
                        _dismiss_dialogs(page)
                        _scroll_and_extract(page, collected)
                    except Exception:
                        pass

            # 接口补充：DOM 漏掉的最新会话好友并入（去重）；DOM 有同名但火花为空的，
            # 用接口探测到的火花值回填（接口字段比前端混淆类名稳定，最通用）。
            if collected and api_names:
                by_name = {c.get("name"): c for c in collected}
                for n in api_names:
                    cur = by_name.get(n.get("name"))
                    if cur is None:
                        collected.append(n)
                    elif not cur.get("streak") and n.get("streak"):
                        cur["streak"] = n.get("streak")
                logger.info("接口补充 %s 条昵称（共 %s 条）", len(api_names) - sum(1 for n in api_names if n["name"] in by_name), len(collected))

            result["names"] = collected
            logger.info("已读取聊天列表联系人 %s 个", len(result["names"]))
            if collected:
                sample = [(c.get("name"), c.get("streak")) for c in collected[:5]]
                logger.info("火花样本（前 5 条，用于核对识别是否正确）: %s", sample)
                _zero = sum(1 for c in collected if not c.get("streak"))
                if _zero > 0 and _zero / len(collected) > 0.5:
                    # 过半联系人火花为空 → 疑似提取失败，自动存首屏图供校准（无需改配置）
                    try:
                        from .config import account_dir
                        dbg = account_dir(aid) / "debug_dom.png"
                        page.locator("body").screenshot(path=str(dbg), timeout=8000)
                        logger.warning("过半联系人火花为空（疑似提取失败），已保存调试图: %s", dbg)
                    except Exception:
                        pass
            elif not result["error"]:
                # 空结果时输出现场诊断（URL/标题/选择器计数/接口命中/类名样本），
                # 并保存截图，避免"同步失败"无任何可排查线索
                diag = _collect_diag(page)
                diag["im_hits"] = im_hits[-12:]
                _screenshot(page, aid)
                logger.warning(
                    "同步联系人为空，现场诊断: %s",
                    json.dumps(diag, ensure_ascii=False)[:900],
                )
                result["error"] = "已同步 0 个联系人：会话列表未渲染，请查看诊断日志与截图"
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


def _send_consumer(page, entry: dict, msg: str, dry_run: bool, result: dict, account_id: str | None = None, tracker: MessageSendTracker | None = None) -> None:
    """通道 A：consumer 重防护发送。"""
    name = entry["display_name"]
    ok, why = send_to_contact(page, name, msg, dry_run, tracker=tracker)
    if ok:
        result["ok"].append(name)
        # dry-run 只演练判定链路，不产生任何台账副作用
        if not dry_run:
            ledger.confirm_join(name, account_id)
            # 发送校准：已进入会话，从会话页读真实火花天数回填（接口/DOM 兜底的最终保险）
            days = _probe_streak_from_page(page)
            if days:
                ledger.set_streak(name, days, account_id)
        logger.info("[%s] 已发送给 %s：%s (%s)", account_id, name, msg if not dry_run else "(干跑)", why)
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
        ledger.update_send_result(name, ok, _now(), account_id=account_id, msg_text=msg)


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
            ledger.update_send_result(name, True, _now(), via_creator=True, account_id=account_id, msg_text=msg)
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
    max_n = int(cfg.get("max_friends_per_run", 0) or 0)
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

            tracker = MessageSendTracker(page)
            try:
                time.sleep(5)
                _dismiss_dialogs(page)
                logged, why = check_login(page)
                if not logged:
                    result["logged_out"] = True
                    result["failed"].append({"name": "_system", "reason": why})
                    _screenshot(page, aid)
                    return result

                logger.info("[%s] 待发送好友 %s 人，dry_run=%s", aid, len(targets), dry_run)
                for entry in targets:
                    name = entry.get("display_name", "?")
                    try:
                        # 上一个好友的处理可能因搜索兜底等路径跳离私信页，
                        # 检测到偏离立即恢复导航，避免本轮剩余目标全部失败。
                        if "/chat" not in (page.url or ""):
                            logger.warning("[%s] 页面已偏离私信页，重新导航", aid)
                            _open_chat_page(page)

                        # 每人发送前复检登录态：轮次中途掉线时（页面弹扫码/骨架化），
                        # 若继续发送会因输入框被弹窗清空而批量产出假成功。
                        logged, why = check_login(page)
                        if not logged:
                            logger.error("[%s] 发送中途登录态失效：%s，中止本轮剩余好友", aid, why)
                            result["logged_out"] = True
                            for rest in targets[len(result["ok"]) + len(result["failed"]) + len(result["skipped"]):]:
                                if rest.get("display_name") != name:
                                    result["failed"].append({"name": rest.get("display_name", "?"), "reason": f"登录态失效，未发送: {why}"})
                            result["failed"].append({"name": name, "reason": f"登录态失效，未发送: {why}"})
                            _screenshot(page, aid)
                            break

                        custom_msg = str(entry.get("custom_message") or "").strip()
                        if custom_msg:
                            msg = custom_msg
                        else:
                            msg = build_message(messages, last_sent_msg=str(entry.get("last_msg", "")))
                        if entry.get("has_conversation"):
                            _send_consumer(page, entry, msg, dry_run, result, aid, tracker=tracker)
                        else:
                            _send_creator(entry, msg, dry_run, result, p, aid)
                    except Exception as e:
                        # 单个好友异常不拖垮整轮：如实记失败并回写台账，继续下一个
                        logger.error("[%s] 处理好友 %s 异常: %s", aid, name, e)
                        result["failed"].append({"name": name, "reason": f"处理异常: {e}"})
                        if not dry_run:
                            try:
                                ledger.update_send_result(name, False, _now(), account_id=aid)
                            except Exception:
                                pass

                    if result["rate_limited"]:
                        break
                    time.sleep(random.uniform(gap_min, gap_max))
            finally:
                tracker.close()
    except Exception as e:
        logger.error("[%s] 运行异常: %s", aid, e)
        result["failed"].append({"name": "_system", "reason": f"运行异常: {e}"})
    return result


# 别名兼容
sync_contacts = fetch_chat_contacts
