@echo off
REM ============================================================
REM  抖音续火花 · 本地提取登录态（Windows 一键启动）
REM  作用：打开浏览器让你登录抖音，并把登录态保存为 state.json
REM  前置：本机已安装 Python 3.10+ ，并已执行过  pip install -r requirements.txt
REM ============================================================
title 抖音续火花 - 本地提取登录态
chcp 65001 >nul

echo 正在启动抖音登录态提取工具……
echo 请在弹出的浏览器中登录抖音，登录完成后回到这里按回车。
echo.

python extract_cookie.py

echo.
echo 如果看到“登录态已保存”说明成功，state.json 已生成在当前目录。
echo 之后请用 Git Bash 运行 sync_state_safe.sh 把它传到服务器。
echo.
pause
