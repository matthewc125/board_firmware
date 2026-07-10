@echo off
cd /d "%~dp0\.."

echo Building board_firmware_full.db from all sources...
py -3 pipeline\build_full_db.py
if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
)

echo.
echo Full archive: board_firmware_full.db
pause
