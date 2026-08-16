@echo off
setlocal

set "KTRADER_DIR=E:\Option_Trade\Options\KTrader Simulator"
set "KTRADER_PYTHON=%KTRADER_DIR%\.venv\Scripts\pythonw.exe"

if not exist "%KTRADER_PYTHON%" (
    echo KTrader Simulator Python environment was not found:
    echo %KTRADER_PYTHON%
    pause
    exit /b 1
)

start "KTrader Simulator" /D "%KTRADER_DIR%" "%KTRADER_PYTHON%" -m ktrader_simulator
exit /b 0
