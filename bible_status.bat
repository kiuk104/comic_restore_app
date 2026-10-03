@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Bible transcribe status
python bible_status.py %*
pause
