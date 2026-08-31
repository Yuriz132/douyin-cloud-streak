"""网页端扫码登录会话管理。

扫码登录流程：

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
        "deep_link": "",
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
            return {"status": "idle", "message": "", "qrcode": "", "deep_link": ""}
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
        "deep_link": st.get("deep_link", "") if st["status"] == "waiting_scan" else "",
        "error": st["error"],
    }


def _qr_looks_valid(img) -> bool:
    """像素级校验二维码图：黑白像素须同时占显著比例。

    过期过渡帧/canvas 未绘制完成的纯黑或纯白占位图必缺其一，
    这类图发给前端就是用户看到的"二维码纯黑屏"。
    """
    try:
        g = img.convert("L")
        hist = g.histogram()
        total = sum(hist)
        if not total:
            return False
        dark = sum(hist[:64]) / total
        light = sum(hist[192:]) / total
        return dark > 0.04 and light > 0.04
    except Exception:
        return False


def _enhance_qr_image(data_url: str) -> tuple[str, str, bool]:
    """对提取到的二维码做白边静区补全与短链提取。返回 (data_url, deep_link, valid)。

    valid=False 表示源图像素校验失败（黑屏/空白），调用方应刷新重试而非展示。
    """
    deep_link = ""
    if not data_url or not data_url.startswith("data:image"):
        return data_url, deep_link, False
    try:
        from io import BytesIO
        from PIL import Image

        raw_b64 = data_url.split("base64,")[1] if "base64," in data_url else data_url
        img_data = base64.b64decode(raw_b64)
        original_rgba = Image.open(BytesIO(img_data)).convert("RGBA")

        # 透明区合成白底：PNG 带 alpha 时直接 convert("RGB") 透明像素会变黑
        bg = Image.new("RGBA", original_rgba.size, (255, 255, 255, 255))
        composited = Image.alpha_composite(bg, original_rgba).convert("RGB")

        valid = _qr_looks_valid(composited)

        # 补全 32px 白色静区，提高扫码对比度
        pad = 32
        padded_img = Image.new(
            "RGB", (composited.size[0] + pad * 2, composited.size[1] + pad * 2), "WHITE"
        )
        padded_img.paste(composited, (pad, pad))

        # 尝试使用 zxingcpp 解码短链 (若环境已安装)；用补过白边的图解码，
        # 二码贴边缺静区时原图解不出而 padded 图能解出。
        try:
            import zxingcpp
            results = zxingcpp.read_barcodes(padded_img)
            if results:
                qr_text = results[0].text
                if any(d in qr_text for d in ("douyin.com", "snssdk.com", "iesdouyin.com")):
                    deep_link = qr_text
        except Exception:
            pass

        buf = BytesIO()
        padded_img.save(buf, format="PNG")
        enhanced_b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/png;base64,{enhanced_b64}", deep_link, valid
    except Exception:
        return data_url, deep_link, True  # 增强失败时保守放行原图，避免误杀可用码


def _extract_valid_qrcode(page, timeout_ms: int = 45000) -> tuple[str, str]:
    """提取二维码并做像素校验；黑屏/空白过渡帧自动点击刷新重试。

    返回 (enhanced_data_url, deep_link)；多次重试仍无效时返回 ("", "")。
    """
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        qr = _wait_and_extract_qrcode(page, timeout_ms=12000)
        if qr:
            enhanced, deep_link, valid = _enhance_qr_image(qr)
            if valid:
                return enhanced, deep_link
            logger.warning("二维码像素校验失败（疑似黑屏/空白过渡帧），尝试刷新重试")
        try:
            _click_qr_refresh(page)
        except Exception:
            pass
        page.wait_for_timeout(2500)
    return "", ""


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
    """回退为纯无头模式：因为 Xvfb 在小内存云服务器上会引发严重的性能与超时问题。"""
    common = dict(
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--disable-extensions",
            "--disable-software-rasterizer",
            "--renderer-process-limit=2",
            "--no-zygote",
            "--mute-audio",
        ],
        ignore_default_args=["--enable-automation"],
    )
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
        # 伪装 WebGL 厂商信息，降低无头浏览器指纹特征
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

        # 用条件等待替代固定 sleep：打开登录页后立即等二维码容器出现，
        # 容器出现即点击唤起，避免旧逻辑"等 3s+1s+2s"的固定开销。
        # 网络抖动时 goto 可能失败，重试一次降低二维码获取失败率。
        for _goto_attempt in range(2):
            try:
                page.goto(CHAT_URL, timeout=60000, wait_until="domcontentloaded")
                break
            except Exception:
                if _goto_attempt == 0:
                    page.wait_for_timeout(1500)
                    continue
                logger.info("[%s] 打开登录页失败，继续尝试等待二维码", aid)

        # 等待登录面板/二维码容器出现（最长 12s），出现即继续，未出现也不阻塞
        try:
            page.wait_for_selector("#animate_qrcode_container", timeout=12000)
        except Exception:
            pass

        # 扫码登录前置动作：收起面板残留 -> 切「扫码登录」标签 -> 点二维码容器。
        # 实测当前登录面板默认即「扫码登录」，二维码容器会自动出码，
        # 这两次点击在大部分结构下会超时（被吞掉，不阻塞），用短超时避免白耗。
        try:
            page.locator(
                "#douyin_login_comp_flat_panel > div > div:nth-child(2) > div > div:nth-child(4) > p"
            ).click(timeout=500)
        except Exception:
            pass
        try:
            page.get_by_text("扫码登录").first.click(timeout=500)
        except Exception:
            pass
        # 等待扫码登录标签生效（二维码容器出现），条件等待替代固定 1s
        try:
            page.wait_for_selector("#animate_qrcode_container", timeout=5000)
        except Exception:
            pass
        try:
            page.locator("#animate_qrcode_container").first.click(timeout=1500)
        except Exception:
            pass
        # 不再单独等待 img：二维码冷启动约 8s 才出图，此处的固定等待只会白耗，
        # _extract_valid_qrcode 内部会自行轮询等待并校验有效码。

        enhanced_qr, deep_link = _extract_valid_qrcode(page)
        if not enhanced_qr:
            if _slider_captcha_present(page):
                raise RuntimeError(
                    "抖音触发滑动验证码（风控拦截），无法获取登录二维码。"
                    "请稍后重新发起扫码；若反复出现，请改用本地电脑运行"
                    "「1.本地运行.bat」（输入 1）扫码后，用「2.上传本地文件加服务器部署.bat」（输入 1）上传"
                )
            raise RuntimeError("未能从页面提取到有效的登录二维码（多次刷新仍为黑屏/空白），请稍后重试")
        _set(aid, status="waiting_scan", message="请使用抖音 App 扫码登录", qrcode=enhanced_qr, deep_link=deep_link)

        deadline = time.time() + SESSION_TIMEOUT
        refresh_count = 0
        face_clicked = False
        slider_polls = 0
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
                enhanced_qr, deep_link = _extract_valid_qrcode(page, timeout_ms=30000)
                if enhanced_qr:
                    _set(aid, qrcode=enhanced_qr, deep_link=deep_link,
                         message=f"二维码已自动刷新（第 {refresh_count} 次），请重新扫码")

            # 滑动验证码风控：出现且持续未消失（3 次轮询 ≈ 4.5s）时快速失败，
            # 给出明确反馈，避免干等超时后只报「扫码超时」让人摸不着头脑
            if _slider_captcha_present(page):
                slider_polls += 1
                if slider_polls >= 3:
                    logger.warning("[%s] 触发滑动验证码（风控拦截），无法自动完成，终止扫码会话", aid)
                    _set(aid, status="failed",
                         message="抖音触发滑动验证码（风控拦截），网页端无法代你完成人工滑动。"
                                 "建议：① 稍等几分钟重新发起扫码；② 若反复出现，请改用本地电脑运行"
                                 "「1.本地运行.bat」（输入 1）扫码后，用「2.上传本地文件加服务器部署.bat」（输入 1）上传。",
                         error="slider_captcha", qrcode="")
                    return
            else:
                slider_polls = 0

            # 二次安全验证风控处理：确认登录后可能要求刷脸，
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
                    enhanced_face, deep_face, valid = _enhance_qr_image(qr_face)
                    # 刷脸码同样可能拿到黑屏过渡帧：无效时保留旧图，下轮轮询再取
                    if valid:
                        _set(aid, qrcode=enhanced_face, deep_link=deep_face)

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
    """二次验证取码：按尺寸启发式扫描页面中的 img/canvas。"""
    try:
        data = page.evaluate(_FACE_QR_JS)
        return data if data and data.startswith("data:image") else None
    except Exception:
        return None


def _wait_and_extract_qrcode(page, timeout_ms: int = 45000) -> str | None:
    """等待二维码出现并提取为 data URL；失败时整页截图兜底。

    容器冷启动首次加载可能超过 20s，窗口过短会把慢加载误判为失败。
    优化：先查主 frame（多数情况二维码就在主文档），再查 iframe；
    轮询间隔 400ms，比旧版 800ms 快一倍，缩短二维码出现后的感知等待。
    """
    deadline = time.time() + timeout_ms / 1000
    src = ""
    while time.time() < deadline:
        # 主 frame 优先：二维码通常直接渲染在主文档，无需每轮遍历所有 iframe
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
        # 二维码可能在 iframe 中（登录面板走独立域时）
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
        page.wait_for_timeout(400)

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


# 滑动验证码（拼图/滑块风控）提示文案与结构特征
_SLIDER_TEXTS = ("拖动滑块", "拖动下方滑块", "向右拖动", "滑块填充拼图", "完成拼图")


def _slider_captcha_present(page) -> bool:
    """检测抖音滑动验证码（滑块拼图风控）是否出现在页面上。"""
    try:
        for sel in (
            "#captcha_container",
            "#captcha-verify-image",
            "[class*='captcha_verify']",
            "iframe[src*='captcha']",
            "iframe[src*='secsdk']",
        ):
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                return True
        for t in _SLIDER_TEXTS:
            loc = page.get_by_text(t, exact=False)
            if loc.count() and loc.first.is_visible():
                return True
    except Exception:
        pass
    return False


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
