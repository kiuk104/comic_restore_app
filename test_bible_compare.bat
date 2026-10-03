@echo off
chcp 65001 >nul
cd /d "%~dp0"
del "E:\Coding\capture_tool\bible_test_big\_done_compare.txt" 2>nul
echo === 작은 캡처 (이미 한 Windows OCR·Gemini 제외) ===
call test_bible_engines.bat "E:\Coding\capture_tool\bible_test_gen1-10" tesseract deepseek
echo.
echo === 큰 캡처 ===
call test_bible_engines.bat "E:\Coding\capture_tool\bible_test_big" winocr tesseract deepseek gemini
echo done> "E:\Coding\capture_tool\bible_test_big\_done_compare.txt"
echo.
echo 비교 테스트 끝 - 이 창은 닫아도 됩니다.
pause
