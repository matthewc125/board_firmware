@echo off
cd /d "%~dp0\.."

set IMPORT_ROOT=%~dp0..\pipeline\netapp_import
set DB=%~dp0..\board_firmware.db

if not exist "%IMPORT_ROOT%" (
    echo Import folder not found: %IMPORT_ROOT%
    echo Run scripts\Copy Column Electronics Docs.bat first.
    pause
    exit /b 1
)

echo NetApp hardware import
echo   Source: %IMPORT_ROOT%
echo   Database: %DB%
echo.

echo Step 1: Preview update-only (existing boards)...
py -3 pipeline\import_netapp.py --import-root "%IMPORT_ROOT%" --db "%DB%" --dry-run
echo.

set /p CREATE=Create missing boards too? [y/N]:
if /I "%CREATE%"=="Y" (
    py -3 pipeline\import_netapp.py --import-root "%IMPORT_ROOT%" --db "%DB%" --create-missing
) else (
    py -3 pipeline\import_netapp.py --import-root "%IMPORT_ROOT%" --db "%DB%"
)

echo.
pause
