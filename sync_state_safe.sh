#!/bin/bash
# =============================================================================
# 同步 state.json 到服务器（Cookie 过期时，在本机 Git Bash 运行）
# 前置：本机当前目录有最新的 state.json（由"1.本地提取通行证.bat"生成）
# =============================================================================
SERVER_IP=""   # ← 在这里填上你的服务器公网 IP，例如 123.45.67.89
SERVER_USER="root"
SSH_KEY=""   # 如果用 .pem 密钥登录，填密钥路径如 ~/.ssh/aliyun.pem；用密码则留空（运行时会提示输入密码）
APP_DIR="/opt/douyin-cloud-streak"

if [ -z "$SERVER_IP" ]; then
  echo "❌ 请先在脚本顶部把 SERVER_IP 改成你的服务器公网 IP 再运行。"
  exit 1
fi

if [ ! -f "./state.json" ]; then
  echo "❌ 当前目录未找到 state.json，请先在本机运行"本地提取通行证.bat"生成"
  exit 1
fi

# 组装 ssh/scp 参数
ARGS=()
if [ -n "$SSH_KEY" ]; then
  ARGS+=(-i "$SSH_KEY")
fi
ARGS+=(-o StrictHostKeyChecking=no)

echo "==> 备份服务器旧 state.json ..."
ssh "${ARGS[@]}" "$SERVER_USER@$SERVER_IP" \
  "cp '$APP_DIR/state.json' '$APP_DIR/state.json.bak.\$(date +%Y%m%d%H%M%S)' 2>/dev/null || echo '(无旧文件，跳过备份)'"

echo "==> 上传最新 state.json ..."
scp "${ARGS[@]}" ./state.json "$SERVER_USER@$SERVER_IP:$APP_DIR/state.json"

echo ""
echo "✅ state.json 已同步。服务器下次 00:01（北京时间）会自动使用新的登录态。"
echo "   想立即生效，可在服务器执行："
echo "   cd $APP_DIR && source venv/bin/activate && python cloud_streak.py"
