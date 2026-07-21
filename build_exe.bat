@echo off
chcp 65001 >nul
echo ============================================
echo   洛谷私信通知助手 - 打包为 exe
echo ============================================

pip install -r requirements.txt

pyinstaller --noconfirm --onefile --windowed ^
  --name "LuoguPMNotifier" ^
  main.py

echo.
echo 打包完成，exe 文件在 dist\LuoguPMNotifier.exe
pause
