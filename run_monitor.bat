@echo off
REM Wrapper launched by Windows Task Scheduler every 2 hours.
REM The Python script logs to trading_bot.log via the logging module.
setlocal
set PYTHONIOENCODING=utf-8
cd /d "C:\Users\ArielGuralnick\Desktop\Claude\My_Stocks_Analysis"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" portfolio_monitor.py --once
) else (
    python portfolio_monitor.py --once
)
endlocal
