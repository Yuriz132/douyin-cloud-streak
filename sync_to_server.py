"""一键同步登录凭据 (state.json) 到云服务器工具

支持：
1. 密码明文/可见输入（支持复制粘贴，输入什么显示什么，绝不盲敲！）；
2. 自动检测并创建远程目录 /opt/douyin-cloud-streak/data；
3. SSH/SFTP 直连上传；
4. 发生异常时提供详细排查提示。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# 确保在 Windows 控制台下输出 UTF-8 正常
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
DATA_STATE = BASE_DIR / "data" / "state.json"
ROOT_STATE = BASE_DIR / "state.json"


def find_state_file() -> Path | None:
    if DATA_STATE.exists():
        return DATA_STATE
    if ROOT_STATE.exists():
        return ROOT_STATE
    return None


def print_banner():
    print("=" * 65)
    print("        🚀 抖音登录凭证 (state.json) 云服务器一键同步工具")
    print("=" * 65)


def main():
    print_banner()

    state_path = find_state_file()
    if not state_path:
        print("\n[❌ 错误] 本地未找到 state.json 登录凭证！")
        print("👉 请先双击运行「1.本地提取通行证.bat」扫码登录生成通行证。")
        input("\n按回车键退出...")
        sys.exit(1)

    print(f"[✓] 检测到本地登录凭据: {state_path} ({os.path.getsize(state_path)} 字节)\n")

    # 1. 交互式输入连接信息
    server_ip = input("👉 请输入云服务器公网 IP 地址 (例如 123.45.67.89): ").strip()
    if not server_ip:
        print("[❌ 错误] 服务器 IP 不能为空！")
        input("\n按回车键退出...")
        sys.exit(1)

    server_user = input("👉 请输入 SSH 登录用户名 [直接回车默认 root]: ").strip() or "root"
    port_input = input("👉 请输入 SSH 端口号 [直接回车默认 22]: ").strip()
    server_port = int(port_input) if port_input.isdigit() else 22
    remote_dir = input("👉 请输入服务器部署路径 [直接回车默认 /opt/douyin-cloud-streak]: ").strip() or "/opt/douyin-cloud-streak"

    # 明文可见输入密码！
    print("-" * 65)
    password = input("🔑 请输入服务器密码 (明文直接可见，支持右键直接粘贴): ").strip()
    print("-" * 65)

    if not password:
        print("[❌ 错误] 服务器密码不能为空！")
        input("\n按回车键退出...")
        sys.exit(1)

    print(f"\n[*] 正在连接到服务器 {server_user}@{server_ip}:{server_port} ...")

    try:
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        ssh.connect(
            hostname=server_ip,
            port=server_port,
            username=server_user,
            password=password,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15
        )

        print("[✓] SSH 连接认证成功！")

        # 确保远程目录存在
        remote_data_dir = f"{remote_dir.rstrip('/')}/data"
        remote_target_file = f"{remote_data_dir}/state.json"

        print(f"[*] 正在确保远程目录存在: {remote_data_dir}")
        stdin, stdout, stderr = ssh.exec_command(f"mkdir -p '{remote_data_dir}'")
        stdout.channel.recv_exit_status()

        # SFTP 上传
        print(f"[*] 正在上传 {state_path.name} -> {remote_target_file} ...")
        sftp = ssh.open_sftp()
        sftp.put(str(state_path), remote_target_file)
        sftp.close()
        ssh.close()

        print("\n" + "=" * 65)
        print("  🎉🎉 恭喜！抖音登录凭证已成功同步上传至云服务器！")
        print(f"  [✓] 远程文件位置: {remote_target_file}")
        print("=" * 65)
        print("\n【下一步推荐】")
        print(f"1. 浏览器打开管理后台: http://{server_ip}:8000")
        print("2. 在「好友与消息」页面点击「一键勾选火花好友」并保存；")
        print("3. 云服务器现在开始将在每天定时为您自动续火花！")
        print("=" * 65)

    except Exception as e:
        err_msg = str(e)
        print(f"\n[❌ 上传失败] 错误详情: {err_msg}")
        print("\n【排查建议】")
        if "Authentication failed" in err_msg or "password" in err_msg.lower():
            print("❌ 密码错误：请仔细核对您的服务器 root 密码是否正确。")
        elif "timed out" in err_msg or "timeout" in err_msg.lower():
            print(f"❌ 连接超时：请检查云服务器安全组是否放行了 {server_port} (TCP) 端口。")
        else:
            print("❌ 其他错误：请检查 IP 或网络状态。")
        print(f"\n💡 备选方案：您也可以直接用电脑浏览器打开 http://{server_ip}:8000 在设置页直接上传 state.json。")

    input("\n按回车键退出...")


if __name__ == "__main__":
    main()
