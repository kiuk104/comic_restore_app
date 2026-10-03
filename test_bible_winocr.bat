@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
set SRC=E:\Coding\capture_tool\bible_test_gen1-10
del "%SRC%\_done_winocr.txt" 2>nul
python -c "import winocr" 2>nul
if errorlevel 1 (
  echo winocr 패키지가 없습니다. 아래 명령으로 설치한 뒤 다시 실행하세요:
  echo     pip install winocr
  echo no-winocr> "%SRC%\_done_winocr.txt"
  pause
  exit /b 1
)
echo Windows OCR 한국어 전사 중... (캡처 20장, 무료 - 1분 안팎)
python ebook_translate.py "%SRC%" --mode bible --ocr winocr --source-lang ko --title winocr --out "%SRC%\out_winocr" > "%SRC%\log_winocr.txt" 2>&1
type "%SRC%\log_winocr.txt"
echo done> "%SRC%\_done_winocr.txt"
echo.
echo 테스트 끝 - 이 창은 닫아도 됩니다.
echo (전사가 전부 실패했다면: 설정 - 시간 및 언어 - 언어에서 한국어의 '광학 문자 인식' 기능이 설치돼 있는지 확인)
pause
