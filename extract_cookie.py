"""抖音本地一键登录 + 联系人同步 + 启动本地后台工具

在你的个人电脑（Windows / macOS）上有界面环境下运行：
1. 启动 Chromium 浏览器打开抖音；
2. 你使用手机抖音 App 扫码登录（或短信登录）；
3. 登录成功后，脚本自动：
   - 检测并导出 state.json 凭证文件；
   - 自动同步联系人列表到本地台账（无需在服务器上再同步）；
   - 自动启动本地 Web 后台并打开 http://127.0.0.1:8000。

此凭证用于在云端/桌面端复用登录态，免去在机房环境直接登录触发的风控异常。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

# 确保在 Windows 控制台下输出 emoji 与特殊字符不崩溃
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
DATA_STATE = BASE_DIR / "data" / "state.json"
ROOT_STATE = BASE_DIR / "state.json"
LOCAL_PORT = int(os.environ.get("LOCAL_PORT", "8000"))


def print_banner():
    print("=" * 65)
    print("        🔑 抖音本地一键登录 · 同步联系人 · 启动后台")
    print("=" * 65)
    print("【使用提示】")
    print("1. 稍后会自动弹出一个浏览器窗口并打开抖音官网；")
    print("2. 请在弹出的网页右上角点击「登录」，使用【手机抖音 App 扫码】或【短信】登录；")
    print("3. 登录成功后，脚本会自动完成：")
    print("   - 保存登录通行证 (state.json)")
    print("   - 自动同步联系人到本地台账")
    print("   - 自动启动本地后台并打开 http://127.0.0.1:8000")
    print("=" * 65 + "\n")


def _port_open(port: int = 8000) -> bool:
    s = socket.socket()
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def auto_sync_contacts() -> None:
    """登录成功后自动同步联系人到本地台账（无头快速完成）。"""
    sys.path.insert(0, str(BASE_DIR))
    from core.automation import sync_contacts
    from core.executor import executor
    from core.browser import shutdown_pool
    from core.ledger import merge_consumer_contacts
    from core.config import DEFAULT_ACCOUNT_ID

    print("\n[*] 登录成功，正在自动同步联系人到本地台账...")
    print("    (无头浏览器后台运行，通常 1~3 分钟，请耐心等待)\n")

    try:
        res = executor.submit_and_wait(lambda: sync_contacts(DEFAULT_ACCOUNT_ID))
    except Exception as e:
        print(f"[⚠️ 联系人同步异常] {e}")
        res = {"error": str(e)}
    finally:
        # 必须先在工作线程内回收浏览器池（关闭 Chromium 与 playwright 驱动），
        # 再停掉工作线程，否则主进程退出时 playwright node 驱动会因管道
        # 断裂抛出 EPIPE 堆栈刷屏。
        try:
            executor.submit_and_wait(shutdown_pool)
        except Exception:
            pass
        executor.shutdown()

    if res.get("error"):
        print(f"[⚠️ 联系人同步失败] {res['error']}")
        return

    names = res.get("names", [])
    stats = merge_consumer_contacts(names, DEFAULT_ACCOUNT_ID)
    print(f"[✓] 联系人同步完成！共 {len(names)} 人")
    print(f"    - 新增: {stats.get('added', 0)} 人")
    print(f"    - 更新: {stats.get('updated', 0)} 人")

    # 打印台账火花统计，便于当场核对提取质量（有火花好友是否被识别）
    from core.ledger import load_ledger
    entries = load_ledger(DEFAULT_ACCOUNT_ID)
    sparking = [e for e in entries if (e.get("streak_days") or 0) > 0]
    print(f"[i] 本地台账共 {len(entries)} 人，其中 {len(sparking)} 人带火花标记")
    if sparking:
        top = sorted(sparking, key=lambda e: e.get("streak_days") or 0, reverse=True)[:8]
        for e in top:
            print(f"    - {e.get('display_name')} (🔥 {e.get('streak_days')} 天)")
    else:
        print("    [⚠️ 台账中无人带火花。若手机 App 上明确显示有火花好友，")
        print("     说明抖音网页聊天端可能不展示火花标记，可在本地后台手动勾选好友，")
        print("     发送时会通过会话页文案自动校准火花天数。]")


def _find_free_port(start: int) -> int:
    """从 start 起找第一个空闲端口（最多探测 50 个）。"""
    port = start
    for _ in range(50):
        if not _port_open(port):
            return port
        port += 1
    return start


def start_local_web() -> str:
    """启动本地 Web 后台并打开浏览器。

    端口被占用时（常见原因：上一次运行的后台还在，或另一个解压目录的
    后台实例未关闭），自动换下一个端口启动本目录实例，避免误开旧后台
    导致"台账有数据、网页端却看不到"。
    """
    port = LOCAL_PORT
    if _port_open(LOCAL_PORT):
        port = _find_free_port(LOCAL_PORT + 1)
        print(f"[⚠️ 提示] 端口 {LOCAL_PORT} 已被占用（可能是上次运行的后台未关闭），"
              f"本次后台改用端口 {port}。")

    print("\n[*] 正在启动本地 Web 后台...")
    env = dict(os.environ, PORT=str(port))
    proc = subprocess.Popen(
        [sys.executable, str(BASE_DIR / "app.py")],
        cwd=str(BASE_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    for _ in range(30):
        if _port_open(port):
            webbrowser.open(f"http://127.0.0.1:{port}")
            return f"本地后台已启动，已打开 http://127.0.0.1:{port}（数据目录: {BASE_DIR / 'data'}）"
        time.sleep(0.5)

    return f"本地后台启动中 (进程 {proc.pid})，若浏览器未自动打开，请手动访问 http://127.0.0.1:{port}"


def main():
    print_banner()
    DATA_STATE.parent.mkdir(parents=True, exist_ok=True)

    logged_in = False

    print("[*] 正在启动本地浏览器，请稍候...")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"]
            )
        except Exception as e:
            print(f"\n[❌ 启动浏览器失败] {e}")
            print("👉 如果是第一次运行，请先在终端执行: playwright install chromium")
            sys.exit(1)

        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[⚠️ 提示] 打开主页稍慢，请直接在弹出的浏览器中操作: {e}")

        print("[*] 浏览器已就绪，请在页面中登录你的抖音账号...")
        print("    (登录成功后脚本将自动识别，最长等待 5 分钟)\n")

        deadline = time.time() + 300

        while time.time() < deadline:
            cookies = context.cookies()
            has_session = any(c["name"].startswith("sessionid") for c in cookies)

            if has_session:
                logged_in = True
                break
            time.sleep(1.5)

        if logged_in:
            time.sleep(2)  # 等待 Cookie 完全落盘
            context.storage_state(path=str(DATA_STATE))
            # 同时也复制一份到根目录备用
            shutil.copy2(DATA_STATE, ROOT_STATE)

            print("\n" + "=" * 65)
            print("  🎉 恭喜！抖音登录态提取成功！")
            print(f"  [✓] 凭据已保存至: {DATA_STATE}")
            print(f"  [✓] 副本已保存至: {ROOT_STATE}")
            print("=" * 65)
        else:
            print("\n[❌ 超时] 5分钟内未检测到成功登录，请重新运行本工具。")
            browser.close()
            sys.exit(1)

        # with 块内退出前关闭扫码浏览器
        browser.close()

    # 退出 sync_playwright 后再启动无头同步与本地后台，避免浏览器实例冲突
    if logged_in:
        # 自动同步联系人到本地台账
        auto_sync_contacts()

        # 自动启动本地后台并打开链接
        msg = start_local_web()
        print(f"\n[✓] {msg}")

        print("\n【下一步推荐操作】")
        print("👉 部署到云服务器 (可选):")
        print("   双击运行「2.上传本地文件加服务器部署.bat」（输入 2），即可把代码连同登录态、联系人信息")
        print("   一并同步到服务器，并获得公网访问链接。")
        print("=" * 65)


if __name__ == "__main__":
    main()
