"""Docker 一键部署工具：上传代码 → 服务器 Docker 构建（国内源加速）→ compose 启动

与 deploy_to_server.py（systemd/venv 方式）二选一：
- 5.服务器部署.bat  → deploy_to_server.py        （系统 service + Python venv）
- 6.Docker部署.bat  → 本脚本                    （容器化，依赖已封装进镜像）

Docker 版在服务器上自动完成：
1. 安装 Docker（若未装，apt 走阿里云国内源 + 官方 aliyun docker-ce 源）
2. 配置 Docker Hub 镜像加速器（拉取 python 基础镜像提速）
3. docker compose build --build-arg USE_CN_MIRROR=1
   （构建时 apt/pip 走清华源、Playwright 走 npmmirror 国内镜像）
4. docker compose up -d 启动，数据目录挂载 ./data 不随容器删除
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
import tempfile
import time
from pathlib import Path

# 确保控制台 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent

# 敏感数据（登录态/配置/密钥/运维文档）与编译缓存绝不上传服务器，防止覆盖线上数据
IGNORE_NAMES = {".git", "__pycache__", ".venv", "venv", ".pytest_cache", ".idea", ".vscode", "data", ".env", "docs"}


def print_banner():
    print("=" * 65)
    print("        🐳 抖音云端自动续火花 · Docker 容器化一键部署工具")
    print("=" * 65)


def create_deploy_archive() -> Path:
    """通用全量打包：自动打包当前项目所有文件与目录（排除 data/.env 等敏感项）"""
    temp_tar = Path(tempfile.gettempdir()) / "douyin_cloud_streak_docker.tar.gz"

    print("[*] 正在全量打包项目文件 (源码 + Dockerfile + compose)...")
    with tarfile.open(temp_tar, "w:gz") as tar:
        for item in BASE_DIR.iterdir():
            if item.name in IGNORE_NAMES:
                continue

            def _filter(tarinfo):
                for ign in IGNORE_NAMES:
                    if f"/{ign}/" in f"/{tarinfo.name}/" or tarinfo.name == ign:
                        return None
                if tarinfo.name.endswith(".pyc") or tarinfo.name.endswith(".pid"):
                    return None
                return tarinfo

            tar.add(str(item), arcname=item.name, filter=_filter)

    size_kb = temp_tar.stat().st_size / 1024
    print(f"[✓] 打包完成！压缩包大小: {size_kb:.1f} KB\n")
    return temp_tar


REMOTE_SCRIPT = r"""set -e
cd "__REMOTE_DIR__"
mkdir -p data
export DEBIAN_FRONTEND=noninteractive

# ── 1. 检查/安装 Docker ──────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
    echo "==> 未检测到 Docker，开始安装（国内源）..."
    apt-get update -y -o Acquire::Retries=3 -o Acquire::http::Timeout=30
    apt-get install -y ca-certificates curl
    install -m 0755 -d /etc/apt/keyrings
    . /etc/os-release
    curl -fsSL "https://mirrors.aliyun.com/docker-ce/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc || true
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://mirrors.aliyun.com/docker-ce/linux/${ID} ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -y -o Acquire::Retries=3 -o Acquire::http::Timeout=30
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    echo "[✓] Docker 安装完成"
else
    echo "[✓] Docker 已安装: $(docker --version)"
fi

# ── 2. 配置 Docker Hub 镜像加速（拉 python 基础镜像提速）─────────────
if [ ! -f /etc/docker/daemon.json ]; then
    echo "==> 配置 Docker 镜像加速器..."
    mkdir -p /etc/docker
    cat > /etc/docker/daemon.json <<'JSON'
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://docker.mirrors.ustc.edu.cn",
    "https://mirror.baidubce.com"
  ]
}
JSON
    systemctl restart docker || true
    echo "[✓] 镜像加速器已配置（若拉取基础镜像仍慢，请改用阿里云容器镜像服务专属加速地址）"
fi

# ── 3. 构建镜像（国内源加速：清华 apt/pip + npmmirror Playwright）────
echo "==> 构建 Docker 镜像（USE_CN_MIRROR=1，国内源加速，首次需下载基础镜像与依赖）..."
docker compose build --build-arg USE_CN_MIRROR=1

# ── 4. 启动容器 ─────────────────────────────────────────────────────
echo "==> 启动容器..."
docker compose up -d
sleep 3
docker compose ps
"""


def sync_local_data(ssh, remote_dir: str) -> list[str]:
    """把本地登录态 (state.json) 与联系人台账 (ledger.json) 一并上传到服务器 data/。

    返回成功上传的文件名列表（可能为空，表示本地无数据可传）。
    """
    remote_data = f"{remote_dir.rstrip('/')}/data"
    stdin, stdout, stderr = ssh.exec_command(f"mkdir -p '{remote_data}'")
    stdout.channel.recv_exit_status()

    candidates = [
        ("state.json", BASE_DIR / "data" / "state.json", "登录态"),
        ("ledger.json", BASE_DIR / "data" / "ledger.json", "联系人台账"),
    ]

    uploaded: list[str] = []
    sftp = ssh.open_sftp()
    try:
        for fname, local_path, desc in candidates:
            if not local_path.exists() or local_path.stat().st_size == 0:
                continue
            remote_file = f"{remote_data}/{fname}"
            print(f"[*] 同步 {desc} {fname} -> {remote_file}")
            sftp.put(str(local_path), remote_file)
            uploaded.append(fname)
    finally:
        sftp.close()

    if uploaded:
        print("[*] 数据已上传，正在重启容器让台账生效...")
        stdin, stdout, stderr = ssh.exec_command(
            f"cd '{remote_dir}' && docker compose restart 2>/dev/null || true"
        )
        stdout.channel.recv_exit_status()
        print("[✓] 容器已重启，登录态与联系人信息已生效！")

    return uploaded


def main():
    print_banner()

    server_ip = input("👉 请输入云服务器公网 IP 地址 (例如 123.45.67.89): ").strip()
    if not server_ip:
        print("[❌ 错误] 服务器 IP 不能为空！")
        input("\n按回车键退出...")
        sys.exit(1)

    server_user = input("👉 请输入 SSH 用户名 [直接回车默认 root]: ").strip() or "root"
    port_input = input("👉 请输入 SSH 端口号 [直接回车默认 22]: ").strip()
    server_port = int(port_input) if port_input.isdigit() else 22
    remote_dir = input("👉 请输入部署路径 [直接回车默认 /opt/douyin-cloud-streak]: ").strip() or "/opt/douyin-cloud-streak"

    print("-" * 65)
    password = input("🔑 请输入服务器密码 (明文可见，支持右键直接粘贴): ").strip()
    print("-" * 65)

    if not password:
        print("[❌ 错误] 密码不能为空！")
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
            auth_timeout=15,
        )
        print("[✓] SSH 连接成功！\n")

        archive_path = create_deploy_archive()
        remote_tar = "/tmp/douyin_cloud_streak_docker.tar.gz"

        print(f"[*] 正在将打包文件上传至服务器 /tmp ...")
        sftp = ssh.open_sftp()
        sftp.put(str(archive_path), remote_tar)
        sftp.close()
        archive_path.unlink(missing_ok=True)
        print("[✓] 上传完成！\n")

        print("=" * 65)
        print("  [*] 正在远程执行 Docker 安装/构建/启动...")
        print("  [*] 首次构建需下载基础镜像与 Chromium 内核，耗时取决于服务器带宽")
        print("=" * 65)

        extract_cmd = f"mkdir -p '{remote_dir}' && tar --overwrite -xzf '{remote_tar}' -C '{remote_dir}' && rm -f '{remote_tar}'"
        stdin, stdout, stderr = ssh.exec_command(extract_cmd)
        stdout.channel.recv_exit_status()

        cmd = REMOTE_SCRIPT.replace("__REMOTE_DIR__", remote_dir)
        stdin, stdout, stderr = ssh.exec_command(cmd, get_pty=True)
        for line in iter(stdout.readline, ""):
            print(line, end="")

        exit_status = stdout.channel.recv_exit_status()
        if exit_status == 0:
            uploaded = sync_local_data(ssh, remote_dir)

            print("\n" + "=" * 65)
            print("  🐳🎉 Docker 部署完成！")
            print("=" * 65)
            print(f"🌐 Web 管理后台地址: http://{server_ip}:8000")
            print(f"🔑 访问令牌: 默认 spark_secret_token_change_me（请改 docker-compose.yml 的 AUTH_TOKEN 后 docker compose up -d 生效）")
            if uploaded:
                print(f"📦 已随部署同步: {uploaded}")
            print(f"📁 数据目录: {remote_dir}/data（容器删除数据不丢）")
            print("=" * 65)
        else:
            print(f"\n[⚠️ Docker 部署脚本返回状态码 {exit_status}] 请查看上方输出信息。")

        ssh.close()

    except Exception as e:
        print(f"\n[❌ 部署发生异常] {e}")

    input("\n按回车键退出...")


if __name__ == "__main__":
    main()
