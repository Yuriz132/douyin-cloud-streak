"""网页端扫码登录会话管理（复刻「抖音自动续火花 2.1」的扫码机制）。

与 2.1 商业版 huohua.py 的流程一致，但基于 Playwright 实现且更简洁：

1. start(account_id)   为账号启动专属无头 Chromium（占用全局并发名额），
                       打开抖音并唤起登录二维码；
2. status(account_id)  前端轮询：返回当前状态与二维码 data URL；
3. 成功检测            轮询 cookie 出现 sessionid/sessionid_ss 即视为登录成功，
                       自动导出 storage_state 覆盖该账号 state.json 并销毁浏览器；
4. cancel(account_id)  手动取消；二维码过期自动点击刷新重新提取；
5. GC                  会话整体超时（默认 5 分钟）自动回收，防浏览器泄漏。

同账号同时只允许一个扫码会话；不同账号可各自扫码（受全局并发上限约束）。
"""

from __future__ import annotations

import base64
import logging
import os
import random
import shutil
import subprocess
import threading
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

from .accounts import acquire_browser_slot, release_browser_slot
from .config import DEFAULT_ACCOUNT_ID, ROOT_STATE_PATH, account_state_path

logger = logging.getLogger("douyin-cloud-streak")

CHAT_URL = "https://www.douyin.com/chat?isPopup=1"

# 扫码等待总时长：覆盖"掏手机 -> 打开抖音 -> 扫码 -> 确认"的完整动作
SESSION_TIMEOUT = 300
# 二维码自动刷新次数上限（抖音二维码约 2~3 分钟过期一次）
QR_REFRESH_LIMIT = 5

# 登录成功判定 Cookie：覆盖抖音各端变体（sid_guard/sid_tt/uid_tt 与 sessionid 同批下发）
_LOGIN_COOKIE_NAMES = {"sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt"}

_slot_guard = threading.Lock()
_slot_holders: set[str] = set()


def _acquire_slot_tracked(aid: str) -> None:
    """获取全局并发名额并登记归属，保证释放幂等（线程卡死被强制接管时不重复释放）。"""
    acquire_browser_slot()
    with _slot_guard:
        _slot_holders.add(aid)


def _release_slot_once(aid: str) -> None:
    with _slot_guard:
        if aid not in _slot_holders:
            return
        _slot_holders.discard(aid)
    release_browser_slot()


def _hard_expire(aid: str) -> None:
    """硬超时保护：工作线程卡死（如浏览器进程被杀后同步调用挂起）时强制终态。"""
    flag = _stop_flags.get(aid)
    if flag:
        flag.set()
    with _guard:
        st = _sessions.get(aid)
        if st and st["status"] in ("queuing", "starting", "waiting_scan"):
            st.update(status="expired", message="扫码会话超时，请重新发起扫码", qrcode="")
            logger.warning("[%s] 扫码会话触发硬超时保护（工作线程疑似卡死）", aid)
    _release_slot_once(aid)

_QR_SELECTORS = [
    "#animate_qrcode_container img",
    '[data-e2e="login-qrcode"] img',
    'div[class*="qrcode"] img',
]

_QR_EXPIRED_TEXTS = ["二维码已过期", "已失效", "已过期", "点击刷新", "刷新"]

_guard = threading.Lock()
_sessions: dict[str, dict] = {}
_stop_flags: dict[str, threading.Event] = {}


def _new_state(aid: str, **fields) -> dict:
    st = {
        "status": "starting",
        "message": "正在启动扫码环境…",
        "qrcode": "",
        "started_at": time.time(),
        "last_active": time.time(),
        "error": "",
    }
    st.update(fields)
    return st


def start(account_id: str) -> dict:
    """为指定账号启动扫码会话（幂等：已有活跃会话则直接返回其状态）。"""
    with _guard:
        old = _sessions.get(account_id)
        if old and old["status"] in ("queuing", "starting", "waiting_scan"):
            return {"ok": True, "resumed": True, **_public(old)}
        flag = threading.Event()
        _stop_flags[account_id] = flag
        st = _new_state(
            account_id,
            status="queuing",
            message="正在排队获取浏览器名额…",
        )
        _sessions[account_id] = st

    t = threading.Thread(target=_session_worker, args=(account_id, flag), daemon=True)
    t.start()
    watchdog = threading.Timer(SESSION_TIMEOUT + 90, lambda: _hard_expire(account_id))
    watchdog.daemon = True
    watchdog.start()
    logger.info("[%s] 网页扫码会话已启动", account_id)
    return {"ok": True, "resumed": False, **_public(st)}


def status(account_id: str) -> dict:
    """查询会话状态（前端轮询入口）。无会话时返回 idle。"""
    with _guard:
        st = _sessions.get(account_id)
        if not st:
            return {"status": "idle", "message": "", "qrcode": ""}
        if st["status"] == "waiting_scan":
            st["last_active"] = time.time()
        return _public(st)


def cancel(account_id: str) -> dict:
    """取消/终止会话并释放浏览器。"""
    with _guard:
        st = _sessions.get(account_id)
        if not st or st["status"] in ("success", "failed", "expired", "cancelled"):
            _sessions.pop(account_id, None)
            return {"ok": True, "message": "无进行中的扫码会话"}
        flag = _stop_flags.get(account_id)
    if flag:
        flag.set()
    # 给线程一点时间自行清理，随后强制标记
    for _ in range(30):
        time.sleep(0.1)
        with _guard:
            cur = _sessions.get(account_id)
            if not cur or cur["status"] not in ("queuing", "starting", "waiting_scan"):
                break
    else:
        with _guard:
            cur = _sessions.get(account_id)
            if cur and cur["status"] in ("waiting_scan",):
                cur["status"] = "cancelled"
                cur["message"] = "已取消"
    logger.info("[%s] 扫码会话已取消", account_id)
    return {"ok": True, "message": "已取消"}


def _public(st: dict) -> dict:
    return {
        "status": st["status"],
        "message": st["message"],
        "qrcode": st["qrcode"] if st["status"] == "waiting_scan" else "",
        "error": st["error"],
    }


def _set(aid: str, **fields) -> None:
    with _guard:
        st = _sessions.get(aid)
        if st is None:
            return
        st.update(fields)
        st["last_active"] = time.time()


def _is_stopped(aid: str) -> bool:
    flag = _stop_flags.get(aid)
    return bool(flag and flag.is_set())


def _launch_browser(pw):
    """优先在 Xvfb 虚拟屏幕中启动有头 Chromium：真实有头内核的风控识别率远低于无头模式。

    返回 (browser, xvfb_proc)；Xvfb 不可用或启动失败时回退为无头模式。
    """
    common = dict(
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--disable-extensions",
            "--disable-software-rasterizer",
        ],
        # 排除自动化开关，与商业版 excludeSwitches 等效
        ignore_default_args=["--enable-automation"],
    )
    if shutil.which("Xvfb"):
        for _ in range(4):
            display = f":{random.randint(90, 180)}"
            try:
                xproc = subprocess.Popen(
                    ["Xvfb", display, "-screen", "0", "1366x900x24", "-nolisten", "tcp"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                time.sleep(0.8)
                if xproc.poll() is not None:
                    continue  # 显示号被占用等，换一个重试
                try:
                    browser = pw.chromium.launch(
                        headless=False, env={**os.environ, "DISPLAY": display}, **common
                    )
                    return browser, xproc
                except Exception:
                    xproc.terminate()
            except Exception:
                continue
    return pw.chromium.launch(headless=True, **common), None


def _session_worker(aid: str, stop_flag: threading.Event) -> None:
    pw = None
    browser = None
    xvfb_proc = None
    try:
        _acquire_slot_tracked(aid)
        if _is_stopped(aid):
            raise CancelledError()

        _set(aid, status="starting", message="正在打开抖音登录页…")
        pw = sync_playwright().start()
        browser, xvfb_proc = _launch_browser(pw)
        # UA 版本号与真实内核保持一致，固定旧版本号容易被风控识别为伪造环境
        chrome_major = (browser.version or "").split(".")[0] or "124"
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{chrome_major}.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            ignore_https_errors=True,
        )
        # 与 2.1 商业版 stealth(webgl_vendor="Intel Inc.", renderer="Intel Iris OpenGL Engine") 对齐
        context.add_init_script(
            "const _spoof=(proto)=>{const g=proto.getParameter;"
            "proto.getParameter=function(p){if(p===37445)return 'Intel Inc.';"
            "if(p===37446)return 'Intel Iris OpenGL Engine';return g.apply(this,[p]);};};"
            "if(window.WebGLRenderingContext)_spoof(WebGLRenderingContext.prototype);"
            "if(window.WebGL2RenderingContext)_spoof(WebGL2RenderingContext.prototype);"
        )
        page = context.new_page()
        try:
            from .browser import _apply_stealth
            _apply_stealth(page)
        except Exception:
            pass

        page.goto(CHAT_URL, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # 复刻 2.1 GetLoginPng 前置动作：收起面板残留 -> 切「扫码登录」标签 -> 点二维码容器
        try:
            page.locator(
                "#douyin_login_comp_flat_panel > div > div:nth-child(2) > div > div:nth-child(4) > p"
            ).click(timeout=1500)
        except Exception:
            pass
        try:
            page.get_by_text("扫码登录").first.click(timeout=1500)
        except Exception:
            pass
        page.wait_for_timeout(1000)
        try:
            page.locator("#animate_qrcode_container").first.click(timeout=1500)
        except Exception:
            pass
        page.wait_for_timeout(2000)

        qr_data = _wait_and_extract_qrcode(page)
        if not qr_data:
            raise RuntimeError("未能从页面提取到登录二维码，请稍后重试")
        _set(aid, status="waiting_scan", message="请使用抖音 App 扫码登录", qrcode=qr_data)

        deadline = time.time() + SESSION_TIMEOUT
        refresh_count = 0
        face_clicked = False
        polls = 0
        while time.time() < deadline:
            if _is_stopped(aid):
                raise CancelledError()

            cookies = context.cookies("https://www.douyin.com")
            if any(c.get("name") in _LOGIN_COOKIE_NAMES and c.get("value") for c in cookies):
                _save_state(context, aid)
                _set(aid, status="success",
                     message=f"登录成功！已保存该账号的登录态（{len(cookies)} 条 Cookie）")
                logger.info("[%s] 网页扫码登录成功，state.json 已更新", aid)
                return

            polls += 1
            if polls % 10 == 0:
                names = ",".join(sorted({c.get("name", "") for c in cookies if c.get("name")}))
                logger.info("[%s] 等待扫码确认中，当前 Cookie：%s", aid, names or "无")

            if _qr_expired(page):
                refresh_count += 1
                if refresh_count > QR_REFRESH_LIMIT:
                    raise RuntimeError("二维码刷新次数过多，请重新发起扫码")
                logger.info("[%s] 登录二维码已过期，第 %s 次自动刷新", aid, refresh_count)
                _click_qr_refresh(page)
                page.wait_for_timeout(2500)
                qr_data = _wait_and_extract_qrcode(page, timeout_ms=30000)
                if qr_data:
                    _set(aid, qrcode=qr_data,
                         message=f"二维码已自动刷新（第 {refresh_count} 次），请重新扫码")

            # 复刻 2.1 GetCooker 的二次刷脸风控处理：确认登录后可能要求刷脸，
            # 页面会展示新二维码供手机扫描，需持续提取并点击「已完成」
            if not face_clicked:
                if _js_click_first(page, ["手机刷脸验证", "刷脸验证"]):
                    face_clicked = True
                    logger.info("[%s] 触发二次安全验证，已点击刷脸按钮", aid)
                    _set(aid, message="触发安全验证：请用抖音 App 扫描下方新二维码并按提示完成验证")
                    page.wait_for_timeout(3000)
            else:
                _js_click_first(page, ["已完成", "验证成功"])
                qr_face = _extract_face_qr(page)
                if qr_face:
                    _set(aid, qrcode=qr_face)

            page.wait_for_timeout(1500)

        _set(aid, status="expired", message="扫码超时，请重新发起扫码", qrcode="")
        logger.info("[%s] 扫码会话超时结束", aid)

    except CancelledError:
        _set(aid, status="cancelled", message="已取消", qrcode="")
    except Exception as e:
        msg = str(e)[:200]
        _set(aid, status="failed", message="扫码会话异常", error=msg, qrcode="")
        logger.warning("[%s] 扫码会话异常：%s", aid, msg)
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if pw:
            try:
                pw.stop()
            except Exception:
                pass
        if xvfb_proc:
            try:
                xvfb_proc.terminate()
            except Exception:
                pass
        _release_slot_once(aid)
        _stop_flags.pop(aid, None)
        # 终态保留 120 秒供前端读取，之后由 GC 或下次 start 清理
        threading.Timer(120, lambda: _sessions.pop(aid, None)).start()


class CancelledError(Exception):
    pass


def _js_click_first(page, texts: list[str]) -> bool:
    """对包含指定文本的首个元素执行 JS 点击（绕过遮挡），成功返回 True。"""
    for t in texts:
        try:
            loc = page.get_by_text(t, exact=False)
            if loc.count():
                loc.first.evaluate("el => el.click()")
                return True
        except Exception:
            continue
    return False


_FACE_QR_JS = """
() => {
    const pick = (el) => {
        const rect = el.getBoundingClientRect();
        if (rect.width < 100 || rect.width > 350 || Math.abs(rect.width - rect.height) > 15) return null;
        const src = el.src || "";
        if (src.includes("base64,")) return src;
        try {
            const c = document.createElement("canvas");
            c.width = el.naturalWidth || rect.width;
            c.height = el.naturalHeight || rect.height;
            c.getContext("2d").drawImage(el, 0, 0, c.width, c.height);
            return c.toDataURL("image/png");
        } catch (e) { return null; }
    };
    const imgs = document.querySelectorAll("img");
    for (let i = imgs.length - 1; i >= 0; i--) {
        const r = pick(imgs[i]);
        if (r) return r;
    }
    const canvases = document.querySelectorAll("canvas");
    for (let j = canvases.length - 1; j >= 0; j--) {
        const c = canvases[j];
        const rect = c.getBoundingClientRect();
        if (rect.width >= 100 && rect.width <= 350 && Math.abs(rect.width - rect.height) <= 15) {
            try { return c.toDataURL("image/png"); } catch (e) {}
        }
    }
    return null;
}
"""


def _extract_face_qr(page) -> str | None:
    """复刻 2.1 二次验证取码：按尺寸启发式扫描页面中的 img/canvas。"""
    try:
        data = page.evaluate(_FACE_QR_JS)
        return data if data and data.startswith("data:image") else None
    except Exception:
        return None


def _wait_and_extract_qrcode(page, timeout_ms: int = 45000) -> str | None:
    """等待二维码出现并提取为 data URL；失败时整页截图兜底。

    容器冷启动首次加载可能超过 20s，窗口过短会把慢加载误判为失败。
    """
    deadline = time.time() + timeout_ms / 1000
    src = ""
    while time.time() < deadline:
        for sel in _QR_SELECTORS:
            try:
                loc = page.locator(sel)
                if loc.count():
                    first = loc.first
                    if first.is_visible():
                        candidate = first.get_attribute("src") or ""
                        if len(candidate) > 50:
                            src = candidate
                            break
            except Exception:
                continue
        if src:
            break
        # 二维码可能在 iframe 中
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            for sel in _QR_SELECTORS:
                try:
                    loc = frame.locator(sel)
                    if loc.count() and loc.first.is_visible():
                        candidate = loc.first.get_attribute("src") or ""
                        if len(candidate) > 50:
                            src = candidate
                            break
                except Exception:
                    continue
            if src:
                break
        if src:
            break
        page.wait_for_timeout(800)

    if src.startswith("data:image"):
        return src
    if src.startswith("http"):
        try:
            resp = requests.get(src, timeout=8)
            b64 = base64.b64encode(resp.content).decode()
            return f"data:image/png;base64,{b64}"
        except Exception:
            pass
    if src:
        return f"data:image/png;base64,{src}"
    # 兜底：整页截图（用户至少能看到登录框与二维码）；渲染进程繁忙时可能瞬时失败，重试一次
    for _attempt in range(2):
        try:
            shot = page.screenshot(timeout=8000)
            return "data:image/png;base64," + base64.b64encode(shot).decode()
        except Exception:
            try:
                page.wait_for_timeout(1500)
            except Exception:
                break
    return None


def _qr_expired(page) -> bool:
    """检测二维码是否已过期（出现过期提示文本）。"""
    for text in _QR_EXPIRED_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            if loc.count():
                for i in range(min(loc.count(), 3)):
                    if loc.nth(i).is_visible():
                        return True
        except Exception:
            continue
    return False


def _click_qr_refresh(page) -> None:
    """点击二维码区域的刷新按钮重新出码。"""
    candidates = [
        "#animate_qrcode_container",
        'div[class*="qrcode"]',
        'div[class*="refresh"]',
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=3000)
                return
        except Exception:
            continue
    try:
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
    except Exception:
        pass


def _save_state(context, account_id: str) -> None:
    """导出 storage_state 覆盖该账号 state.json（default 账号同步根目录副本）。"""
    state = context.storage_state()
    raw = _ensure_origins(state)
    path: Path = account_state_path(account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    if account_id == DEFAULT_ACCOUNT_ID:
        try:
            ROOT_STATE_PATH.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def _ensure_origins(state) -> dict:
    """storage_state 兼容处理：确保结构与上传校验一致（cookies 列表 + origins）。"""
    if isinstance(state, dict):
        state.setdefault("cookies", [])
        state.setdefault("origins", [])
        return state
    return {"cookies": [], "origins": []}
