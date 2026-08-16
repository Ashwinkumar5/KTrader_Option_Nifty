@echo off
setlocal
set "WATCHDOG_DIR=%~dp0"
for %%I in ("%WATCHDOG_DIR%..") do set "PROJECT_ROOT=%%~fI"
pushd "%PROJECT_ROOT%"
"%PROJECT_ROOT%\myenv\Scripts\python.exe" -m process_watch_dog --config "%WATCHDOG_DIR%watchdog_config.json" run
set "WATCHDOG_EXIT=%ERRORLEVEL%"
popd
endlocal & exit /b %WATCHDOG_EXIT%
