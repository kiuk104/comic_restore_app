@echo off
cd /d "%~dp0"
python ebook_translate.py %*
if errorlevel 1 (
    echo.
    echo [ERROR] App failed to start. Messages above may help.
    echo  - Missing packages? Run: python -m pip install opencv-python numpy pillow anthropic
    echo  - PDF input needs:     python -m pip install pymupdf
    pause
)
