#!/usr/bin/env bash
# ============================================================
# server_setup.sh — 服务器端一键部署
# 在 /opt/douyin-cloud-streak 下运行
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "=== 抖音 AI 自动回复 v2 一键部署 ==="
echo "项目路径: $PROJECT_DIR"

# 1) 检测虚拟环境
if [[ -x "${PROJECT_DIR}/venv/bin/python" ]]; then
  PY_BIN="${PROJECT_DIR}/venv/bin/python"
elif [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
  PY_BIN="${PROJECT_DIR}/.venv/bin/python"
else
  echo "[错误] 找不到 venv，请先创建: python3 -m venv venv"
  exit 1
fi
echo "Python: $($PY_BIN --version)"

# 2) 备份旧文件
BACKUP_DIR="${PROJECT_DIR}/backup_$(date +%Y%m%d_%H%M%S)"
echo "备份旧文件 -> $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
for f in ai_config.json ai_reply.py requirements.txt deploy_ai_cron.sh; do
  [[ -f "${PROJECT_DIR}/$f" ]] && cp "${PROJECT_DIR}/$f" "${BACKUP_DIR}/$f"
done

# 3) 注入密钥生成 ai_config.json
echo "注入 API Key 并生成 ai_config.json..."
# 如果 ai_config.example.json 存在且 ai_config.json 缺失或 key 是占位符
if [[ ! -f "${PROJECT_DIR}/ai_config.json" ]]; then
  echo "  ai_config.json 缺失，从 ai_config.example.json 生成..."
  cp "${PROJECT_DIR}/ai_config.example.json" "${PROJECT_DIR}/ai_config.json"
fi

# 如果环境变量有 key，注入
if [[ -n "${AI_YXKL_KEY:-}" ]]; then
  echo "  环境变量 AI_YXKL_KEY 存在，注入..."
  # 用 python 安全替换
  $PY_BIN -c "
import json
cfg=json.load(open('${PROJECT_DIR}/ai_config.json'))
for p in cfg['providers']:
    if p['name']=='yxkl':
        p['api_key']='${AI_YXKL_KEY}'
        break
json.dump(cfg,open('${PROJECT_DIR}/ai_config.json','w'),ensure_ascii=False,indent=2)
print('   yxkl key injected')
"
fi

if [[ -n "${AI_AGNES_KEY:-}" ]]; then
  $PY_BIN -c "
import json
cfg=json.load(open('${PROJECT_DIR}/ai_config.json'))
for p in cfg['providers']:
    if p['name']=='agnes':
        p['api_key']='${AI_AGNES_KEY}'
        break
json.dump(cfg,open('${PROJECT_DIR}/ai_config.json','w'),ensure_ascii=False,indent=2)
print('   agnes key injected')
"
fi

# 检查 key 不是占位符
$PY_BIN -c "
import json,sys
cfg=json.load(open('${PROJECT_DIR}/ai_config.json'))
for p in cfg['providers']:
    k=p.get('api_key','')
    if k and '填入' not in k and 'YOUR' not in k and k:
        print(f'   {p[\"name\"]}: key OK ({k[:8]}...)')
    elif p['name']=='deepseek':
        print(f'   deepseek: key 空，跳过')
    else:
        print(f'   {p[\"name\"]}: key 仍是占位符！')
        sys.exit(1)
print('   key check passed')
"

# 4) 安装依赖
echo "安装依赖..."
$PY_BIN -m pip install -q -r "${PROJECT_DIR}/requirements.txt" 2>&1 | tail -3

# 5) 语法校验
echo "语法校验..."
$PY_BIN -c "import ast; ast.parse(open('${PROJECT_DIR}/ai_reply.py').read()); print('   ai_reply.py 语法 OK')"

# 6) 注册 cron（保留原续火花）
echo "注册 cron..."
CRON_LINE="*/2 * * * * cd ${PROJECT_DIR} && ${PY_BIN} ai_reply.py >> ai_reply_cron.log 2>&1"
( crontab -l 2>/dev/null | grep -v "ai_reply.py" ; echo "${CRON_LINE}" ) | crontab -
echo "   cron 已注册"

# 7) 统计校验
echo "历史统计..."
$PY_BIN ai_reply.py --stats

# 8) dry-run 验证
echo ""
echo "=== 执行 dry-run 验证（不发送） ==="
$PY_BIN ai_reply.py --dry-run --friend 梁登辉 2>&1

echo ""
echo "=== 部署完成 ==="
echo "查看日志: tail -f ${PROJECT_DIR}/ai_reply_cron.log"
echo "手动跑一轮: $PY_BIN ai_reply.py --once"
