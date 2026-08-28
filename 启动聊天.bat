@echo off
chcp 65001 >nul
cd /d "%~dp0"
title P2P聊天

rem 探测可用的 Python 启动命令
set "RUN="
python --version >nul 2>&1 && set "RUN=python"
if not defined RUN (
    py --version >nul 2>&1 && set "RUN=py"
)

if defined RUN goto RUNIT

echo [!] 未检测到 Python。
echo 正在打开「安装环境.bat」帮你自动安装…
echo.
if exist "安装环境.bat" ( call "安装环境.bat" ) else ( echo 未找到 安装环境.bat )
pause
exit /b 0

:RUNIT
rem 启动器会自动检测环境、缺啥一键装、装完直接进入聊天
%RUN% launcher.py
echo.
pause