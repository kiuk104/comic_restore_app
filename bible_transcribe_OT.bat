@echo off
chcp 65001 >nul
cd /d "%~dp0"
set F=E:\Coding\capture_tool\02_구약
del "%F%\_done_transcribe.txt" 2>nul
echo 구약 캡처 2757장 전사 - Windows OCR, Tesseract, DeepSeek(6장 동시), Gemini 3.8(6장 동시)
call test_bible_engines.bat "%F%" winocr tesseract deepseek gemini
echo done> "%F%\_done_transcribe.txt"
echo.
echo 전사 끝 - 이 창은 닫아도 됩니다.
pause
