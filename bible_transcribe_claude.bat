@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
set F=E:\Coding\capture_tool\01_창세기_claude
del "%F%\_done.txt" 2>nul
echo Gemini가 막은 창세기 캡처 83장을 Claude Sonnet 4.5로 전사합니다 (6장 동시, 약 1.5달러)
python ebook_translate.py "%F%" --mode bible --ocr claude --model claude-sonnet-4-5 --source-lang ko --title claude --out "%F%\out_claude" > "%F%\log_claude.txt" 2>&1
findstr /C:"완료" /C:"실패" /C:"오류" /C:"합계" /C:"전사 [" "%F%\log_claude.txt"
echo done> "%F%\_done.txt"
echo.
echo 전사 끝 - 이 창은 닫아도 됩니다.
pause
