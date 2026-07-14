@echo off
cd /d "%~dp0\.."

if not exist "board_firmware.db" (
    echo board_firmware.db not found.
    pause
    exit /b 1
)

if not exist "backups" mkdir "backups"

echo Checkpointing database...
py -3 -c "import sqlite3; c=sqlite3.connect('board_firmware.db'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"
if errorlevel 1 (
    echo Checkpoint failed.
    pause
    exit /b 1
)

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set STAMP=%%I
set DEST=backups\board_firmware_%STAMP%.db.bak

echo Creating %DEST%...
copy /Y "board_firmware.db" "%DEST%" >nul
if errorlevel 1 (
    echo Backup failed.
    pause
    exit /b 1
)

echo.
echo Backup saved: %DEST%
pause
