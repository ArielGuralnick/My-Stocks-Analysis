# My Stocks Analysis — Portfolio Monitor

An automated stock portfolio monitoring bot that combines **technical analysis** with **AI-powered fundamental analysis** to send WhatsApp alerts when high-probability trade signals are detected.

## Features

- Loads portfolio from an Excel file (Excellence brokerage export)
- Technical analysis using a 2-out-of-4 confluence filter:
  - SMA200 (trend direction)
  - RSI14 (momentum)
  - MACD (signal crossover)
  - Bollinger Bands (volatility breakout)
- AI validation via **Claude (Anthropic)** — analyzes recent news headlines before firing an alert
- WhatsApp alerts via **Green API**
- 48-hour cooldown to prevent duplicate alerts
- Runs automatically every 2 hours via **Windows Task Scheduler**
- Full logging to file and console

## Monitored Assets

US stocks: AMZN, AAPL, META, MSFT, NVDA, ORCL, TSLA
Crypto ETF: BITB
Mining: IREN
ETFs: included in portfolio

## Setup

1. Clone the repo
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in your credentials:
   ```
   GREEN_API_ID_INSTANCE=your_id
   GREEN_API_TOKEN_INSTANCE=your_token
   WHATSAPP_PHONE_NUMBER=your_number
   ANTHROPIC_API_KEY=your_key
   ANTHROPIC_MODEL=claude-sonnet-4-6
   ```
4. Place your Excel portfolio file at the path defined in `portfolio_monitor.py`
5. Run once to test:
   ```bash
   python portfolio_monitor.py --once
   ```
6. Schedule with Task Scheduler:
   ```bash
   install_task.bat
   ```

## Project Structure

```
portfolio_monitor.py     # Main monitoring script
requirements.txt         # Python dependencies
.env.example             # Credentials template
run_monitor.bat          # Task Scheduler wrapper
install_task.bat         # Register scheduled task
health_check.bat         # One-shot health check
```

## Tech Stack

- Python 3.9+
- yfinance, pandas, numpy, ta
- Anthropic Claude API
- Green API (WhatsApp)
- Windows Task Scheduler
