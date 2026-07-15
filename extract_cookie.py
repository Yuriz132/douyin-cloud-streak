#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地提取抖音登录态（state.json）
================================
用法（在你的 Windows / macOS 本机，有界面的电脑上运行）：
    1. 先安装依赖：  pip install -r requirements.txt
    2. 运行本脚本：  python extract_cookie.py
       （或双击仓库里的「1.本地提取通行证.bat」）
    3. 浏览器会自动打开抖音，请用「扫码 / 短信验证码」登录你的账号
    4. 登录成功后，回到终端按回车键，脚本会把登录态保存为 state.json

⚠️ 重要说明
-----------
- 之所以要「在本机」提取，是因为抖音对「境外机房 IP + 异地登录」
  风控很严；你本机（家庭 / 手机网络）的环境更自然，能正常弹出二维码。
- state.json 里包含 Cookie / 登录凭证，属于敏感文件，
  千万不要提交到 Git 仓库（已在 .gitignore 中屏蔽）。
- 服务器上只放这个 state.json，不需要再登录，脚本直接复用登录态。
"""

import os
import sys
from playwright.sync_api import sync_playwright


def main():
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

    print("==================================================")
    print("  抖音登录态提取工具（本地 · 有界面模式）")
    print("==================================================")
    print("将打开一个浏览器窗口，请在其中登录抖音。")
    print("登录完成后，回到本窗口按【回车键】保存登录态。")
    print("--------------------------------------------------")

    with sync_playwright() as p:
        # 本地用有头模式，方便你扫码/输验证码
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.goto("https://www.douyin.com", wait_until="domcontentloaded")
        print("✅ 浏览器已打开，请登录……")

        try:
            input("登录完成后按回车键保存登录态 >>> ")
        except (EOFError, KeyboardInterrupt):
            print("\n❌ 已取消。未保存任何文件。")
            browser.close()
            sys.exit(1)

        # 保存登录态（cookie + localStorage）
        context.storage_state(path=out_path)
        browser.close()

    if os.path.exists(out_path):
        size = os.path.getsize(out_path)
        print(f"✅ 登录态已保存到：{out_path}  （大小 {size} 字节）")
        print("下一步：把该 state.json 上传到服务器 /opt/douyin-cloud-streak/")
        print("       可运行仓库里的 sync_state_safe.sh 一键同步。")
    else:
        print("❌ 保存失败，未生成 state.json。")


if __name__ == "__main__":
    main()
