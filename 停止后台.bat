@echo off
chcp 65001 >nul
title 抖音续火花 · 停止后台服务

echo ============================================
echo   抖音续火花 · 停止后台服务
echo ============================================
echo.

taskkill /f /im pythonw.exe >nul 2>&1
if errorlevel 1 (
    echo  后台服务当前未在运行。
) else (
    echo  [OK] 后台服务已停止。
)

echo.
set /p ans=是否同时取消开机自启？(输入 Y 确认，直接回车跳过)：
if /i "%ans%"=="Y" (
    powershell -NoProfile -Command "Remove-Item ([Environment]::GetFolderPath('Startup') + '\douyin-streak-background.lnk') -ErrorAction SilentlyContinue"
    echo  [OK] 已取消开机自启。重新部署请再运行 一键后台部署.bat
)

echo.
echo  提示：若定时任务仍在执行，可能是终端窗口里还跑着旧实例
echo  （python.exe），直接关闭那个终端窗口即可彻底停止。
echo.
pause
