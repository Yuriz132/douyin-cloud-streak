@echo off
title 抖音云端自动续火花 - 启动 Web 管理后台
cd /d "%~dp0"

echo =======================================================
echo        抖音云端自动续火花 - 启动 Web 管理后台
echo =======================================================
echo.

if not exist ".env" (
    if exist ".env.example" (
        copy .env.example .env >nul
    ) else (
        echo PORT=8000 > .env
        echo HOST=0.0.0.0 >> .env
        echo AUTH_TOKEN=spark_secret_token_change_me >> .env
    )
)

echo [*] 正在启动 Web 管理后台服务...
echo [*] 服务地址: http://127.0.0.1:8000
echo.
echo [*] 正在自动打开默认浏览器...
start http://127.0.0.1:8000

echo.
echo [提示] 保持此窗口开启即可维持后台服务。关闭窗口将停止服务。
echo =======================================================
echo.
python app.py

pause
