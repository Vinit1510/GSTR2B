@echo off
title GSTR-2B Downloader Launcher
echo =========================================
echo    GSTR-2B Downloader First-Time Setup
echo =========================================
echo.

REM Check if Python is installed (check python then py)
set PYTHON_CMD=python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    set PYTHON_CMD=py
    py --version >nul 2>&1
    if %errorlevel% neq 0 (
        echo [ERROR] Python is not installed or not added to PATH!
        echo Please install Python 3 from python.org and check the box "Add Python to PATH".
        pause
        exit /b
    )
)

echo [1/3] Setting up the environment (using %PYTHON_CMD%)...
if not exist .venv (
    %PYTHON_CMD% -m venv .venv
)
call .venv\Scripts\activate

echo [2/3] Checking and installing required files (takes a moment)...
%PYTHON_CMD% -m pip install --upgrade pip >nul 2>&1
pip install -r requirements.txt >nul 2>&1

echo [3/3] Downloading automation browser (if needed)...
%PYTHON_CMD% -m playwright install chromium >nul 2>&1

echo.
echo =========================================
echo    Setup Complete! Launching Software...
echo =========================================
%PYTHON_CMD% run.py

pause
