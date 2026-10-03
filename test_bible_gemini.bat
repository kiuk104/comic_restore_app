@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
set SRC=E:\Coding\capture_tool\bible_test_gen1-10
del "%SRC%\_done.txt" 2>nul
echo [1/2] gemini-3.8-flash 전사 중... (캡처 20장)
python ebook_translate.py "%SRC%" --mode bible --ocr gemini --gemini-model gemini-3.8-flash --title g38flash --out "%SRC%\out_g38flash" > "%SRC%\log_g38flash.txt" 2>&1
type "%SRC%\log_g38flash.txt"
echo.
echo [2/2] gemini-3.1-flash-lite 전사 중... (캡처 20장)
python ebook_translate.py "%SRC%" --mode bible --ocr gemini --gemini-model gemini-3.1-flash-lite --title g31lite --out "%SRC%\out_g31lite" > "%SRC%\log_g31lite.txt" 2>&1
type "%SRC%\log_g31lite.txt"
echo done> "%SRC%\_done.txt"
echo.
echo 테스트 끝 - 이 창은 닫아도 됩니다.
pause
