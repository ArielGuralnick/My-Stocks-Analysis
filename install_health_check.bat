@echo off
REM Registers a one-shot health check task for Monday 2026-04-06 at 16:45 local time.
schtasks /Delete /TN "PortfolioHealthCheck" /F >nul 2>&1
schtasks /Create ^
    /TN "PortfolioHealthCheck" ^
    /TR "\"C:\Users\ArielGuralnick\Desktop\Claude\My_Stocks_Analysis\health_check.bat\"" ^
    /SC ONCE ^
    /SD 04/06/2026 ^
    /ST 16:45 ^
    /RL LIMITED ^
    /F
echo.
echo Health check scheduled for Monday 06/04/2026 at 16:45 local time.
echo You will receive a WhatsApp message when it runs.
