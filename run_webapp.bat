@echo off
rem ComicRestore 웹앱 (pywebview) — 최초 1회: pip install pywebview
cd /d "%~dp0"
python comic_restore_web.py
if errorlevel 1 pause
