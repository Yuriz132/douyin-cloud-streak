#!/usr/bin/env bash
set -euo pipefail

echo "======================================================"
echo "    🔥 抖音云端自动续火花助手 · Linux 一键安装部署"
echo "======================================================"

if [ "$(id -u)" -ne 0 ]; then
  echo "[错误] 请以 root 用户运行：sudo bash deploy/deploy.sh"
  exit 1
fi

SERVICE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$SERVICE_DIR/.venv"
UNIT_SRC="$SERVICE_DIR/deploy/douyin-cloud-streak.service"
UNIT_DST="/etc/systemd/system/douyin-cloud-streak.service"

echo "==> 1. 安装系统依赖..."
if command -v apt-get &>/dev/null; then
    apt-get update -y
    apt-get install -y python3 python3-venv python3-pip libnss3 libnspr4 libasound2 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2t64 2>/dev/null || apt-get install -y python3 python3-venv python3-pip
elif command -v dnf &>/dev/null; then
    dnf install -y python3 python3-pip
elif command -v yum &>/dev/null; then
    yum install -y python3 python3-pip
fi

echo "==> 2. 创建 Python 虚拟环境..."
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi

echo "==> 3. 安装 Python 依赖..."
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$SERVICE_DIR/requirements.txt"

echo "==> 4. 安装 Playwright Chromium 浏览器内核与依赖..."
"$VENV/bin/playwright" install --with-deps chromium || "$VENV/bin/playwright" install chromium

echo "==> 5. 检查并配置 2G 交换空间 (Swap，防止小内存服务器被系统 OOM)..."
if ! swapon --show | grep -q 'swap'; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "[✓] 2G Swap 已成功创建并启用"
else
  echo "[✓] 检测到已有 Swap 空间，跳过"
fi

echo "==> 6. 设置时区为 Asia/Shanghai..."
timedatectl set-timezone Asia/Shanghai 2>/dev/null || true

echo "==> 7. 配置访问令牌 (Token)..."
if [ ! -f "$SERVICE_DIR/.env" ]; then
  TOKEN="$(head -c 24 /dev/urandom | sha256sum | head -c 24)"
  cat > "$SERVICE_DIR/.env" <<EOF
AUTH_TOKEN=$TOKEN
PORT=8000
HOST=0.0.0.0
TZ=Asia/Shanghai
EOF
fi

TOKEN_VALUE="$(grep '^AUTH_TOKEN=' "$SERVICE_DIR/.env" | cut -d= -f2- | tr -d '\r\n')"
if [ -z "$TOKEN_VALUE" ]; then
  TOKEN_VALUE="$(head -c 24 /dev/urandom | sha256sum | head -c 24)"
  sed -i "s/^AUTH_TOKEN=.*/AUTH_TOKEN=$TOKEN_VALUE/" "$SERVICE_DIR/.env"
fi

echo "==> 8. 注册并启动 systemd 开机自启服务..."
systemctl stop douyin-spark 2>/dev/null || true
systemctl disable douyin-spark 2>/dev/null || true
systemctl stop douyin-cloud-streak 2>/dev/null || true
pkill -9 -f "python.*app.py" 2>/dev/null || true
fuser -k -9 8000/tcp 2>/dev/null || true
sed "s|__DIR__|$SERVICE_DIR|g; s|__VENV__|$VENV|g" "$UNIT_SRC" > "$UNIT_DST"
systemctl daemon-reload
systemctl enable --now douyin-cloud-streak
systemctl restart douyin-cloud-streak
sleep 2

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -z "$IP" ]; then
  IP="你的服务器公网IP"
fi

echo ""
echo "======================================================"
echo "  🎉 恭喜！抖音云端续火花助手服务部署完成并已启动！"
echo "======================================================"
echo "Web 管理后台地址: http://$IP:8000"
echo "后台访问安全令牌: $TOKEN_VALUE"
echo "配置文件位置:     $SERVICE_DIR/.env"
echo "======================================================"
echo "【下一步操作】"
echo "1. 请在服务器安全组/防火墙中放行 8000 端口 (TCP)；"
echo "2. 在电脑上运行「1.本地提取通行证.bat」扫码获取登录态；"
echo "3. 运行「4.同步登录态到服务器.bat」上传通行证（或直接在网页后台上传）；"
echo "4. 浏览器访问 http://$IP:8000 输入令牌，勾选好友开启每日自动续火花！"
echo "======================================================"
