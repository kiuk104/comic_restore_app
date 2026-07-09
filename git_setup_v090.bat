@echo off
rem ------------------------------------------------------------------
rem  Comic Restore v0.9.0 - first git commit + tag
rem  Run this once by double-clicking. Requires Git for Windows.
rem  (https://git-scm.com/download/win)
rem ------------------------------------------------------------------
cd /d "%~dp0"

where git >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Git is not installed or not in PATH.
    echo         Install from https://git-scm.com/download/win
    pause
    exit /b 1
)

rem Clean up half-created .git left by the sandbox (stale lock file)
if exist ".git" (
    echo Removing stale .git folder...
    rd /s /q ".git"
)

git init -b main 2>nul
if errorlevel 1 (
    git init
    git symbolic-ref HEAD refs/heads/main
)

git config user.name "kiuk"
git config user.email "kiuk104@gmail.com"

git add -A
git status --short
echo.

git commit -m "Comic Restore v0.9.0 - upscale / Claude transcription / retype / browser review editor"
if errorlevel 1 (
    echo [ERROR] Commit failed. See messages above.
    pause
    exit /b 1
)

git tag -a v0.9.0 -m "First pre-release (beta). API and data formats may change before 1.0."

echo.
echo ================================================================
git log --oneline -1
git tag
echo ================================================================
echo.
echo Done! To push to GitHub:
echo   1) Create an empty repo on github.com (e.g. comic-restore)
echo      * Do NOT add README/.gitignore there - they already exist here
echo   2) Run:
echo        git remote add origin https://github.com/YOUR_ID/comic-restore.git
echo        git push -u origin main --tags
echo.
pause
