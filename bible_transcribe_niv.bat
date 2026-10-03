@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 사용법: 캡처 폴더를 이 파일 위에 끌어다 놓거나, 인자 없이 실행하면 아래 기본 폴더
set F=%~1
if "%F%"=="" set F=E:\Coding\capture_tool\NIV_01_창세기
set BLANG=en
del "%F%\_done_transcribe.txt" 2>nul
echo NIV 캡처 전사 (영어) - Windows OCR, Tesseract, DeepSeek, Gemini 3.8: %F%
call test_bible_engines.bat "%F%" winocr tesseract deepseek gemini
echo done> "%F%\_done_transcribe.txt"
echo.
echo 전사 끝 - 이 창은 닫아도 됩니다.
pause
