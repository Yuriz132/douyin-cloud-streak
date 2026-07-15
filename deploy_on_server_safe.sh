#!/bin/bash
# =============================================================================
# 抖音云端续火花 · 安全隔离部署脚本 (Ubuntu 24.04 LTS)
# 设计原则：绝对不影响服务器上已有的其他项目（例如你正在运行的其他网站/服务）
#   - 独立部署目录 /opt/douyin-cloud-streak
#   - 独立 Python 虚拟环境 venv
#   - 独立 crontab 任务（只新增，不覆盖/不删除任何已有任务）
#   - 仅安装最小必要系统依赖，绝不升级/卸载现有软件包
# =============================================================================
set -euo pipefail

APP_DIR="/opt/douyin-cloud-streak"
DEPLOY_ZIP="/root/douyin-cloud-streak-deploy.zip"
RUN_USER="${SUDO_USER:-$USER}"

echo "============================================================"
echo " 抖音云端续火花 · 安全隔离部署"
echo " 部署目录 : $APP_DIR   (独立目录，绝不触碰其他项目)"
echo " 运行用户 : $RUN_USER"
echo " 时间     : $(date)"
echo "============================================================"

# ---------- 0. 安全检查：不与现有目录冲突 ----------
if [ -d "$APP_DIR" ] && [ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]; then
  echo "⚠️  $APP_DIR 已存在且非空，停止执行以免覆盖数据！"
  echo "    如需重装，请先手动备份/清理该目录。"
  exit 1
fi

# ---------- 0.5 内存不足时创建 swap（防 Chromium OOM） ----------
MEM_TOTAL_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
SWAP_EXISTS=$(swapon --show 2>/dev/null | wc -l)
if [ "$MEM_TOTAL_KB" -lt 2097152 ] && [ "$SWAP_EXISTS" -eq 0 ]; then
  echo "==> 内存 < 2GB 且无 swap，创建 2GB swap 文件防 OOM ..."
  fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
  echo "✅ swap 已创建并启用"
fi

# ---------- 1. 需要 root 安装系统依赖 ----------
if [ "$EUID" -ne 0 ]; then
  echo "请使用 sudo 或以 root 执行： sudo bash $0"
  exit 1
fi

# ---------- 2. 仅安装最小必要系统包（不升级/不卸载现有包） ----------
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip unzip curl ca-certificates \
  fonts-liberation libnss3 libatk-bridge2.0-0 libxkbcommon0 \
  libgtk-3-0 libasound2t64 libgbm1 libxcomposite1 libxdamage1 \
  libxfixes3 libxrandr2 libpango-1.0-0 libcairo2 libcups2

# ---------- 3. 创建独立部署目录 ----------
mkdir -p "$APP_DIR"

# ---------- 4. 解压部署包（必须已上传到 /root/） ----------
if [ ! -f "$DEPLOY_ZIP" ]; then
  echo "❌ 未找到 $DEPLOY_ZIP"
  echo "   请先把 douyin-cloud-streak-deploy.zip 上传到服务器 /root/ 后再执行本脚本。"
  exit 1
fi
unzip -o "$DEPLOY_ZIP" -d "$APP_DIR"
echo "✅ 已解压部署包 -> $APP_DIR"

# ---------- 5. 归属权交还给运行用户 ----------
if [ -n "${SUDO_USER:-}" ]; then
  chown -R "$SUDO_USER:$SUDO_USER" "$APP_DIR"
fi

# ---------- 6. 建立独立 Python 虚拟环境并装依赖 ----------
echo "==> 创建 venv 并安装依赖（不影响系统 Python）..."
sudo -u "$RUN_USER" bash -c "
  cd '$APP_DIR'
  python3 -m venv venv
  source venv/bin/activate
  pip install --upgrade pip
  pip install -r requirements.txt
  playwright install chromium
  playwright install-deps chromium
"
echo "✅ 依赖安装完成"

# ---------- 7. 设置中国时区（确保定时任务按本地时间执行） ----------
timedatectl set-timezone Asia/Shanghai || true
echo "时区: $(date)"

# ---------- 8. 写入独立 cron 任务（只新增，不覆盖已有任务） ----------
CRON_LINE="0 8 * * * cd $APP_DIR && $APP_DIR/venv/bin/python $APP_DIR/cloud_streak.py >> $APP_DIR/run.log 2>&1"
sudo -u "$RUN_USER" bash -c "
  ( crontab -l 2>/dev/null | grep -v 'cloud_streak.py'; echo \"$CRON_LINE\" ) | crontab -
"
echo "✅ 已写入 cron 任务:"

# ---------- 9. 收尾提示 ----------
echo ""
echo "============================================================"
echo " 部署完成！"
echo " 项目目录 : $APP_DIR"
echo " 验证 cron : crontab -l | grep cloud_streak"
echo " 手动试跑 : cd $APP_DIR && source venv/bin/activate && python cloud_streak.py"
echo " 查看日志 : tail -f $APP_DIR/run.log"
echo "============================================================"
