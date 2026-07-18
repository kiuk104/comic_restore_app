@echo off
cd /d "%~dp0"
python ebook_translate_web.py %*
if errorlevel 1 (
  echo.
  echo Run failed. If pywebview is missing:  pip install pywebview
  echo PDF input needs pymupdf:             pip install pymupdf
  pause
)
