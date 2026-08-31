@echo off
title 抖音云端自动续火花 - 本地运行
cd /d "%~dp0"

echo =======================================================
echo        抖音云端自动续火花 - 本地运行
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
echo 请选择本地运行模式：
echo [1] 扫码登录抖音并提取通行证（登录后自动同步联系人、启动本地后台）
echo [2] 立即发送续火花
echo [3] 模拟演练模式 (Dry-Run，不真正发送消息)
echo [4] 同步抖音联系人列表到本地台账
echo [5] 启动本地 Web 管理后台
echo.
set /p choice="请输入数字 (1-5，直接回车默认2): "

if "%choice%"=="1" (
    echo.
    echo [*] 正在启动浏览器，请在弹出的网页右上角扫码登录抖音...
    python extract_cookie.py
) else if "%choice%"=="3" (
    echo.
    echo [*] 正在以【模拟演练模式】启动...
    python run_cli.py --dry-run
) else if "%choice%"=="4" (
    echo.
    echo [*] 正在同步联系人列表...
    python run_cli.py --sync-contacts
) else if "%choice%"=="5" (
    echo.
    echo [*] 正在启动本地 Web 管理后台（端口占用时自动换端口）...
    python app.py
) else (
    if not exist "data\state.json" (
        if not exist "state.json" (
            echo [提示] 未找到登录凭据 state.json，先启动扫码登录...
            python extract_cookie.py
        )
    )
    echo.
    echo [*] 正在启动【正式发送】...
    python run_cli.py
)

echo.
pause
