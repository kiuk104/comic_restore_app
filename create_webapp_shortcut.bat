@echo off
rem ---------------------------------------------------------------
rem  ComicRestore WebApp shortcut creator
rem  - Creates "ComicRestore Web.lnk" on Desktop and in Start Menu
rem  - Icon: webapp_icon.ico (teal speech-bubble variant)
rem  - Requires: pip install pywebview  (first run only)
rem ---------------------------------------------------------------
setlocal
set "HERE=%~dp0"
set "PS1=%TEMP%\make_comic_web_shortcut.ps1"

> "%PS1%" echo $ws = New-Object -ComObject WScript.Shell
>>"%PS1%" echo $here = '%HERE%'
>>"%PS1%" echo $dirs = @([Environment]::GetFolderPath('Desktop'),
>>"%PS1%" echo          (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'))
>>"%PS1%" echo foreach($d in $dirs) {
>>"%PS1%" echo   $s = $ws.CreateShortcut((Join-Path $d 'ComicRestore Web.lnk'))
>>"%PS1%" echo   $s.TargetPath = $env:ComSpec
>>"%PS1%" echo   $s.Arguments = '/c "' + $here + 'run_webapp.bat"'
>>"%PS1%" echo   $s.WorkingDirectory = $here
>>"%PS1%" echo   $s.IconLocation = $here + 'webapp_icon.ico,0'
>>"%PS1%" echo   $s.Description = 'Comic Restore WebApp (pywebview)'
>>"%PS1%" echo   $s.Save()
>>"%PS1%" echo   Write-Host ('Created: ' + (Join-Path $d 'ComicRestore Web.lnk'))
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
echo Done. To pin to the taskbar:
echo   1) Press the Win key, type "ComicRestore Web",
echo      right-click the result and choose "Pin to taskbar"
echo   2) Or drag the Desktop shortcut onto the taskbar
echo.
pause
