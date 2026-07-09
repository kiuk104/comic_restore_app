@echo off
cd /d "%~dp0"
echo ============================================
echo  Comic Restore App - EXE build (PyInstaller)
echo ============================================
echo.

python -m pip install pyinstaller --quiet
if errorlevel 1 (
    echo [ERROR] PyInstaller install failed. Check internet / pip.
    pause
    exit /b 1
)

python -m PyInstaller --noconfirm --onefile --windowed --name ComicRestore --collect-submodules psd_tools --collect-submodules anthropic --hidden-import comic_retype_pipeline --hidden-import comic_restore_pipeline comic_restore_app.py

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed. See messages above.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  DONE!  ->  dist\ComicRestore.exe
echo  (app_config.json is saved next to the exe)
echo ============================================
pause
