@echo off
cd /d "%~dp0"
python comic_restore_app.py
if errorlevel 1 (
    echo.
    echo [ERROR] App failed to start. Messages above may help.
    echo  - Missing packages? Run: python -m pip install opencv-python numpy pillow psd-tools anthropic
    pause
)
