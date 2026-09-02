"""离线验证浏览器池的 Playwright 驱动单例（不启动浏览器，仅验证驱动生命周期）。

背景 bug：用户登录第二个账号后「立即续火花」秒弹失败，
日志 "It looks like you are using Playwright Sync API inside the asyncio loop."
根因：sync_playwright().start() 会在当前线程留下一个运行中的 asyncio loop，
同一线程第二次 start() 必然报错；旧实现按账号（池 key）各自 start 驱动。

运行：python3 tests/test_browser_pool.py
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import browser

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    print(("PASS" if cond else "FAIL"), name)
    PASS += cond
    FAIL += (not cond)


# 确保测试从干净状态开始（若上次运行残留驱动则先释放）
if browser._PW is not None:  # noqa: SLF001
    browser.shutdown_pool()

# ── 1. 同一线程连续获取：只启动一次，不触发 asyncio loop 报错 ──
p1 = browser._ensure_playwright()
check("B1 首次获取驱动成功", p1 is not None)
try:
    p2 = browser._ensure_playwright()
    check("B2 二次获取不报 asyncio loop 错误", p2 is p1)
except Exception as e:
    check(f"B2 二次获取不报 asyncio loop 错误 (异常: {e})", False)

# ── 2. 多线程并发首次调用：竞态下仍只启动一个驱动 ──────────────
results = {}


def get():
    results[threading.get_ident()] = browser._ensure_playwright()


threads = [threading.Thread(target=get) for _ in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("B3 多线程返回同一驱动实例", len({id(v) for v in results.values()}) == 1)

# ── 3. 释放后可重建（进程退出回收路径）─────────────────────────
browser.shutdown_pool()
check("B4 shutdown 后驱动已释放", browser._PW is None)  # noqa: SLF001
p3 = browser._ensure_playwright()
check("B5 shutdown 后可重建", p3 is not None)
check("B6 重建后仍是单例", browser._ensure_playwright() is p3)

# ── 4. _new_pool_entry 不再启动第二个驱动（用假 launch 验证）───
launched = []
orig_launch = p3.chromium.launch


class FakeBrowser:
    def __init__(self):
        self.closed = False

    def is_connected(self):
        return not self.closed

    def close(self):
        self.closed = True


p3.chromium.launch = lambda headless=True, args=None: (launched.append(1) or FakeBrowser())
e1 = browser._new_pool_entry()
e2 = browser._new_pool_entry()
check("B7 多 key 建池: 每个 key 一个浏览器", e1["browser"] is not e2["browser"])
check("B8 多 key 建池: 驱动只启动一次", browser._PW is p3)  # noqa: SLF001
p3.chromium.launch = orig_launch

# 清理
browser.shutdown_pool()

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
