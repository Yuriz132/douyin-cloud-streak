"""creator 页抖音号采集：滚动好友列表 + 拦截 user_detail 接口响应。

实测依据：
- 接口带 msToken + a_bogus 签名，无法用裸 HTTP 复现，只能浏览器内拦截；
- ShortId 为原样值（可能含末尾点），只做空白 trim，禁止去点归一化；
- 全程只读（滚动 + 监听响应），不发送任何消息；
- 采集频率控制：滚动间隔可配（默认 500ms）、连续 6 轮无新映射即停。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from ..browser import open_browser
from ..config import DEFAULT_ACCOUNT_ID, account_state_path, load_config

logger = logging.getLogger("douyin-cloud-streak")

CREATOR_CHAT_URL = "https://creator.douyin.com/creator-micro/data/following/chat"

FRIENDS_TAB_CANDIDATES = [
    'xpath=//*[@id="sub-app"]/div/div/div[1]/div[2]',
    'xpath=//*[contains(text(),"好友")]',
]

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


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _click_friends_tab(page) -> bool:
    """点击「好友」tab，返回是否成功。"""
    for sel in FRIENDS_TAB_CANDIDATES:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=3000)
                return True
        except Exception:
            continue
    return False


def _check_session(page, context) -> str | None:
    """检查登录态，返回错误信息或 None。"""
    if "passport" in page.url.lower() or "login" in page.url.lower():
        return f"creator 页跳转到登录页（{page.url}）"
    if not any(c["name"].startswith("sessionid") for c in context.cookies()):
        return "creator 页未检测到 sessionid cookie，登录态可能已过期"
    return None


def _on_response_factory(mapping: dict, urls: list[str], path: str):
    """创建 response 拦截回调，把 user_detail 接口的映射收集到 mapping 中。"""
    def on_response(resp):
        try:
            if path not in resp.url:
                return
            if len(urls) < 3:
                urls.append(resp.url)
            if resp.status != 200:
                return
            data = resp.json()
            for item in data.get("user_list", []) or []:
                user = item.get("user", {}) or {}
                sid = str(user.get("ShortId", "")).strip()
                nick = str(user.get("nickname", "")).strip()
                uid = str(item.get("user_id", ""))
                if sid:
                    mapping[sid] = {"nickname": nick, "user_id": uid}
        except Exception:
            pass
    return on_response


def collect_short_id_map(
    state_path=None,
    target_short_ids: list[str] | None = None,
    max_scrolls: int | None = None,
    stop_when_found: bool = True,
    scroll_interval: float = 0.5,
    account_id: str | None = None,
) -> dict:
    """打开 creator 消息页，滚动好友列表并拦截 user_detail 响应，采集 short_id 映射。

    返回 {"at", "mapping", "count", "hit", "sample_urls", "error"}
    """
    aid = account_id or DEFAULT_ACCOUNT_ID
    result: dict = {
        "at": _now(), "mapping": {}, "count": 0, "hit": False,
        "sample_urls": [], "error": None,
    }
    cfg = load_config(aid)
    api_path = str(cfg.get("creator_user_detail_path") or "aweme/v1/creator/im/user_detail/")
    if max_scrolls is None:
        max_scrolls = int(cfg.get("creator_max_scrolls", 80) or 80)

    mapping: dict[str, dict] = {}
    urls: list[str] = []
    targets = set(target_short_ids or [])

    try:
        ctx_kwargs = {"viewport": {"width": 1366, "height": 900}, "locale": "zh-CN"}
        with open_browser(state_path or account_state_path(aid), **ctx_kwargs) as (p, browser, context, page):
            page.on("response", _on_response_factory(mapping, urls, api_path))

            page.goto(CREATOR_CHAT_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(10000)

            error = _check_session(page, context)
            if error:
                result["error"] = error
                return result

            _click_friends_tab(page)
            page.wait_for_timeout(3000)

            # 滚动 + 拦截
            stagnant = 0
            last_size = 0
            for _ in range(max_scrolls):
                try:
                    moved = bool(page.evaluate(SCROLL_JS))
                except Exception:
                    moved = False
                page.wait_for_timeout(int(scroll_interval * 1000))
                grew = len(mapping) > last_size
                last_size = len(mapping)
                if not moved and not grew:
                    stagnant += 1
                    if stagnant >= 6:
                        break
                else:
                    stagnant = 0
                if stop_when_found and targets and targets.issubset(mapping):
                    break

            result["mapping"] = mapping
            result["count"] = len(mapping)
            result["sample_urls"] = urls
            result["hit"] = bool(targets and targets.issubset(mapping))
            logger.info("creator 采集完成：%s 条映射，命中目标 %s", len(mapping), result["hit"])
    except Exception as e:
        logger.error("creator 采集异常: %s", e)
        result["error"] = f"creator 采集异常: {e}"
    return result


if __name__ == "__main__":
    import sys
    targets = sys.argv[1:] or None
    res = collect_short_id_map(target_short_ids=targets)
    out = {
        "at": res["at"], "count": res["count"], "hit": res["hit"],
        "targets": targets, "error": res["error"],
        "sample_urls": res["sample_urls"],
        "sample_mapping": dict(list(res["mapping"].items())[:5]),
    }
    report = account_state_path(aid).parent / "harvest_report.json"
    report.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("采集完成，report ->", report)
    print("count:", res["count"], "hit:", res["hit"], "error:", res["error"])
