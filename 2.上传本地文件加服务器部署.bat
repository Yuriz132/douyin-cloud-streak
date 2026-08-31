@echo off
title 抖音云端自动续火花 - 上传本地文件与服务器部署
cd /d "%~dp0"

echo =======================================================
echo        抖音云端自动续火花 - 上传本地文件与服务器部署
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

echo.
echo 请选择模式：
echo [1] 上传本地文件到服务器（登录态+联系人台账，日常同步用）
echo [2] 服务器部署（完整代码部署/更新，首次部署或代码更新时用）
echo [3] Docker 部署
echo.
set /p choice="请输入数字 (1/2/3，直接回车默认1): "

if "%choice%"=="2" (
    echo.
    echo [*] 正在启动服务器部署...
    python deploy_to_server.py
) else if "%choice%"=="3" (
    echo.
    echo [*] 正在启动 Docker 部署...
    python deploy_docker_to_server.py
) else (
    echo.
    echo [*] 正在上传本地文件到服务器...
    python sync_to_server.py
)

echo.
pause
