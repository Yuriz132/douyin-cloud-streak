import time
import random
import os
import json
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# ============ 可靠性参数 ============
RETRY_GAP = 120      # 两次尝试之间的间隔（秒）
MAX_ATTEMPTS = 3     # 总共最多尝试次数（应对 00:01 瞬断等网络抖动）


def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"message": "🔥", "target_names": [], "max_count": 10}


def search_and_open(page, name):
    """通过私信搜索框查找并打开与 name 的会话；
    搜索结果点击可能进入用户主页，此时再点“发消息”进入会话。成功返回 True"""
    try:
        box = page.get_by_placeholder("搜索", exact=False).first
        if box.count() == 0:
            return False
        box.click()
        box.fill(name)
        time.sleep(5)  # 等搜索结果加载
        candidate = page.get_by_text(name, exact=False).first
        if candidate.count() == 0:
            return False
        candidate.click(force=True)
        time.sleep(4)
        # 是否已在会话（有输入框）？否则可能在用户主页，点“发消息”
        box_input = page.locator('div[contenteditable="true"]').first
        if box_input.count() == 0:
            send_btn = page.get_by_text("发消息", exact=False).first
            if send_btn.count() > 0:
                send_btn.click(force=True)
                time.sleep(4)
            else:
                return False
        return True
    except Exception as e:
        print(f"搜索打开 {name} 失败: {e}")
    return False


def safe_screenshot(page):
    """失败时截图，但加超时保护，避免卡住整个任务。"""
    try:
        path = os.path.join(os.path.dirname(__file__), "error_screenshot.png")
        page.screenshot(path=path, timeout=5000)
        print(f"已将当前页面截图保存为 error_screenshot.png")
    except Exception as e:
        print(f"截图也失败（可忽略）：{e}")


def run_attempt():
    """执行一次完整尝试，返回成功发送的消息条数。
    返回 0 表示在「真正发送前」就失败（通常是网络不通），此时外层会重试。"""
    print("----------------------------------------")

    state_path = os.path.join(os.path.dirname(__file__), "state.json")
    if not os.path.exists(state_path):
        print("❌ 致命错误：未找到 state.json")
        print("请先在有界面的电脑上运行 extract_cookie.py 登录抖音提取状态，然后再复制到云服务器。")
        return 0

    config = load_config()
    msg_text = config.get("message", "🔥")
    target_names = config.get("target_names", [])
    max_count = config.get("max_count", 10)

    success_count = 0

    with sync_playwright() as p:
        print("🤖 启动隐身无头浏览器...")
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox"
            ]
        )
        context = browser.new_context(
            storage_state=state_path,
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        Stealth().apply_stealth_sync(page)

        print("🌐 正在访问抖音私信页面...")
        goto_ok = False
        for attempt in range(3):
            try:
                page.goto("https://www.douyin.com/chat", timeout=60000, wait_until="domcontentloaded")
                goto_ok = True
                break
            except Exception as e:
                print(f"⚠️ 第{attempt+1}次打开页面失败，重试... ({str(e)[:60]})")
                time.sleep(5)
        if not goto_ok:
            print("❌ 无法打开抖音页面，可能当前网络到 douyin.com 不通")
            safe_screenshot(page)
            browser.close()
            return 0

        try:
            print("⏳ 等待页面渲染和数据加载(强制等待 15 秒以防元素变化)...")
            time.sleep(15)
            page.wait_for_selector("img", timeout=5000)
            print("✅ 页面结构已加载！")
        except Exception as e:
            print("❌ 加载联系人超时！可能是 Cookie 已过期，或者你的服务器 IP 被抖音强制弹出验证码。")
            print("建议重新在本地提取一次 Cookie 或者更换服务器 IP。")
            safe_screenshot(page)
            browser.close()
            return 0

        time.sleep(random.uniform(2, 4))

        def send_current(name):
            nonlocal success_count
            input_box = page.locator('div[contenteditable="true"]').first
            if input_box.count() > 0:
                input_box.click()
                time.sleep(0.5)
                page.keyboard.type(msg_text, delay=100)
                time.sleep(1)
                page.keyboard.press("Enter")
                time.sleep(1)
                success_count += 1
                print(f"💌 ✅ 已向 {name} 发送续火花消息")
            else:
                print(f"❌ 未找到 {name} 的聊天输入框")

        # 如果指定了目标，只发给目标
        if len(target_names) > 0:
            for name in target_names:
                print(f"🔍 寻找好友: {name}")
                contact = page.get_by_text(name, exact=False).first
                if contact.count() > 0:
                    contact.click(force=True)
                    print(f"👉 已点击好友: {name}，等待聊天框展开...")
                    time.sleep(random.uniform(2, 4))
                    send_current(name)
                else:
                    print(f"列表未直接找到 {name}，尝试搜索...")
                    if search_and_open(page, name):
                        print(f"👉 已通过搜索打开: {name}")
                        time.sleep(random.uniform(2, 4))
                        send_current(name)
                    else:
                        print(f"❌ 未找到好友: {name}，可能需要确认昵称是否正确")
                time.sleep(random.uniform(2, 5))
        # 如果没有指定目标，按顺序发前 N 个
        else:
            print("🔍 未指定好友，准备按列表顺序发送...")
            images = page.locator('img').all()
            contacts_found = []
            for img in images:
                box = img.bounding_box()
                if box and box['x'] < 400 and box['width'] > 20:
                    contacts_found.append(img)

            limit = min(len(contacts_found), max_count)
            for i in range(limit):
                contact = contacts_found[i]
                contact.click(force=True)
                print(f"👉 已点击第 {i+1} 个好友")
                time.sleep(random.uniform(2, 4))
                send_current(f"第 {i+1} 个好友")
                time.sleep(random.uniform(2, 5))

        browser.close()
        return success_count


def run():
    print("========================================")
    print("  抖音全自动续火花 - 云端无头模式版")
    print("========================================")
    print(f"📄 配置信息:")
    cfg = load_config()
    print(f"发送内容: {cfg.get('message', '🔥')}")
    print(f"指定目标: {cfg.get('target_names', []) if cfg.get('target_names') else '未指定(将按顺序发送)'}")
    print(f"最大数量: {cfg.get('max_count', 10)}")
    print("----------------------------------------")

    sent = 0
    for i in range(MAX_ATTEMPTS):
        try:
            sent = run_attempt()
        except Exception as e:
            print(f"⚠️ 第 {i+1} 次尝试发生异常: {e}")
            sent = 0

        if sent > 0:
            print(f"✅ 本次成功发送 {sent} 条，结束。")
            break

        if i < MAX_ATTEMPTS - 1:
            print(f"⚠️ 第 {i+1} 次未成功发送（疑似网络瞬断），{RETRY_GAP} 秒后自动重试"
                  f"（第 {i+2}/{MAX_ATTEMPTS} 次）...")
            time.sleep(RETRY_GAP)

    else:
        print(f"❌ 已重试 {MAX_ATTEMPTS} 次仍失败，请检查服务器网络或重新提取 state.json。")

    print("========================================")
    print(f"🎉 任务结束，本次共发送了 {sent} 个续火花消息！")
    print("========================================")


if __name__ == "__main__":
    run()
