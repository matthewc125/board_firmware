@echo off
cd /d "%~dp0\.."

echo Backing up board_firmware.db...
py -3 pipeline\backup_db.py
if errorlevel 1 (
    echo Backup failed.
    pause
    exit /b 1
)

echo.
echo Backups are stored in backups\
pause
