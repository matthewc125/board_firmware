@echo off
cd /d "%~dp0\.."

set INDEX=%USERPROFILE%\Documents\Column_Electronics_hardware_docs.txt
set DEST=%~dp0..\pipeline\netapp_import

if not exist "%INDEX%" (
    echo Index not found: %INDEX%
    pause
    exit /b 1
)

echo Copying hardware tracking docs from NetApp...
echo Source list: %INDEX%
echo Destination: %DEST%
echo.

py -3 pipeline\copy_parse_files.py --from-index "%INDEX%" --dest "%DEST%"
if errorlevel 1 (
    echo.
    echo Copy failed. Check that NetApp is reachable and Python 3 is installed.
    pause
    exit /b 1
)

echo.
echo Done. Files are in pipeline\netapp_import\
pause
