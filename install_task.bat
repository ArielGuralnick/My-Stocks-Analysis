@echo off
REM Registers (or re-registers) the scheduled task that runs the monitor every 2 hours.
schtasks /Delete /TN "PortfolioConfluenceMonitor" /F >nul 2>&1
schtasks /Create ^
    /TN "PortfolioConfluenceMonitor" ^
    /TR "\"C:\Users\ArielGuralnick\Desktop\Claude\My_Stocks_Analysis\run_monitor.bat\"" ^
    /SC HOURLY /MO 2 ^
    /ST 09:50 ^
    /RL LIMITED ^
    /F
