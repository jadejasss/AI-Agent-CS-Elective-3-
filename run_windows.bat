@echo off
setlocal
cd /d "%~dp0"
title Lost and Found AI

echo =============================================
echo       LOST AND FOUND AI - STARTING SERVER
echo =============================================
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    set PYTHON=py
) else (
    set PYTHON=python
)

echo [1/2] Checking and installing required packages...
%PYTHON% -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: Could not install Flask. Check your internet connection and Python installation.
    pause
    exit /b 1
)

echo.
echo [2/2] Starting Flask application...
echo.
echo IMPORTANT: Open this address in Chrome:
echo http://127.0.0.1:5000
 echo.
echo Do NOT use VS Code Live Server for this application.
echo Keep this window open while using the system.
echo Press CTRL+C to stop the server.
echo.
%PYTHON% app.py
pause
