"""离线验证 automation.py 重构后的发送判定链路（mock Playwright 对象，不启动浏览器）。

运行：python3 tests/test_send_verdict.py
"""
import base64
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.automation import (
    _receipt_verdict, _settle_send, _verify_in_conversation,
    _list_title_matches, MessageSendTracker,
)

PASS, FAIL = 0, 0
def check(name, cond):
    global PASS, FAIL
    print(("PASS" if cond else "FAIL"), name)
    PASS += cond; FAIL += (not cond)

# ── 1. 收据翻译 ──────────────────────────────────────────────
check("R1 无收据返回None", _receipt_verdict(None) is None)
check("R2 code0成功", _receipt_verdict({"status_code": 0})[0] is True)
check("R3 code3审核拦截", _receipt_verdict({"status_code": 3, "status_msg": "sensitive"})[0] is False)
check("R4 code5拉黑", _receipt_verdict({"status_code": 5})[0] is False)

# ── 2. _settle_send 判定矩阵 ─────────────────────────────────
class FakeBox:
    def __init__(self, texts): self.texts = list(texts); self.i = 0
    def inner_text(self):
        t = self.texts[min(self.i, len(self.texts)-1)]; self.i += 1; return t

class FakeTrackerPage:
    def on(self, *a): pass
    def remove_listener(self, *a): pass

def tracker_with(receipt=None, delay=0):
    t = MessageSendTracker(FakeTrackerPage())
    if receipt is not None:
        if delay:
            t._delay_until = time.time() + delay
            t._pending = dict(receipt, time=time.time())
        else:
            t.last_receipt = dict(receipt, time=time.time())
    return t

# 延迟出现收据：pop_recent 需在 delay 后才返回
class DelayTracker(MessageSendTracker):
    def __init__(self, receipt, show_after):
        super().__init__(FakeTrackerPage())
        self._rc = dict(receipt, time=time.time()); self._show_at = time.time() + show_after
    def pop_recent(self, within_seconds=15):
        if time.time() >= self._show_at:
            r = self._rc; self._rc = None; return r
        return None

# S1 有收据且 code0 → 成功，即使输入框未清空也以收据为准
box = FakeBox(["🔥", "🔥"])
ok, why = _settle_send(box, tracker_with({"status_code": 0}), "🔥", wait=1, grace=0.5)
check("S1 收据code0→成功", ok and "msg_id" not in why or ok)

# S2 迟到拦截收据（清空后宽限窗内到达）→ 失败
box = FakeBox(["", ""])  # 输入框立即“清空”
ok, why = _settle_send(box, DelayTracker({"status_code": 3}, show_after=1.2), "🔥", wait=1, grace=2)
check("S2 宽限窗内迟到拦截→失败", not ok and "安全审核" in why)

# S3 无收据+已清空 → 弱判定成功
box = FakeBox(["", ""])
ok, why = _settle_send(box, None, "🔥", wait=1, grace=0.6)
check("S3 无收据已清空→成功", ok)

# S4 未清空+无收据 → 失败
box = FakeBox(["🔥"]*99)
t0 = time.time()
ok, why = _settle_send(box, None, "🔥", wait=1, grace=0)
check("S4 未清空→失败", not ok)

# ── 3. 会话切换全等校验 ──────────────────────────────────────
class FakeEl:
    def __init__(self, text, x=500, y=50): self.t = text; self.b = {"x": x, "y": y}
    def bounding_box(self): return self.b
    def inner_text(self): return self.t

class FakeByText:
    def __init__(self, els): self.els = els
    def count(self): return len(self.els)
    def nth(self, i): return self.els[i]

def page_with(header_els):
    class P:
        def get_by_text(self, name, exact=True): return FakeByText(header_els)
    return P()

check("V1 全等命中→True", _verify_in_conversation(page_with([FakeEl("小美")]), "小美") is True)
# 前缀昵称误匹配场景：头部是「小美酱」，目标「小美」必须校验失败
check("V2 前缀名不误判→False", _verify_in_conversation(page_with([FakeEl("小美酱")]), "小美") is False)
# NBSP 归一化：头部带不间断空格仍应全等命中
check("V3 NBSP归一化命中", _verify_in_conversation(page_with([FakeEl("小\u00a0美")]), "小 美") is True)
# 区域外（左侧列表 x<300）不算切换成功
check("V4 左侧区域排除", _verify_in_conversation(page_with([FakeEl("小美", x=100)]), "小美") is False)

# ── 4. 同名会话拦截 ──────────────────────────────────────────
class FakeTitles:
    def __init__(self, texts): self.texts = texts
    def count(self): return len(self.texts)
    def nth(self, i):
        el = FakeEl(self.texts[i], x=150)
        class W:
            def inner_text(self): return self.t.t
            def bounding_box(self): return self.t.b
        return W.__new__(W) if False else _Wrap(el)
class _Wrap:
    def __init__(self, el): self.el = el
    def inner_text(self): return self.el.inner_text()
    def bounding_box(self): return self.el.bounding_box()

def title_page(texts):
    class P:
        def locator(self, sel): return FakeTitles(texts)
    return P()

check("D1 同名2行→count2", _list_title_matches(title_page(["张三", "张三", "李四"]), "张三") == 2)
check("D2 唯一→count1", _list_title_matches(title_page(["张三", "李四"]), "张三") == 1)
check("D3 右侧同名不计入", _list_title_matches(title_page([]), "张三") == 0)

# ── 5. 收据 URL 匹配收紧（path 锚定 + status_code 缺失丢弃）──
import re as _re
from core import ledger as ledger_mod

class FakeResp:
    def __init__(self, url, body, status=200):
        self.url = url; self._b = body; self.status = status
    def json(self): return self._b

def fresh_tracker():
    return MessageSendTracker(FakeTrackerPage())

t = fresh_tracker()
t._on_response(FakeResp("https://imapi.douyin.com/aweme/v1/imapi/conv/list?aid=6383", {"status_code": 0}))
check("T1 conv列表不误收", t.last_receipt is None)
t._on_response(FakeResp("https://frontien-i18n.douyin.com/luckycat/aweme/v1/message/send_task", {"status_code": 0}))
check("T5 send_task等前缀路径不误收", t.last_receipt is None)
t._on_response(FakeResp("https://imapi.douyin.com/aweme/v1/message/send", {"message": "ok"}))
check("T2 缺status_code丢弃", t.last_receipt is None)
t._on_response(FakeResp("https://imapi.douyin.com/aweme/v1/message/send",
                        {"status_code": 0, "data": {"server_message_id": "m9"}}))
check("T3 正常发送接口采收据", (t.last_receipt or {}).get("server_message_id") == "m9")
t.last_receipt = None
t._on_response(FakeResp("https://imapi.douyin.com/aweme/v1/message/send", {"status_code": 0}, status=500))
check("T4 非200忽略", t.last_receipt is None)

# ── 6. 武装时间过滤：上轮迟到旧收据不得污染新一轮 ──
t = fresh_tracker()
t.reset()  # 武装本轮采集
t.last_receipt = {"status_code": 3, "time": time.time() - 30}
check("A1 武装前的迟到旧收据不取", t.pop_recent() is None)
t.last_receipt = {"status_code": 0, "time": time.time()}
r = t.pop_recent()
check("A2 武装后新鲜收据取出", r is not None and r.get("status_code") == 0)
check("A3 取出即清空防重放", t.pop_recent() is None)

# ── 7. 火花天数正则："N天前"时间标签不得误采为火花 ──
spark_pat = _re.compile(r"(\d{1,4})\s*天(?!前)")
check("E1 '3天前'不误采", spark_pat.search("小美 3天前 你好呀") is None)
_m = spark_pat.search("连续互动 5天 了")
check("E2 正常火花采到5", bool(_m) and _m.group(1) == "5")

# ── 8. last_msg 回写（build_message 去重依赖此字段） ──
_store = [{"display_name": "张三", "selected": True}]
_orig_load, _orig_save = ledger_mod.load_ledger, ledger_mod._save
ledger_mod.load_ledger = lambda account_id=None: _store
ledger_mod._save = lambda entries, account_id=None: None
try:
    ledger_mod.update_send_result("张三", True, at="2026-08-26 10:00:00", msg_text="早上好")
    check("L1 成功时回写last_msg", _store[0].get("last_msg") == "早上好")
    ledger_mod.update_send_result("张三", False, at="2026-08-26 11:00:00", msg_text="晚上好")
    check("L2 失败时不覆盖last_msg", _store[0].get("last_msg") == "早上好")
finally:
    ledger_mod.load_ledger, ledger_mod._save = _orig_load, _orig_save

# ── 9. 二维码像素校验：黑屏/空白过渡帧必须被判无效 ──
def _png_bytes(img):
    from io import BytesIO
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

from core.login_session import _qr_looks_valid, _enhance_qr_image
try:
    from PIL import Image
    import random as _rnd

    # 模拟正常二维码：白底随机黑白块（黑白占比均显著）
    qr_img = Image.new("RGB", (200, 200), "WHITE")
    for _x in range(25):
        for _y in range(25):
            if _rnd.random() < 0.5:
                for dx in range(4):
                    for dy in range(4):
                        qr_img.putpixel((_x * 4 + dx, _y * 4 + dy), (0, 0, 0))
    check("Q1 正常二维码判有效", _qr_looks_valid(qr_img) is True)

    black_img = Image.new("RGB", (200, 200), (10, 10, 10))
    check("Q2 纯黑屏判无效", _qr_looks_valid(black_img) is False)

    white_img = Image.new("RGB", (200, 200), "WHITE")
    check("Q3 纯空白判无效", _qr_looks_valid(white_img) is False)

    # alpha 全透明 PNG 直接 convert("RGB") 会变黑——增强函数须合成白底后仍有效
    rgba_img = Image.new("RGBA", (120, 120), (0, 0, 0, 0))
    for _x in range(15):
        for _y in range(15):
            if (_x + _y) % 2 == 0:
                for dx in range(8):
                    for dy in range(8):
                        rgba_img.putpixel((_x * 8 + dx, _y * 8 + dy), (0, 0, 0, 255))
    url, link, ok = _enhance_qr_image("data:image/png;base64," + _png_bytes(rgba_img))
    check("Q4 alpha透明图增强后有效且非黑", ok and url.startswith("data:image/png"))

    # 纯黑 data URL 必须报无效（调用方会刷新重试）
    _, _, ok2 = _enhance_qr_image("data:image/png;base64," + _png_bytes(black_img))
    check("Q5 黑屏dataURL报无效", ok2 is False)
except ImportError:
    print("SKIP Q组（无 Pillow）")

# ── 10. 滚动采集 JS 与提取 JS 语法自检 ──
import subprocess
from core.automation import _SCROLL_LIST_JS, _EXTRACT_JS
for _name, _js in (("X1 SCROLL_JS", _SCROLL_LIST_JS), ("X2 EXTRACT_JS", _EXTRACT_JS)):
    _p = subprocess.run(["node", "-e", f"new Function({_js!r}); console.log('ok')"],
                        capture_output=True, text=True)
    check(_name + " 语法合法", _p.returncode == 0)

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
