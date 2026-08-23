@echo off
title 抖音云端自动续火花 - 桌面端立即运行
cd /d "%~dp0"

echo =======================================================
echo        抖音云端自动续火花 - 桌面端立即运行
echo =======================================================
echo.

if not exist "data\state.json" (
    if not exist "state.json" (
        echo [提示] 未找到登录凭据 state.json！
        echo 正在自动为您启动扫码登录程序...
        python extract_cookie.py
    )
)

echo 请选择运行模式：
echo [1] 正式发送续火花 (默认)
echo [2] 模拟演练模式 (Dry-Run，不真正发送消息)
echo [3] 同步抖音联系人列表到本地台账
echo.
set /p choice="请输入数字 (1/2/3，直接回车默认1): "

if "%choice%"=="2" (
    echo.
    echo [*] 正在以【模拟演练模式】启动...
    python run_cli.py --dry-run
) else if "%choice%"=="3" (
    echo.
    echo [*] 正在同步联系人列表...
    python run_cli.py --sync-contacts
) else (
    echo.
    echo [*] 正在启动【正式发送】...
    python run_cli.py
)

echo.
pause
