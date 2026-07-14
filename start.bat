@echo off
title AudioFileManager
cd /d "%~dp0"

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python and add it to PATH.
    pause
    exit /b 1
)

python main.py %*

echo.
pause
