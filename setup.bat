@echo off
echo ========================================
echo   J-Link RTT Logger - Environment Setup
echo ========================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [!] Python not found. Please install Python 3.10+ first.
    pause
    exit /b 1
)
python --version
echo.

:: Create venv if not exists
if not exist "venv\" (
    echo [*] Creating virtual environment...
    python -m venv venv
)

:: Activate venv
echo [*] Activating virtual environment...
call venv\Scripts\activate.bat

:: Install dependencies
echo [*] Installing dependencies...
pip install -r requirements.txt
echo.
echo [OK] Setup complete.
echo.
echo Run the script:  python jlink_rttlog.py
echo Build EXE:        build.bat
pause
