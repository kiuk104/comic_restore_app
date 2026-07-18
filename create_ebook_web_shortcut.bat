@echo off
rem ---------------------------------------------------------------
rem  EbookTranslate Web (pywebview) shortcut creator
rem  - Creates "EbookTranslate Web.lnk" on Desktop and in Start Menu
rem ---------------------------------------------------------------
setlocal
set "HERE=%~dp0"
set "PS1=%TEMP%\make_ebook_web_shortcut.ps1"

> "%PS1%" echo $ws = New-Object -ComObject WScript.Shell
>>"%PS1%" echo $here = '%HERE%'
>>"%PS1%" echo $dirs = @([Environment]::GetFolderPath('Desktop'),
>>"%PS1%" echo          (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'))
>>"%PS1%" echo foreach($d in $dirs) {
>>"%PS1%" echo   $s = $ws.CreateShortcut((Join-Path $d 'EbookTranslate Web.lnk'))
>>"%PS1%" echo   $s.TargetPath = $env:ComSpec
>>"%PS1%" echo   $s.Arguments = '/c "' + $here + 'run_ebook_web.bat"'
>>"%PS1%" echo   $s.WorkingDirectory = $here
>>"%PS1%" echo   $s.IconLocation = $here + 'ebook_web_icon.ico'
>>"%PS1%" echo   $s.Description = 'Scanned Ebook Korean Translator (Web UI)'
>>"%PS1%" echo   $s.Save()
>>"%PS1%" echo   Write-Host ('Created: ' + (Join-Path $d 'EbookTranslate Web.lnk'))
>>"%PS1%" echo }

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
if errorlevel 1 (
    echo.
    echo [ERROR] Shortcut creation failed.
    pause
    exit /b 1
)
del "%PS1%" >nul 2>&1

echo.
echo Done. Pin it: Win key, type "EbookTranslate Web",
echo right-click, "Pin to taskbar".
echo.
pause
