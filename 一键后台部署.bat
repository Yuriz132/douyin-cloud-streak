@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 抖音续火花 · 一键后台部署

:menu
cls
echo ============================================
echo   抖音续火花 · 一键后台部署
echo ============================================
echo.
echo   [1] 全新部署 / 更新   （首次使用：自动装环境+开机自启+启动）
echo   [2] 重新登录         （登录失效时：清除旧凭证，重新扫码）
echo   [3] 保持登录重新设置 （重启服务+重注册自启，登录不受影响）
echo   [4] 停止后台服务     （停止运行，可选取消开机自启）
echo   [0] 退出
echo.
set "choice="
set /p choice=请输入序号后回车：
if "%choice%"=="1" goto deploy
if "%choice%"=="2" goto relogin
if "%choice%"=="3" goto reset_keep
if "%choice%"=="4" goto stop_svc
if "%choice%"=="0" exit /b 0
goto menu

REM ============ [1] 全新部署 / 更新 ============
:deploy
echo.
echo [1/5] 检查 Python 环境...
call :check_python
if errorlevel 1 goto menu
echo.
echo [2/5] 准备虚拟环境与依赖（首次约 3~8 分钟，已装则跳过）...
call :install_deps
if errorlevel 1 goto menu
echo.
echo [3/5] 配置开机自启（用户级启动项，无需管理员权限）...
call :register_autostart
if errorlevel 1 goto menu
echo.
echo [4/5] 停止旧实例，避免端口冲突...
call :kill_old
echo.
echo [5/5] 启动后台服务...
call :start_service
start "" http://localhost:8000
echo.
echo ============================================
echo  [OK] 部署完成！
echo   - 服务已在后台静默运行，关终端/关网页不影响
echo   - 已配置开机自启，重启电脑后自动运行
echo   - 停止服务：再次运行本脚本选 [4]
echo ============================================
echo.
pause
goto menu

REM ============ [2] 重新登录 ============
:relogin
echo.
echo [1/5] 检查运行环境（已装则跳过）...
call :check_python
if errorlevel 1 goto menu
call :install_deps
if errorlevel 1 goto menu
echo.
echo [2/5] 停止当前后台服务...
call :kill_old
echo.
echo [3/5] 清除旧登录凭证（旧登录态全部作废）...
del /f /q "state.json" >nul 2>&1
del /f /q "data\state.json" >nul 2>&1
for /d %%d in ("data\accounts\*") do del /f /q "%%d\state.json" >nul 2>&1
echo  已清除全部登录凭证。
echo.
echo [4/5] 重新启动后台服务...
call :start_service
echo.
echo [5/5] 打开网页...
start "" http://localhost:8000
echo.
echo ============================================
echo  [OK] 请在网页中完成重新扫码：
echo   1. 打开【账号凭证】页面
echo   2. 点击【扫码登录】按钮
echo   3. 用抖音 APP 扫描二维码并确认登录
echo   4. 完成后可点【实时检测登录态】验证
echo ============================================
echo.
pause
goto menu

REM ============ [3] 保持登录重新设置 ============
:reset_keep
echo.
echo [1/4] 检查环境（已装则跳过，不碰登录凭证）...
call :check_python
if errorlevel 1 goto menu
call :install_deps
if errorlevel 1 goto menu
echo.
echo [2/4] 重新注册开机自启...
call :register_autostart
if errorlevel 1 goto menu
echo.
echo [3/4] 重启后台服务（登录凭证与定时任务设置均保留）...
call :kill_old
call :start_service
echo.
echo [4/4] 打开网页...
start "" http://localhost:8000
echo.
echo  [OK] 服务已重启，登录状态保留。
echo  建议到网页【账号凭证】页点一次【实时检测登录态】确认。
echo.
pause
goto menu

REM ============ [4] 停止后台服务 ============
:stop_svc
echo.
echo  停止后台服务...
call :kill_old
echo  [OK] 后台服务已停止。
echo.
set "ans="
set /p ans=是否同时取消开机自启？(输入 Y 确认，直接回车跳过)：
if /i "%ans%"=="Y" call :unregister_autostart
echo.
pause
goto menu

REM ============ 子过程 ============

:check_python
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
exit /b 0

:install_deps
if exist "venv\Scripts\pythonw.exe" (
    echo  虚拟环境已就绪，跳过安装。
    exit /b 0
)
python -m venv venv
if errorlevel 1 exit /b 1
venv\Scripts\python -m pip install -q --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
venv\Scripts\python -m pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 exit /b 1
set "PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright"
venv\Scripts\python -m playwright install chromium
if errorlevel 1 exit /b 1
exit /b 0

:register_autostart
powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $lnk = $ws.CreateShortcut([Environment]::GetFolderPath('Startup') + '\douyin-streak-background.lnk'); $lnk.TargetPath = '%~dp0启动后台.vbs'; $lnk.Save()"
if errorlevel 1 exit /b 1
echo  已注册：开机自动后台运行（用户级，无需管理员权限）
exit /b 0

:unregister_autostart
powershell -NoProfile -Command "Remove-Item ([Environment]::GetFolderPath('Startup') + '\douyin-streak-background.lnk') -ErrorAction SilentlyContinue"
echo  [OK] 已取消开机自启。重新开启请运行本脚本选 [1] 或 [3]。
exit /b 0

:kill_old
taskkill /f /im pythonw.exe >nul 2>&1
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue; if ($c) { $c | ForEach-Object { $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue; if ($p -and $p.ProcessName -like 'python*') { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; Write-Host ('  已停止旧实例 PID ' + $p.Id) } } }"
timeout /t 2 >nul
exit /b 0

:start_service
wscript "启动后台.vbs"
echo  等待服务就绪...
timeout /t 10 >nul
exit /b 0
