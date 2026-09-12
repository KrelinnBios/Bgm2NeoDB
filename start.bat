@echo off
title Bgm2NeoDB
echo Starting Bgm2NeoDB...
rem Keep batch commands ASCII so cmd.exe also accepts LF-only source archives.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating the project Python environment...
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 -m venv .venv
    ) else (
        where python >nul 2>&1
        if errorlevel 1 goto :missing_python
        python -m venv .venv
    )
    if errorlevel 1 goto :setup_failed
)

echo Checking project dependencies...
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, httpx, jinja2, keyring" >nul 2>&1
if errorlevel 1 (
    echo Installing project dependencies. The first run may take a moment...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :setup_failed
)

echo Launching the local web interface...
".venv\Scripts\python.exe" -u main.py %*
set "exit_code=%errorlevel%"
if not "%exit_code%"=="0" pause
exit /b %exit_code%

:missing_python
echo Python was not found. Install Python 3.11 or newer and add it to PATH.
pause
exit /b 1

:setup_failed
echo Setup failed. Check your Python installation and network connection, then try again.
pause
exit /b 1
