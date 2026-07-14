@echo off
cd /d "%~dp0\.."
py -3 run.py
if errorlevel 1 (
    echo.
    echo Failed to start. Make sure Python 3 is installed.
    echo Try: py -3 -m pip install -r requirements.txt
    pause
)
