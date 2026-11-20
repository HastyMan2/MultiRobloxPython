@echo off
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)

python -c "import psutil, requests, webview" >nul 2>nul
if errorlevel 1 (
    echo Installing required packages...
    python -m pip install --quiet psutil requests pywebview
)

start "" pythonw "%~dp0multi_rbx.py"
