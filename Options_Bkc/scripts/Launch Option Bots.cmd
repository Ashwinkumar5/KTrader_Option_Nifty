@echo off
setlocal
for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
call "%PROJECT_ROOT%\process_watch_dog\Run Watchdog.cmd"
set "WATCHDOG_EXIT=%ERRORLEVEL%"
endlocal & exit /b %WATCHDOG_EXIT%
