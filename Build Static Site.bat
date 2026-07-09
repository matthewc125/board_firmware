@echo off
setlocal
cd /d "%~dp0"

py static_site\build.py
if errorlevel 1 (
  echo Build failed.
  pause
  exit /b 1
)

echo.
echo Built site\ folder. Preview with:
echo   cd site
echo   py -m http.server 8080
echo Then open http://127.0.0.1:8080/
echo.
pause
