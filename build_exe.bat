@echo off
rem 一键打包成 exe（图形界面版，onedir 模式：启动更快）
chcp 65001 >nul
cd /d "%~dp0"

echo [1/4] 安装依赖...
python -m pip install paho-mqtt pillow customtkinter tkinterdnd2 pyinstaller --disable-pip-version-check

echo [2/4] 打包中（首次较慢，请耐心等待）...
python -m PyInstaller --noconfirm --onedir --windowed --name P2PChat --collect-all tkinterdnd2 --collect-all customtkinter chat_gui.py

echo [3/4] 打包 ZIP（方便分发整个文件夹）...
powershell -NoProfile -Command "if (Test-Path 'dist\P2PChat-win.zip') { Remove-Item 'dist\P2PChat-win.zip' -Force }; Compress-Archive -Path 'dist\P2PChat\*' -DestinationPath 'dist\P2PChat-win.zip' -Force"

echo [4/4] 完成！
echo.
echo 启动文件：dist\P2PChat\P2PChat.exe  （双击运行）
echo 分发压缩包：dist\P2PChat-win.zip    （解压后整个文件夹一起发给别人）
echo.
echo 注意：onedir 模式启动快，但必须把「整个文件夹」一起分发，不能只发单个 exe。
echo.
pause