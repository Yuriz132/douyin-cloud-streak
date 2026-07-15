#!/usr/bin/env bash
# =============================================================================
# 本机一键：创建 GitHub 仓库并推送（读取环境变量中的 Token，不写死任何密钥）
# 用法（在你自己的电脑 / Git Bash 中执行）：
#   TOKEN="你的github_pat_xxx" bash push_to_github.sh
# 前置：当前目录已 git init 且已 git add . && git commit（或本脚本会自动初始化）
# 注意：state.json 已被 .gitignore 排除，不会上传。
# =============================================================================
set -euo pipefail

# 兼容 Windows Git Bash：python 命令可能叫 python 或 python3
if command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "❌ 未找到 python / python3，请先安装 Python 3.10+"
  exit 1
fi

TOKEN="${TOKEN:-}"
if [ -z "$TOKEN" ]; then
  echo "❌ 未提供 GitHub Token。请这样运行："
  echo '   TOKEN="你的github_pat_xxx" bash push_to_github.sh'
  exit 1
fi

REPO_NAME="douyin-cloud-streak"
API="https://api.github.com"
AUTH_HDR="Authorization: Bearer $TOKEN"

echo "==> 获取 GitHub 用户名 ..."
USERNAME=$(curl -s -H "$AUTH_HDR" "$API/user" | $PY -c "import sys,json;print(json.load(sys.stdin)['login'])")
echo "    用户名: $USERNAME"

echo "==> 创建仓库 $REPO_NAME (public) ..."
# 已存在则忽略错误（HTTP 422）
curl -s -o /dev/null -w "    create repo -> HTTP %{http_code}\n" \
  -X POST -H "$AUTH_HDR" -H "Accept: application/vnd.github+json" \
  "$API/user/repos" \
  -d "{\"name\":\"$REPO_NAME\",\"private\":false,\"description\":\"抖音云端自动续火花 · 完整部署教程与源码\",\"auto_init\":false}"

# 初始化（如果还没有 git 仓库）
if [ ! -d ".git" ]; then
  git init
fi

# 设置提交者身份（服务器上未配置 git 全局身份时，避免 commit 失败）
GIT_USER_EMAIL="${GIT_USER_EMAIL:-noreply@example.com}"
GIT_USER_NAME="${GIT_USER_NAME:-$(whoami)}"
git config user.email "$GIT_USER_EMAIL" >/dev/null 2>&1 || true
git config user.name "$GIT_USER_NAME" >/dev/null 2>&1 || true

# 提交（如果还有未提交的）
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  git add .
  git commit -m "feat: 抖音云端续火花 自动化项目（含完整部署教程）" || true
fi

# 默认分支：先提交再重命名，否则空仓库无法切到 main
DEFAULT_BRANCH="main"
git branch -M "$DEFAULT_BRANCH" 2>/dev/null || true

echo "==> 配置 remote 并推送 ..."
# 用 token 嵌入 URL；注意：token 仅用于本次推送，不会写入仓库文件
REMOTE="https://$TOKEN@github.com/$USERNAME/$REPO_NAME.git"
if git remote | grep -q "^origin$"; then
  git remote set-url origin "$REMOTE"
else
  git remote add origin "$REMOTE"
fi

git push -u origin "$DEFAULT_BRANCH"

echo ""
echo "✅ 推送完成！仓库地址：https://github.com/$USERNAME/$REPO_NAME"
echo "   （提示：推送用的 token 已直接写在 remote URL 里，若不想保留，"
echo "    可改为 ssh 方式：git remote set-url origin git@github.com:$USERNAME/$REPO_NAME.git）"
