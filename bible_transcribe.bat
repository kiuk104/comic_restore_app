@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "F=%~1"
if not "%F%"=="" goto run
echo Drag a capture folder onto this file.
pause
exit /b
:run
set BLANG=ko
echo %date% %time% start "%F%"> "%~dp0bible_transcribe_last.txt"
del "%F%\_done_transcribe.txt" 2>nul
echo Transcribing: "%F%"
echo Engines: winocr tesseract deepseek gemini
call "%~dp0test_bible_engines.bat" "%F%" winocr tesseract deepseek gemini
echo done> "%F%\_done_transcribe.txt"
echo.
echo Finished. You can close this window.
pause
