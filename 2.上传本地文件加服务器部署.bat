@echo off
title 抖音云端自动续火花 - 上传本地文件与服务器部署
cd /d "%~dp0"

echo ======================================================
echo        抖音云端自动续火花 - 上传本地文件与服务器部署
echo ======================================================
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

echo [*] 正在检查并自动安装依赖包（首次安装约需几分钟，请耐心等待）...
pip install -r requirements.txt >nul 2>&1
if errorlevel 1 (
    echo     官方源下载失败，改用国内清华镜像源加速...
    pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
)
echo.
echo ------------------------------------------------------
echo  请选择模式：
echo ------------------------------------------------------
echo  [1] 上传本地文件到服务器
echo      -- 登录态 state.json + 联系人台账 ledger.json，日常刷新用
echo  [2] 服务器部署 -- 首次部署或更新代码，systemd 方式（推荐）
echo  [3] Docker 部署
echo ------------------------------------------------------
echo  小贴士：想直接用网页端 -- 运行本脚本输入 2 完成部署，
echo  然后打开网页「凭证」页用手机扫码登录即可。
echo  若服务器扫码或同步联系人失败 -- 先双击「1.本地运行.bat」
echo  （输入 1）本地扫码，再用本脚本（输入 1）上传到服务器。
echo ------------------------------------------------------
echo.
set "choice="
set /p choice="请输入数字 1/2/3 (直接回车默认 1): "
if not defined choice set "choice=1"
set "choice=%choice: =%"

if "%choice%"=="2" (
    echo.
    echo [*] 正在启动服务器部署（systemd 方式）...
    python deploy_to_server.py
    goto end
)
if "%choice%"=="3" (
    echo.
    echo [*] 正在启动 Docker 部署...
    python deploy_docker_to_server.py
    goto end
)

rem 其余输入（含直接回车）= 上传本地文件
echo.
echo [*] 正在上传本地文件到服务器（登录态 + 联系人台账）...
python sync_to_server.py

:end
echo.
echo ------------------------------------------------------
echo  本次任务已结束，感谢使用！
pause
