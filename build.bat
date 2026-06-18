@echo off
echo ========================================
echo   J-Link RTT Logger - Build EXE
echo ========================================
echo.

:: Activate venv
if exist "venv\" (
    call venv\Scripts\activate.bat
) else (
    echo [!] Virtual environment not found. Run setup.bat first.
    pause
    exit /b 1
)

:: Clean old build
echo [*] Cleaning old build...
if exist "build\" rmdir /s /q "build"
if exist "jlink-rttlog.spec" del /q "jlink-rttlog.spec"
if exist "jlink-rttlog.exe" del /q "jlink-rttlog.exe"

:: Build
echo [*] Building jlink-rttlog.exe...
python -m PyInstaller --onefile --name jlink-rttlog --distpath . jlink_rttlog.py

if errorlevel 1 (
    echo [!] Build failed!
    pause
    exit /b 1
)

:: Clean artifacts
echo [*] Cleaning build artifacts...
rmdir /s /q "build"
del /q "jlink-rttlog.spec"
echo.
echo [OK] Build complete: jlink-rttlog.exe
pause
