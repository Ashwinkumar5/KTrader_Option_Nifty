@echo off
setlocal
set "PROJECT_ROOT=E:\Option_Trade\Options"
set "WATCHDOG_CONFIG=%PROJECT_ROOT%\process_watch_dog\watchdog_config.json"
set "WATCHDOG_PYTHON=%PROJECT_ROOT%\myenv\Scripts\python.exe"

if not exist "%WATCHDOG_PYTHON%" (
    echo ERROR: Python environment not found: %WATCHDOG_PYTHON%
    pause
    exit /b 1
)

if not exist "%WATCHDOG_CONFIG%" (
    echo ERROR: Watchdog configuration not found: %WATCHDOG_CONFIG%
    pause
    exit /b 1
)

cd /d "%PROJECT_ROOT%"
title Process Watchdog
"%WATCHDOG_PYTHON%" -m process_watch_dog --config "%WATCHDOG_CONFIG%" run
set "WATCHDOG_EXIT=%ERRORLEVEL%"

if not "%WATCHDOG_EXIT%"=="0" (
    echo.
    echo Process Watchdog stopped with exit code %WATCHDOG_EXIT%.
    pause
)

endlocal & exit /b %WATCHDOG_EXIT%
