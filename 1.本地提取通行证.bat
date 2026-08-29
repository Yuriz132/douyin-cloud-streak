@echo off
title 抖音云端自动续火花 - 提取登录通行证
cd /d "%~dp0"

echo =======================================================
echo        抖音云端自动续火花 - 提取登录通行证
echo =======================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [未检测到 Python 环境]
    echo 正在为您打开 Python 官网下载页面...
    start https://www.python.org/downloads/
    echo.
    echo 【提示】安装 Python 时，请务必勾选底部选项：
    echo        [√] Add Python to PATH
    echo 安装完成后，请重新双击此脚本！
    echo.
    pause
    exit /b
)

echo [*] 正在检查并自动安装依赖包...
pip install -r requirements.txt >nul 2>&1
if errorlevel 1 (
    echo [*] 正在使用国内镜像源加速下载依赖...
    pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
)

echo [*] 正在确保 Chromium 浏览器内核就绪...
playwright install chromium >nul 2>&1

echo.
echo =======================================================
echo [*] 即将启动浏览器，请在弹出的网页右上角扫码登录抖音...
echo =======================================================
echo.
python extract_cookie.py

echo.
pause
