@echo off
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install it from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Installing build tools...
python -m pip install --upgrade pyinstaller psutil requests pywebview

echo Building MultiRBX.exe...
python -m PyInstaller --onefile --noconsole --name MultiRBX --collect-all webview --collect-all pythonnet --hidden-import clr multi_rbx.py

echo.
if exist "dist\MultiRBX.exe" (
    echo Done. Your exe is here: %~dp0dist\MultiRBX.exe
) else (
    echo Build failed - check the messages above.
)
pause
