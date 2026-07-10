@echo off
cd /d "%~dp0\.."

echo Starting Board Firmware Log (full archive, side database)...
echo Main app still uses board_firmware.db — this is browse-only.
echo.

set DATABASE_PATH=%CD%\board_firmware_full.db
set FLASK_PORT=5001

py -3 app.py
if errorlevel 1 (
    echo.
    echo Failed to start. Build the archive first: scripts\Build Full Database.bat
    pause
    exit /b 1
)
