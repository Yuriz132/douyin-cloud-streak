#!/usr/bin/env bash
# ============================================================
#  deploy_ai_cron.sh
#  把 ai_reply.py 部署为 cron 定时任务（与 cloud_streak.py 并行）
#  适配服务端真实环境：/opt/douyin-cloud-streak/venv
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 自动探测虚拟环境（服务端是 venv/，兼容 .venv/）
if [[ -x "${PROJECT_DIR}/venv/bin/python" ]]; then
  PY_BIN="${PROJECT_DIR}/venv/bin/python"
elif [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
  PY_BIN="${PROJECT_DIR}/.venv/bin/python"
else
  echo "[错误] 找不到虚拟环境，请先创建并安装依赖："
  echo "  cd ${PROJECT_DIR}"
  echo "  python3 -m venv venv"
  echo "  source venv/bin/activate"
  echo "  pip install -r requirements.txt"
  echo "  playwright install chromium"
  exit 1
fi

# 安装依赖（服务端 venv 缺 requests，且需要 playwright-stealth）
"${PY_BIN}" -m pip install -q -r "${PROJECT_DIR}/requirements.txt" \
  || echo "[提示] pip install 失败，请手动执行： ${PY_BIN} -m pip install -r requirements.txt"

if [[ ! -f "${PROJECT_DIR}/ai_config.json" ]]; then
  echo "[提示] 未发现 ai_config.json，已从 ai_config.example.json 创建副本，"
  echo "       请用 vim/nano 打开并填写 deepseek api_key 与好友列表后再跑此脚本。"
  cp "${PROJECT_DIR}/ai_config.example.json" "${PROJECT_DIR}/ai_config.json"
  exit 0
fi

if [[ ! -f "${PROJECT_DIR}/state.json" ]]; then
  echo "[错误] 缺少 state.json，请用原项目提取的 state.json 放到 ${PROJECT_DIR}/"
  exit 1
fi

# 每 2 分钟轮询一次新消息（足够即时，又不至于太密集）
# 想要更即时：把 */2 改成 */1
CRON_LINE="*/2 * * * * cd ${PROJECT_DIR} && ${PY_BIN} ai_reply.py >> ai_reply_cron.log 2>&1"

# 写入 crontab（保留原 cloud_streak 任务）
( crontab -l 2>/dev/null | grep -v "ai_reply.py" ; echo "${CRON_LINE}" ) | crontab -

echo "[完成] 已注册 cron:"
crontab -l | grep "ai_reply.py" || true
echo
echo "立即试跑一次（不真实发送）："
echo "  cd ${PROJECT_DIR} && ${PY_BIN} ai_reply.py --dry-run"
echo
echo "查看日志："
echo "  tail -f ${PROJECT_DIR}/ai_reply_cron.log"
