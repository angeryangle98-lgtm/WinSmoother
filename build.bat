@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if %errorlevel%==0 (
    set PY=py
) else (
    set PY=python
)

echo Installing/updating PyInstaller...
%PY% -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo PyInstaller installation failed.
    pause
    exit /b 1
)

echo.
echo Building WinSmoother 5.2 ...
%PY% -m PyInstaller --onefile --windowed --clean --noconfirm --uac-admin ^
    --name WinSmootherBot --icon "assets\icon.ico" --add-data "assets;assets" main.py
if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
)

echo.
echo Build complete: dist\WinSmootherBot.exe
echo.
pause
