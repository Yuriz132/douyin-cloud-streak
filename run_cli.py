"""抖音云端/本地自动续火花 · 命令行/计划任务执行入口 (CLI & Cron Mode)

用法：
    python run_cli.py                     # 执行一次续火花（发送给已勾选好友）
    python run_cli.py --dry-run           # 模拟运行（不实际发消息，仅测试流程）
    python run_cli.py --sync-contacts     # 仅同步联系人列表到台账
    python run_cli.py --auto-spark        # 自动勾选并发送所有已有火花的好友
    python run_cli.py --friends "张三,李四" # 指定发送给特定好友
    python run_cli.py --headed            # 有头模式（弹出浏览器窗口，便于观察）
"""

from __future__ import annotations

import argparse
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

# 确保项目根目录在 sys.path 中
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from core.automation import fetch_chat_contacts, run_send, sync_contacts
from core.config import DEFAULT_ACCOUNT_ID, account_state_path, load_config, save_config
from core.ledger import (
    get_selected,
    load_ledger,
    merge_consumer_contacts,
    set_selected,
)
from core.runtime import load_runtime, record_contacts, record_run, setup_logging
from core.accounts import list_accounts


def sync_local_state(account_id: str | None = None) -> bool:
    """如果根目录下存在 state.json，自动同步到 data/state.json（仅默认账号）。"""
    aid = account_id or DEFAULT_ACCOUNT_ID
    root_state = BASE_DIR / "state.json"
    data_state = account_state_path(aid)
    data_state.parent.mkdir(parents=True, exist_ok=True)

    if aid == DEFAULT_ACCOUNT_ID and root_state.exists() and not data_state.exists():
        shutil.copy2(root_state, data_state)
        print(f"[✓] 已自动将根目录 {root_state.name} 导入到 {data_state}")
    return data_state.exists()


def parse_args():
    parser = argparse.ArgumentParser(description="抖音自动续火花 CLI 运行工具")
    parser.add_argument("--dry-run", action="store_true", help="模拟运行模式（不真正点击发送）")
    parser.add_argument("--sync-contacts", action="store_true", help="同步抖音聊天列表联系人到本地台账")
    parser.add_argument("--auto-spark", action="store_true", help="自动勾选所有当前带火花的好友")
    parser.add_argument("--friends", type=str, default="", help="临时指定好友昵称（英文逗号隔开）")
    parser.add_argument("--headed", action="store_true", help="弹出浏览器窗口运行（默认无头后台运行）")
    parser.add_argument("--account", type=str, default="", help="指定账号 ID（默认账号，可用 --list-accounts 查看）")
    parser.add_argument("--list-accounts", action="store_true", help="列出全部账号")
    return parser.parse_args()


def print_banner():
    print("=" * 60)
    print("      🔥 抖音自动续火花助手 · CLI 极简运行引擎")
    print("      Douyin Cloud Streak - Automation Runner")
    print("=" * 60)


def auto_select_sparking_friends(account_id: str | None = None) -> list[dict]:
    """如果未勾选好友，自动扫描台账中带火花的好友并设为已勾选。"""
    ledger_data = load_ledger(account_id)
    sparking = [x for x in ledger_data if x.get("streak_days", 0) > 0]
    if sparking:
        for x in ledger_data:
            x["selected"] = x.get("streak_days", 0) > 0
        set_selected(ledger_data, account_id)
        return [x for x in ledger_data if x.get("selected")]
    return []


def main():
    args = parse_args()
    logger = setup_logging()
    print_banner()

    account_id = (args.account or "").strip() or DEFAULT_ACCOUNT_ID

    if args.list_accounts:
        print("\n[*] 当前配置的账号列表：")
        for a in list_accounts():
            mark = "★" if a["is_default"] else " "
            print(f"  {mark} {a['id']:12s} {a['name']}  启用={a['enabled']}  state={'✓' if a['state_file_exists'] else '✗'}")
        return

    # 1. 检查凭据
    if not sync_local_state(account_id):
        print("\n[❌ 错误] 未检测到登录凭证 state.json！")
        print("👉 请先运行「1.本地提取通行证.bat」或执行 `python extract_cookie.py` 扫码登录，")
        print(f"   或为账号 {account_id} 上传登录态。")
        sys.exit(1)

    # 2. 如果指定了仅同步联系人
    if args.sync_contacts:
        print("\n[*] 正在启动浏览器同步抖音联系人列表...")
        res = sync_contacts(account_id)
        if res.get("error"):
            print(f"[❌ 同步失败] {res['error']}")
            sys.exit(1)
        names = res.get("names", [])
        stats = merge_consumer_contacts(names, account_id)
        record_contacts(res, account_id)
        print(f"[✓] 同步成功！共获取联系人 {len(names)} 人。")
        print(f"    - 新增: {stats.get('added', 0)} 人")
        print(f"    - 更新: {stats.get('updated', 0)} 人")
        print(f"    - 当前有火花: {stats.get('sparking', 0)} 人")
        return

    # 3. 解析目标好友
    only_names = None
    if args.friends.strip():
        only_names = [x.strip() for x in args.friends.split(",") if x.strip()]
        print(f"[*] 已指定临时发送目标 ({len(only_names)} 人): {', '.join(only_names)}")
    else:
        cfg = load_config(account_id)
        selected = get_selected(account_id)

        # 尝试从 config.json 的 friends 迁移
        if not selected and cfg.get("friends"):
            from core.ledger import import_config_friends
            import_config_friends(cfg["friends"], account_id)
            selected = get_selected(account_id)

        # 智能检查：如果台账中有联系人但都没勾选，自动检测是否有带火花的好友
        if not selected:
            ledger_data = load_ledger(account_id)
            sparking = [x for x in ledger_data if x.get("streak_days", 0) > 0]
            if sparking or args.auto_spark:
                print(f"\n[💡 智能识别] 检测到台账中有 {len(ledger_data)} 位联系人，其中 {len(sparking)} 位当前有火花标记：")
                for idx, item in enumerate(sparking, 1):
                    try:
                        print(f"    {idx}. {item.get('display_name')} (🔥 {item.get('streak_days')} 天)")
                    except Exception:
                        print(f"    {idx}. 好友 (🔥 {item.get('streak_days')} 天)")

                # 自动为用户勾选这些带火花的好友
                selected = auto_select_sparking_friends(account_id)
                print(f"[✓] 已自动为您勾选这 {len(selected)} 位火花好友！")

        if not selected:
            print("\n[⚠️ 提示] 台账中未勾选任何好友，且未检测到带火花的好友！")
            print("👉 建议双击「3.启动管理后台.bat」进入网页端勾选好友，")
            print("   或者在 config.json 填入好友昵称。")
            sys.exit(1)

        print(f"\n[*] 准备向已选中的 {len(selected)} 位好友发送火花...")
        for idx, entry in enumerate(selected, 1):
            spark = f" (🔥 {entry.get('streak_days', 0)}天)" if entry.get('streak_days') else ""
            try:
                print(f"    {idx}. {entry.get('display_name')}{spark}")
            except Exception:
                print(f"    {idx}. 好友{spark}")

    # 4. 开始发送
    mode_str = "【模拟演练 (Dry Run)】" if args.dry_run else "【正式发送】"
    print(f"\n[*] 任务启动中... 当前模式: {mode_str} (账号 {account_id})")
    start_time = time.time()

    res = run_send(dry_run=args.dry_run, only_names=only_names, account_id=account_id)
    record_run(res, account_id)
    elapsed = time.time() - start_time

    # 5. 输出总结
    print("\n" + "=" * 60)
    print("                    📊 本次运行报告")
    print("=" * 60)
    print(f"耗时时间: {elapsed:.1f} 秒")
    print(f"成功发送: {len(res.get('ok', []))} 人 -> {', '.join(res.get('ok', [])) if res.get('ok') else '无'}")

    if res.get("failed"):
        print(f"\n[⚠️ 失败名单 ({len(res.get('failed'))} 人)]:")
        for item in res.get("failed"):
            print(f"  - {item.get('name')}: {item.get('reason')}")

    if res.get("skipped"):
        print(f"\n[ℹ️ 跳过名单 ({len(res.get('skipped'))} 人)]:")
        for item in res.get("skipped"):
            print(f"  - {item.get('name')}: {item.get('reason')}")

    if res.get("rate_limited"):
        print("\n[🚨 警报] 触发了抖音限流保护/滑动验证，脚本已自动提前安全熔断！")

    if res.get("logged_out"):
        print("\n[❌ 登录失效] 登录凭据已过期，请重新运行 `python extract_cookie.py` 扫码！")
        sys.exit(2)

    print("=" * 60)
    if res.get("failed") and not res.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
