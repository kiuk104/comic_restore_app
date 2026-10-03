@echo off
rem 사용법: test_bible_engines.bat "<캡처 폴더>" winocr tesseract deepseek gemini
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if "%BLANG%"=="" set BLANG=ko
cd /d "%~dp0"
set SRC=%~1
shift
:loop
if "%~1"=="" goto end
set E=%~1
set ARGS=--ocr %E%
if "%E%"=="gemini" set ARGS=--ocr gemini --gemini-model gemini-3.8-flash
echo.
echo [%E%] %SRC% 전사 중...
python ebook_translate.py "%SRC%" --mode bible %ARGS% --source-lang %BLANG% --title %E% --out "%SRC%\out_%E%" > "%SRC%\log_%E%.txt" 2>&1
findstr /C:"완료" /C:"오류" /C:"합계" "%SRC%\log_%E%.txt"
shift
goto loop
:end
