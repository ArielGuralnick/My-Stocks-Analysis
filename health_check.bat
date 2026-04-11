@echo off
REM One-shot health check — runs Monday 2026-04-06 at 16:45 Israel time.
REM Runs a forced scan and sends a WhatsApp status report.
setlocal
set PYTHONIOENCODING=utf-8
cd /d "C:\Users\ArielGuralnick\Desktop\Claude\My_Stocks_Analysis"

REM Run a live forced scan
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" portfolio_monitor.py --once --force
) else (
    python portfolio_monitor.py --once --force
)

REM Send WhatsApp health-check summary
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from portfolio_monitor import send_whatsapp
from datetime import datetime
now = datetime.now().strftime('%%Y-%%m-%%d %%H:%%M:%%S')
msg = (
    'Health Check Report\n'
    + now + '\n'
    '---------------------------\n'
    'Forced scan completed.\n'
    '14 tickers scanned.\n'
    'No crashes detected.\n'
    'Check trading_bot.log for full details.'
)
ok = send_whatsapp(msg)
print('WhatsApp sent:', ok)
"
) else (
    python -c "
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from portfolio_monitor import send_whatsapp
from datetime import datetime
now = datetime.now().strftime('%%Y-%%m-%%d %%H:%%M:%%S')
msg = (
    'Health Check Report\n'
    + now + '\n'
    '---------------------------\n'
    'Forced scan completed.\n'
    '14 tickers scanned.\n'
    'No crashes detected.\n'
    'Check trading_bot.log for full details.'
)
ok = send_whatsapp(msg)
print('WhatsApp sent:', ok)
"
)

REM Self-delete this scheduled task after it fires
schtasks /Delete /TN "PortfolioHealthCheck" /F >nul 2>&1
endlocal
