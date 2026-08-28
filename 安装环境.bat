@echo off
chcp 65001 >nul
cd /d "%~dp0"
title P2P聊天 - 环境一键安装

echo ============================================
echo   P2P 聊天 - 运行环境和依赖一键安装
echo ============================================
echo.

rem 探测可用的 Python 启动命令
set "RUN="
python --version >nul 2>&1 && set "RUN=python"
if not defined RUN (
    py --version >nul 2>&1 && set "RUN=py"
)

if defined RUN goto HASPY

echo [!] 未检测到 Python，尝试用 winget 自动安装...
where winget >nul 2>&1
if %errorlevel%==0 (
    winget install --id Python.Python.3.12 -e --source winget --scope user
    echo.
    echo 安装完成后，请重新双击本脚本，完成依赖安装。
) else (
    echo 未找到 winget，正在为你打开 Python 官网下载页...
    start "" https://www.python.org/downloads/
    echo.
    echo 请下载并安装 Python，安装时务必勾选 "Add Python to PATH"，
    echo 装完后再双击本脚本一次即可。
)
pause
exit /b 0

:HASPY
echo 检测到 Python，正在升级 pip 并安装全部依赖...
echo.
%RUN% -m pip install --upgrade pip --disable-pip-version-check
%RUN% -m pip install paho-mqtt pillow customtkinter tkinterdnd2 cryptography --disable-pip-version-check
echo.
echo ============================================
echo  依赖安装完成！现在可双击「启动聊天.bat」开始聊天。
echo ============================================
pause
exit /b 0