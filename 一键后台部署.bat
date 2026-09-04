@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 抖音续火花 · 一键后台部署

echo ============================================
echo   抖音续火花 · 一键后台部署
echo   本脚本只需运行一次，之后开机自动后台运行
echo ============================================
echo.

echo [1/5] 检查 Python 环境...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [X] 未检测到 Python！请先安装 Python 3.10 或 3.11
    echo      安装时务必勾选 "Add Python to PATH"
    echo      下载地址: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)
python --version

echo.
echo [2/5] 准备虚拟环境与依赖（首次运行约 3~8 分钟，请耐心等待）...
if not exist "venv\Scripts\pythonw.exe" (
    python -m venv venv
    if errorlevel 1 goto :err
    venv\Scripts\python -m pip install -q --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
    venv\Scripts\python -m pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 goto :err
    set "PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright"
    venv\Scripts\python -m playwright install chromium
    if errorlevel 1 goto :err
) else (
    echo  虚拟环境已就绪，跳过安装。
)

echo.
echo [3/5] 配置开机自启（用户级启动项，无需管理员权限）...
powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $lnk = $ws.CreateShortcut([Environment]::GetFolderPath('Startup') + '\douyin-streak-background.lnk'); $lnk.TargetPath = '%~dp0启动后台.vbs'; $lnk.Save()"
if errorlevel 1 goto :err

echo.
echo [4/5] 清理旧实例（避免端口冲突）...
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue; if ($c) { $c | ForEach-Object { $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue; if ($p -and $p.ProcessName -like 'python*') { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; Write-Host ('  已停止旧实例 PID ' + $p.Id) } } }"
timeout /t 2 >nul

echo.
echo [5/5] 启动后台服务...
wscript "启动后台.vbs"
echo  等待服务就绪...
timeout /t 10 >nul
start "" http://localhost:8000

echo.
echo ============================================
echo  [OK] 部署完成！
echo   - 服务已在后台静默运行，关终端/关网页不影响
echo   - 已配置开机自启，重启电脑后自动运行
echo   - 首次进入网页请用抖音 APP 扫码登录
echo   - 停止服务：双击 停止后台.bat
echo ============================================
echo.
echo  提示：启动前已自动清理旧实例并占用 8000 端口。
echo  以后想停止服务：双击 停止后台.bat 即可。
pause
exit /b 0

:err
echo.
echo  [X] 安装过程出错，请把上面的报错信息截图求助。
pause
exit /b 1
