"""抖音通行证 / 登录态 (state.json) 本地提取工具

在你的个人电脑（Windows / macOS）上有界面环境下运行：
1. 启动 Chromium 浏览器打开抖音；
2. 你使用手机抖音 App 扫码登录（或短信登录）；
3. 登录成功后，脚本自动检测并导出 state.json 凭证文件。

此凭证用于在云端/桌面端复用登录态，免去在机房环境直接登录触发的风控异常。
"""

from __future__ import annotations

import os
import shutil
import sys
import time
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


def print_banner():
    print("=" * 65)
    print("        🔑 抖音登录通行证 (state.json) 快速提取工具")
    print("=" * 65)
    print("【使用提示】")
    print("1. 稍后会自动弹出一个浏览器窗口并打开抖音官网；")
    print("2. 请在弹出的网页右上角点击「登录」，使用【手机抖音 App 扫码】或【短信】登录；")
    print("3. 登录成功后，脚本会自动检测并保存通行证（也可回到本窗口按回车保存）。")
    print("=" * 65 + "\n")


def main():
    print_banner()
    DATA_STATE.parent.mkdir(parents=True, exist_ok=True)

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
        logged_in = False

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
            print("\n【下一步推荐操作】")
            print("👉 方案 A (本地电脑运行):")
            print("   双击「2.桌面端立即运行.bat」，或者双击「3.启动管理后台.bat」")
            print("\n👉 方案 B (云服务器运行):")
            print("   1. 首次部署：双击运行「5.一键部署整站到服务器.bat」")
            print("   2. 日常维护：若后续服务器登录凭证过期，重新点击「1.本地提取通行证.bat」提取；")
            print("      提取完成后，只用双击运行「4.同步登录态到服务器.bat」上传登录凭证即可！")
            print("=" * 65)
            browser.close()
            return
        else:
            print("\n[❌ 超时] 5分钟内未检测到成功登录，请重新运行本工具。")
            browser.close()
            sys.exit(1)


if __name__ == "__main__":
    main()
