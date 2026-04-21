"""
Portfolio Hybrid Signal Monitor (Technical + AI Fundamental)
------------------------------------------------------------
Monitors a stock portfolio loaded from an Excel file for high-probability
BUY signals using a HYBRID workflow:

    Step 1 — Technical Filter (2-out-of-4 rule)
        Evaluate 4 independent BUY indicators:
            • SMA200       (price above 200-day moving average → uptrend)
            • RSI14        (RSI below 35 → oversold)
            • MACD         (recent bullish crossover)
            • Bollinger    (close at/below lower band → mean-reversion)
        A stock passes ONLY IF ≥ 2 of the 4 indicators fire.

    Step 2 — News Fetching
        If the technical score ≥ 2, fetch the top 5 most recent news
        headlines for that ticker via yfinance. If there is no news,
        skip the stock.

    Step 3 — LLM Fundamental Analysis (Anthropic API, JSON output)
        Send the headlines to Claude and require a strict JSON response:
            {
              "sentiment": "BUY" | "SELL" | "HOLD",
              "analysis":  "<3-sentence fundamental analysis>"
            }

    Step 4 — WhatsApp Alert (Green API)
        Only if sentiment == "BUY", send a nicely formatted WhatsApp
        message containing:
            • Ticker symbol
            • Technical score (e.g. 2/4) + which indicators triggered
            • The AI's fundamental analysis paragraph
        (Raw headlines are NEVER sent to WhatsApp.)

Production features:
    • US market-hours gate (Mon–Fri, 9:30–16:00 America/New_York, DST-aware)
    • 48-hour anti-spam cooldown persisted in signals_state.json
    • Robust try/except around every network call (yfinance, Anthropic, Green API)
    • Robust JSON parsing of the LLM response (handles fences / extra prose)
    • Unified logging to both console and trading_bot.log (UTF-8)
    • Safe for both --once (Task Scheduler) and continuous-loop modes

Author: Ariel Guralnick
"""

from __future__ import annotations

import argparse
import json
import math
import logging
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Ensure Hebrew / emoji print correctly in Windows consoles and log files.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

import pandas as pd
import requests
import yaml
import yfinance as yf
from dotenv import load_dotenv
from ta.momentum import RSIIndicator
from ta.trend import MACD, SMAIndicator
from ta.volatility import BollingerBands

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env", override=True)

# Load user config (config.yaml) — non-secret settings live here.
def _load_config() -> dict:
    cfg_path = BASE_DIR / "config.yaml"
    if cfg_path.exists():
        try:
            with cfg_path.open("r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            print(f"[WARNING] Could not load config.yaml: {e}", file=sys.stderr)
    return {}

_cfg = _load_config()
_cfg_excel = _cfg.get("excel", {})

# Green API (WhatsApp)
GREEN_API_ID_INSTANCE = os.getenv("GREEN_API_ID_INSTANCE")
GREEN_API_TOKEN_INSTANCE = os.getenv("GREEN_API_TOKEN_INSTANCE")
WHATSAPP_PHONE_NUMBER = os.getenv("WHATSAPP_PHONE_NUMBER")  # e.g. 972501234567

# Anthropic API (LLM fundamental analysis)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# Excel settings: config.yaml > .env > auto-detect
EXCEL_FILE = _cfg_excel.get("file") or os.getenv("EXCEL_FILE", "")
if not EXCEL_FILE:
    # Fallback: look for the most recent Excellence_*.xlsx in Downloads
    _downloads = Path.home() / "Downloads"
    _candidates = sorted(_downloads.glob("Excellence_*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    if _candidates:
        EXCEL_FILE = str(_candidates[0])

TICKER_COLUMN = _cfg_excel.get("ticker_column") or os.getenv("TICKER_COLUMN", "שם נייר")
HEADER_ROW = int(_cfg_excel.get("header_row") if _cfg_excel.get("header_row") is not None else os.getenv("HEADER_ROW", "9"))
CHECK_INTERVAL_SECONDS = 2 * 60 * 60   # 2 hours (continuous mode)

DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "signals_state.json"
SCAN_HISTORY_FILE = DATA_DIR / "scan_history.json"
LOG_FILE = DATA_DIR / "trading_bot.log"

COOLDOWN_HOURS = 48                    # Per-ticker cooldown
MIN_SCAN_INTERVAL_MINUTES = 25         # Minimum minutes between scans (prevents duplicate runs on restart)
MARKET_TZ = ZoneInfo("America/New_York")
API_MAX_RETRIES = 3                    # Retry count for Anthropic & Green API
API_RETRY_DELAY = 5                    # Seconds between retries

# Global lock: only one scan may run at a time (prevents concurrent scans from duplicate scheduler instances)
_scan_lock = threading.Lock()

NEWS_HEADLINE_LIMIT = 5
TECHNICAL_SCORE_THRESHOLD = 3          # ≥ 3 of 4 indicators must fire

# Portfolio names (as they appear in the Excel) → Yahoo Finance tickers.
# Loaded from config.yaml; falls back to hardcoded defaults if not set.
_DEFAULT_TICKER_MAP: dict[str, str] = {
    "AMAZON COM INC": "AMZN",
    "APPLE COMPUTER": "AAPL",
    "BITB": "BITB",
    "IREN LTD": "IREN",
    "META PLATFORMS INC": "META",
    "MICROSOFT CORP": "MSFT",
    "MICROSTRATEGY INC": "MSTR",
    "NVIDIA CORP": "NVDA",
    "ORACLE CORPORATION": "ORCL",
    "TESLA MOTORS INC": "TSLA",
    "(ISHARES CORE MSCI EUROPE UCITS ETF EUR (ACC": "IMEU.L",
    "ISHARES CORE MSCI EM IMI UCITS ETF": "EIMI.L",
    "ISHARES CORE S&P 500 UCITS ETF": "CSPX.L",
    "ISHARES NASDAQ 100 UCITS ETF": "CNDX.L",
}
TICKER_MAP: dict[str, str] = _cfg.get("ticker_map") or _DEFAULT_TICKER_MAP

# Rough US market holidays (NYSE full closures). Not exhaustive across years;
# extend as needed. Dates are ISO (YYYY-MM-DD) in America/New_York.
US_MARKET_HOLIDAYS: set[str] = {
    # 2025
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


# ---------------------------------------------------------------------------
# LOGGING SETUP
# ---------------------------------------------------------------------------

def _build_logger() -> logging.Logger:
    logger = logging.getLogger("portfolio_monitor")
    if logger.handlers:  # already configured (e.g. re-import)
        return logger
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    logger.propagate = False
    return logger


log = _build_logger()


# ---------------------------------------------------------------------------
# MARKET HOURS
# ---------------------------------------------------------------------------

def is_us_market_open(now: Optional[datetime] = None) -> tuple[bool, str]:
    """Return (open, reason). Regular US hours: Mon–Fri 09:30–16:00 ET,
    excluding known NYSE full-day closures."""
    now = now or datetime.now(tz=MARKET_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=MARKET_TZ)
    else:
        now = now.astimezone(MARKET_TZ)

    if now.weekday() >= 5:
        return False, f"weekend ({now.strftime('%A')})"

    iso_date = now.strftime("%Y-%m-%d")
    if iso_date in US_MARKET_HOLIDAYS:
        return False, f"US market holiday ({iso_date})"

    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)

    if now < market_open:
        return False, f"pre-market (ET {now.strftime('%H:%M')})"
    if now >= market_close:
        return False, f"after-hours (ET {now.strftime('%H:%M')})"

    return True, f"open (ET {now.strftime('%H:%M')})"


# ---------------------------------------------------------------------------
# STATE (anti-spam cooldown)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """State shape: {ticker: {"BUY": iso_utc}}."""
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log.warning("signals_state.json is not a dict — resetting.")
            return {}
        return data
    except Exception as e:
        log.error("Failed to read signals_state.json: %s — resetting.", e)
        return {}


def save_state(state: dict) -> None:
    try:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        tmp.replace(STATE_FILE)
    except Exception as e:
        log.error("Failed to write signals_state.json: %s", e)


# ---------------------------------------------------------------------------
# SCAN HISTORY (for dashboard)
# ---------------------------------------------------------------------------

SCAN_HISTORY_RETENTION_DAYS = 14  # Keep scans from the last 14 days

def load_scan_history() -> list[dict]:
    if not SCAN_HISTORY_FILE.exists():
        return []
    try:
        with SCAN_HISTORY_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _prune_old_scans(history: list[dict]) -> list[dict]:
    """Remove scan records older than SCAN_HISTORY_RETENTION_DAYS."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=SCAN_HISTORY_RETENTION_DAYS)
    result = []
    for record in history:
        try:
            ts = datetime.fromisoformat(record["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                result.append(record)
        except Exception:
            result.append(record)  # Keep records with unparseable timestamps
    return result


def _sanitize_nan(obj):
    """Recursively replace NaN/Infinity floats with None so the JSON output
    is valid (JS JSON.parse rejects NaN/Infinity)."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_nan(v) for v in obj]
    return obj


def save_scan_record(record: dict) -> None:
    history = load_scan_history()
    history.insert(0, _sanitize_nan(record))
    history = _prune_old_scans(history)
    try:
        tmp = SCAN_HISTORY_FILE.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
        tmp.replace(SCAN_HISTORY_FILE)
    except Exception as e:
        log.error("Failed to write scan_history.json: %s", e)


def is_in_cooldown(state: dict, ticker: str, signal_type: str) -> bool:
    entry = state.get(ticker, {}).get(signal_type)
    if not entry:
        return False
    try:
        last = datetime.fromisoformat(entry)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    age = datetime.now(tz=timezone.utc) - last
    return age < timedelta(hours=COOLDOWN_HOURS)


def mark_alerted(state: dict, ticker: str, signal_type: str) -> None:
    state.setdefault(ticker, {})[signal_type] = datetime.now(tz=timezone.utc).isoformat()
    save_state(state)


# ---------------------------------------------------------------------------
# EXCEL + TICKER RESOLUTION
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    """Collapse all whitespace so 'שם  נייר' == 'שם נייר'."""
    return re.sub(r"\s+", " ", str(s)).strip()


def load_portfolio_names(path: str) -> list[str]:
    df = pd.read_excel(path, header=HEADER_ROW)
    df.columns = [_normalize(c) for c in df.columns]
    target = _normalize(TICKER_COLUMN)
    if target not in df.columns:
        raise ValueError(
            f"Column '{TICKER_COLUMN}' not found. Available: {list(df.columns)}"
        )
    return [str(n).strip() for n in df[target].dropna().tolist()]


def resolve_tickers(names: list[str]) -> list[tuple[str, str]]:
    resolved, skipped = [], []
    for n in names:
        if n in TICKER_MAP:
            resolved.append((n, TICKER_MAP[n]))
        else:
            skipped.append(n)
    if skipped:
        log.warning("No Yahoo Finance ticker mapping for %d names (add to TICKER_MAP):", len(skipped))
        for s in skipped:
            log.warning("   - %s", s)
    return resolved


# ---------------------------------------------------------------------------
# DATA + INDICATORS
# ---------------------------------------------------------------------------

def fetch_history(ticker: str, period: str = "2y") -> Optional[pd.DataFrame]:
    """Fetch daily OHLCV from Yahoo Finance. Never raises — returns None on any error."""
    try:
        df = yf.download(
            ticker,
            period=period,
            interval="1d",
            auto_adjust=True,
            progress=False,
        )
    except Exception as e:
        log.error("yfinance download failed for %s: %s", ticker, e)
        return None

    try:
        if df is None or df.empty:
            log.warning("No data returned for %s", ticker)
            return None
        if len(df) < 210:
            log.warning("Insufficient history for %s (%d rows; need >=210)", ticker, len(df))
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        log.error("Error post-processing yfinance data for %s: %s", ticker, e)
        return None



def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Append SMA200, RSI14, MACD(12,26,9), BBands(20,2) columns."""
    df = df.copy()
    close = df["Close"].astype(float)

    df["SMA_200"] = SMAIndicator(close=close, window=200, fillna=False).sma_indicator()
    df["RSI_14"] = RSIIndicator(close=close, window=14, fillna=False).rsi()

    macd = MACD(close=close, window_slow=26, window_fast=12, window_sign=9, fillna=False)
    df["MACD"] = macd.macd()
    df["MACD_SIGNAL"] = macd.macd_signal()
    df["MACD_HIST"] = macd.macd_diff()

    bb = BollingerBands(close=close, window=20, window_dev=2, fillna=False)
    df["BB_LOWER"] = bb.bollinger_lband()
    df["BB_MID"] = bb.bollinger_mavg()
    df["BB_UPPER"] = bb.bollinger_hband()

    return df


# ---------------------------------------------------------------------------
# TECHNICAL SIGNAL LOGIC (2-out-of-4 rule)
# ---------------------------------------------------------------------------

def macd_crossed_up_recently(df: pd.DataFrame, lookback: int = 5) -> bool:
    """True if MACD crossed above its signal line within the last `lookback` bars."""
    sub = df.tail(lookback + 1)
    for i in range(1, len(sub)):
        pm, ps = sub["MACD"].iloc[i - 1], sub["MACD_SIGNAL"].iloc[i - 1]
        cm, cs = sub["MACD"].iloc[i], sub["MACD_SIGNAL"].iloc[i]
        if pd.isna(pm) or pd.isna(cm):
            continue
        if pm <= ps and cm > cs:
            return True
    return False


def macd_crossed_down_recently(df: pd.DataFrame, lookback: int = 5) -> bool:
    """True if MACD crossed BELOW its signal line within the last `lookback` bars."""
    sub = df.tail(lookback + 1)
    for i in range(1, len(sub)):
        pm, ps = sub["MACD"].iloc[i - 1], sub["MACD_SIGNAL"].iloc[i - 1]
        cm, cs = sub["MACD"].iloc[i], sub["MACD_SIGNAL"].iloc[i]
        if pd.isna(pm) or pd.isna(cm):
            continue
        if pm >= ps and cm < cs:
            return True
    return False


def evaluate_technical_signals(df: pd.DataFrame) -> dict:
    """Evaluate 4 INDEPENDENT BUY indicators AND 4 INDEPENDENT SELL indicators.

    A side passes the filter when at least TECHNICAL_SCORE_THRESHOLD (=3) of
    its 4 indicators fire. BUY and SELL are evaluated independently in the
    same scan.

    BUY indicators:
        1. SMA200     — close > 200-day SMA  (long-term uptrend)
        2. RSI14      — RSI < 35             (oversold)
        3. MACD       — bullish crossover within last 5 bars
        4. Bollinger  — close ≤ lower band   (mean-reversion buy zone)

    SELL indicators:
        1. SMA200     — close < 200-day SMA  (long-term downtrend)
        2. RSI14      — RSI > 70             (overbought)
        3. MACD       — bearish crossover within last 5 bars
        4. Bollinger  — close ≥ upper band   (overextended)
    """
    last = df.iloc[-1]
    close = float(last["Close"])
    sma200 = float(last["SMA_200"])
    rsi = float(last["RSI_14"])
    bb_lower = float(last["BB_LOWER"])
    bb_upper = float(last["BB_UPPER"])
    macd_val = float(last["MACD"])
    macd_sig = float(last["MACD_SIGNAL"])

    # --- 4 independent BUY indicators ---
    sma_buy = close > sma200
    rsi_buy = rsi < 35
    macd_buy = macd_crossed_up_recently(df, lookback=5)
    bb_buy = close <= bb_lower

    buy_triggered: list[dict] = []
    if sma_buy:
        buy_triggered.append({
            "name": "SMA200",
            "detail": f"Close ${close:.2f} > SMA200 ${sma200:.2f} (uptrend)",
        })
    if rsi_buy:
        buy_triggered.append({
            "name": "RSI14",
            "detail": f"RSI {rsi:.2f} < 35 (oversold)",
        })
    if macd_buy:
        buy_triggered.append({
            "name": "MACD",
            "detail": f"Bullish crossover (MACD {macd_val:.4f} vs Signal {macd_sig:.4f})",
        })
    if bb_buy:
        buy_triggered.append({
            "name": "Bollinger",
            "detail": f"Close ${close:.2f} ≤ BB lower ${bb_lower:.2f}",
        })

    # --- 4 independent SELL indicators ---
    sma_sell = close < sma200
    rsi_sell = rsi > 70
    macd_sell = macd_crossed_down_recently(df, lookback=5)
    bb_sell = close >= bb_upper

    sell_triggered: list[dict] = []
    if sma_sell:
        sell_triggered.append({
            "name": "SMA200",
            "detail": f"Close ${close:.2f} < SMA200 ${sma200:.2f} (downtrend)",
        })
    if rsi_sell:
        sell_triggered.append({
            "name": "RSI14",
            "detail": f"RSI {rsi:.2f} > 70 (overbought)",
        })
    if macd_sell:
        sell_triggered.append({
            "name": "MACD",
            "detail": f"Bearish crossover (MACD {macd_val:.4f} vs Signal {macd_sig:.4f})",
        })
    if bb_sell:
        sell_triggered.append({
            "name": "Bollinger",
            "detail": f"Close ${close:.2f} ≥ BB upper ${bb_upper:.2f}",
        })

    buy_score = len(buy_triggered)
    sell_score = len(sell_triggered)

    return {
        "date": df.index[-1].strftime("%Y-%m-%d"),
        "close": close,
        "sma200": sma200,
        "rsi": rsi,
        "bb_lower": bb_lower,
        "bb_upper": bb_upper,
        "macd": macd_val,
        "macd_signal": macd_sig,
        # BUY side
        "buy_score": buy_score,
        "buy_triggered": buy_triggered,
        "buy_passes": buy_score >= TECHNICAL_SCORE_THRESHOLD,
        # SELL side
        "sell_score": sell_score,
        "sell_triggered": sell_triggered,
        "sell_passes": sell_score >= TECHNICAL_SCORE_THRESHOLD,
    }


# ---------------------------------------------------------------------------
# BACKTEST
# ---------------------------------------------------------------------------

def run_backtest(df: pd.DataFrame) -> Optional[dict]:
    """Simulate the 3/4-signal strategy over the full 2-year history of df.

    Logic mirrors the live scan:
      - BUY  signal: 3+ of (SMA200-above, RSI<35, MACD-cross-up-5bar, BB-lower-touch)
      - SELL signal: 3+ of (SMA200-below, RSI>70, MACD-cross-down-5bar, BB-upper-touch)
      - Entry: buy at next day's open after a BUY signal (not already in position)
      - Exit:  sell at next day's open after a SELL signal (while in position)
      - Open position at end of history is closed at the last close price.

    Returns a dict with strategy_return, hold_return, num_trades, win_rate,
    or None if there is not enough data.
    """
    required_cols = {"Close", "Open", "SMA_200", "RSI_14", "MACD", "MACD_SIGNAL",
                     "BB_LOWER", "BB_UPPER"}
    if len(df) < 210 or not required_cols.issubset(df.columns):
        return None

    close      = df["Close"]
    open_price = df["Open"]
    sma200     = df["SMA_200"]
    rsi        = df["RSI_14"]
    macd       = df["MACD"]
    macd_sig   = df["MACD_SIGNAL"]
    bb_lower   = df["BB_LOWER"]
    bb_upper   = df["BB_UPPER"]

    # Vectorised MACD crossover days (same logic as macd_crossed_*_recently)
    cross_up   = (macd >= macd_sig) & (macd.shift(1) < macd_sig.shift(1))
    cross_down = (macd < macd_sig)  & (macd.shift(1) >= macd_sig.shift(1))
    macd_buy_ind  = cross_up.rolling(5,  min_periods=1).max().astype(bool)
    macd_sell_ind = cross_down.rolling(5, min_periods=1).max().astype(bool)

    # Per-row signal scores
    buy_scores = (
        (close > sma200).astype(int) +
        (rsi < 35).astype(int) +
        macd_buy_ind.astype(int) +
        (close <= bb_lower).astype(int)
    )
    sell_scores = (
        (close < sma200).astype(int) +
        (rsi > 70).astype(int) +
        macd_sell_ind.astype(int) +
        (close >= bb_upper).astype(int)
    )

    buy_signals  = buy_scores  >= TECHNICAL_SCORE_THRESHOLD
    sell_signals = sell_scores >= TECHNICAL_SCORE_THRESHOLD

    # Trade simulation: execute at next-day open
    in_position = False
    entry_price = 0.0
    trades: list[float] = []

    for i in range(len(df) - 1):
        exec_price = float(open_price.iloc[i + 1])
        if not in_position and buy_signals.iloc[i]:
            entry_price = exec_price
            in_position = True
        elif in_position and sell_signals.iloc[i]:
            trades.append((exec_price - entry_price) / entry_price)
            in_position = False

    # Close any open position at last close
    if in_position:
        trades.append((float(close.iloc[-1]) - entry_price) / entry_price)

    # Compound returns
    strategy_mult = 1.0
    for t in trades:
        strategy_mult *= (1 + t)
    strategy_return = round((strategy_mult - 1) * 100, 1)

    # Buy-and-hold return over same period
    first_close = float(close.dropna().iloc[0])
    last_close  = float(close.iloc[-1])
    hold_return = round((last_close - first_close) / first_close * 100, 1)

    win_rate = round(sum(1 for t in trades if t > 0) / len(trades) * 100) if trades else 0

    return {
        "strategy_return": strategy_return,
        "hold_return":     hold_return,
        "num_trades":      len(trades),
        "win_rate":        win_rate,
    }


# ---------------------------------------------------------------------------
# NEWS FETCHING (yfinance)
# ---------------------------------------------------------------------------

def fetch_news(ticker: str, limit: int = NEWS_HEADLINE_LIMIT) -> list[str]:
    """Fetch the top N most recent news headlines for `ticker` via yfinance.
    Returns an empty list on any error or if no news is available."""
    try:
        t = yf.Ticker(ticker)
        news = t.news or []
    except Exception as e:
        log.error("yfinance news fetch failed for %s: %s", ticker, e)
        return []

    headlines: list[str] = []
    for item in news:
        if len(headlines) >= limit:
            break
        if not isinstance(item, dict):
            continue
        # yfinance has used several schemas over time:
        title = None
        content = item.get("content")
        if isinstance(content, dict):
            title = content.get("title")
        if not title:
            title = item.get("title")
        if title:
            headlines.append(str(title).strip())

    return headlines


# ---------------------------------------------------------------------------
# LLM FUNDAMENTAL ANALYSIS (Anthropic API, strict JSON)
# ---------------------------------------------------------------------------

_LLM_SYSTEM_PROMPT = (
    "You are a professional equity research analyst. You read recent news "
    "headlines about a stock and produce a concise fundamental sentiment "
    "judgement. You ALWAYS respond with a single valid JSON object and "
    "absolutely nothing else — no markdown, no code fences, no preamble, "
    "no commentary outside the JSON."
)


def _build_llm_user_prompt(ticker: str, headlines: list[str], side: str) -> str:
    """Build the user prompt. `side` is 'BUY' or 'SELL' — the technical side
    that triggered the lookup. The LLM is told what the technicals suggest
    but is still asked for an INDEPENDENT fundamental judgement."""
    headlines_block = "\n".join(f"{i+1}. {h}" for i, h in enumerate(headlines))
    side_context = (
        "Technical analysis is currently flashing a BUY signal "
        "(oversold / bullish setup). Decide whether the news fundamentally "
        "supports buying."
        if side == "BUY"
        else
        "Technical analysis is currently flashing a SELL signal "
        "(overbought / bearish setup). Decide whether the news fundamentally "
        "supports selling."
    )
    return (
        f"Ticker: {ticker}\n"
        f"Context: {side_context}\n\n"
        f"Recent news headlines:\n{headlines_block}\n\n"
        "Based ONLY on these headlines, decide the fundamental sentiment for "
        f"{ticker} and respond with a JSON object having EXACTLY these two keys:\n"
        '  "sentiment": one of "BUY", "SELL", or "HOLD"\n'
        '  "analysis":  a concise fundamental analysis of EXACTLY 3 sentences '
        "explaining the sentiment, referencing the headlines.\n\n"
        "Be honest and independent — if the news contradicts the technical setup, "
        'return "HOLD" or the opposite sentiment.\n\n'
        "Output ONLY the JSON object. No markdown. No code fences. No extra text.\n"
        'Example: {"sentiment": "BUY", "analysis": "Sentence one. Sentence two. Sentence three."}'
    )


def _parse_llm_json(text: str) -> Optional[dict]:
    """Robustly extract a {sentiment, analysis} JSON object from an LLM reply.

    Handles:
        • plain JSON
        • markdown code fences (```json ... ``` or ``` ... ```)
        • leading/trailing prose around a JSON object
    """
    if not text:
        return None

    cleaned = text.strip()

    # Strip markdown code fences if present.
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json|JSON)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    data = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fallback: extract the first {...} block (greedy, dot-all).
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError as e:
                log.error("LLM JSON parse failed even after regex extract: %s", e)
                return None

    if not isinstance(data, dict):
        log.error("LLM response is not a JSON object: %s", text[:200])
        return None

    sentiment = str(data.get("sentiment", "")).strip().upper()
    analysis = str(data.get("analysis", "")).strip()

    if sentiment not in {"BUY", "SELL", "HOLD"}:
        log.error("LLM returned invalid sentiment: %r", sentiment)
        return None
    if not analysis:
        log.error("LLM returned empty analysis field")
        return None

    return {"sentiment": sentiment, "analysis": analysis}


def analyze_with_llm(ticker: str, headlines: list[str], side: str) -> Optional[dict]:
    """Call the Anthropic API to analyze the headlines. `side` is the
    technical side that triggered the call ('BUY' or 'SELL'). Returns
    {"sentiment": "...", "analysis": "..."} or None on any failure."""
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY missing — skipping LLM analysis.")
        return None

    try:
        from anthropic import Anthropic
    except ImportError:
        log.error("anthropic package not installed. Run: pip install anthropic")
        return None

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = None
    for attempt in range(1, API_MAX_RETRIES + 1):
        try:
            resp = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=500,
                system=_LLM_SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": _build_llm_user_prompt(ticker, headlines, side)},
                ],
            )
            break
        except Exception as e:
            log.error("Anthropic API attempt %d/%d failed for %s: %s",
                      attempt, API_MAX_RETRIES, ticker, e)
            if attempt < API_MAX_RETRIES:
                time.sleep(API_RETRY_DELAY * attempt)
            else:
                log.error("Anthropic API exhausted all %d retries for %s.", API_MAX_RETRIES, ticker)
                return None

    # Concatenate any text blocks in the response.
    try:
        parts = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        raw = "".join(parts).strip()
    except Exception as e:
        log.error("Could not extract text from Anthropic response for %s: %s", ticker, e)
        return None

    if not raw:
        log.error("Empty LLM response for %s", ticker)
        return None

    parsed = _parse_llm_json(raw)
    if parsed is None:
        log.error("Could not parse LLM JSON for %s. Raw: %s", ticker, raw[:300])
    return parsed


# ---------------------------------------------------------------------------
# NOTIFICATIONS (Green API)
# ---------------------------------------------------------------------------

def send_whatsapp(message: str) -> bool:
    """Post to Green API sendMessage. Never raises — returns True on success."""
    if not (GREEN_API_ID_INSTANCE and GREEN_API_TOKEN_INSTANCE and WHATSAPP_PHONE_NUMBER):
        log.error("Green API credentials missing. Check your .env file.")
        return False

    url = (
        f"https://api.green-api.com/waInstance{GREEN_API_ID_INSTANCE}"
        f"/sendMessage/{GREEN_API_TOKEN_INSTANCE}"
    )
    payload = {
        "chatId": f"{WHATSAPP_PHONE_NUMBER}@c.us",
        "message": message,
    }

    for attempt in range(1, API_MAX_RETRIES + 1):
        try:
            r = requests.post(url, json=payload, timeout=20)
        except requests.exceptions.Timeout:
            log.error("WhatsApp send timed out (attempt %d/%d)", attempt, API_MAX_RETRIES)
            if attempt < API_MAX_RETRIES:
                time.sleep(API_RETRY_DELAY * attempt)
                continue
            return False
        except requests.exceptions.ConnectionError as e:
            log.error("WhatsApp connection error (attempt %d/%d): %s", attempt, API_MAX_RETRIES, e)
            if attempt < API_MAX_RETRIES:
                time.sleep(API_RETRY_DELAY * attempt)
                continue
            return False
        except Exception as e:
            log.error("WhatsApp unexpected exception: %s", e)
            return False

        try:
            if r.status_code == 200:
                mid = r.json().get("idMessage", "ok")
                log.info("WhatsApp sent (idMessage=%s)", mid)
                return True
            log.error("WhatsApp send failed: HTTP %s — %s (attempt %d/%d)",
                      r.status_code, r.text[:300], attempt, API_MAX_RETRIES)
            if r.status_code >= 500 and attempt < API_MAX_RETRIES:
                time.sleep(API_RETRY_DELAY * attempt)
                continue
            return False
        except Exception as e:
            log.error("WhatsApp response parse error: %s", e)
            return False

    return False


def format_hybrid_alert(
    name: str,
    ticker: str,
    tech: dict,
    llm: Optional[dict],
    side: str,
    backtest: Optional[dict] = None,
) -> str:
    """Format the WhatsApp BUY/SELL alert. Headlines are NEVER included —
    only the AI's distilled analysis (if available)."""
    if side == "BUY":
        header_emoji = "🟢"
        triggered = tech["buy_triggered"]
        score = tech["buy_score"]
    else:
        header_emoji = "🔴"
        triggered = tech["sell_triggered"]
        score = tech["sell_score"]

    triggered_lines = "\n".join(
        f"  • {ind['name']}: {ind['detail']}" for ind in triggered
    )

    if llm is not None:
        ai_sentiment = llm.get("sentiment", "N/A")
        ai_analysis = llm.get("analysis", "")
        if ai_sentiment != side:
            ai_section = (
                f"🤖 *AI Analysis* (sentiment: {ai_sentiment})\n"
                f"⚠️ AI disagrees with technical signal.\n"
                f"{ai_analysis}"
            )
        else:
            ai_section = f"🤖 *AI Analysis* (sentiment: {ai_sentiment})\n{ai_analysis}"
    else:
        ai_section = "🤖 *AI Analysis*\nNo news available for this security."

    if backtest is not None:
        strat = backtest["strategy_return"]
        hold  = backtest["hold_return"]
        strat_str = f"+{strat}%" if strat >= 0 else f"{strat}%"
        hold_str  = f"+{hold}%"  if hold  >= 0 else f"{hold}%"
        verdict = "✅ Strategy beat buy & hold" if strat >= hold else "⚠️ Buy & hold outperformed"
        backtest_section = (
            f"\n"
            f"📈 *2-Year Backtest (this signal strategy)*\n"
            f"  • Strategy: {strat_str} ({backtest['num_trades']} trades, {backtest['win_rate']}% win rate)\n"
            f"  • Buy & Hold: {hold_str}\n"
            f"  • {verdict}"
        )
    else:
        backtest_section = ""

    return (
        f"{header_emoji} *{side} SIGNAL* — {name} ({ticker})\n"
        f"📅 Date: {tech['date']}\n"
        f"💵 Close: {tech['close']:.2f}\n"
        f"\n"
        f"📊 *Technical Score: {score}/4*\n"
        f"Triggered indicators:\n{triggered_lines}\n"
        f"\n"
        f"{ai_section}"
        f"{backtest_section}"
    )


# ---------------------------------------------------------------------------
# SCAN (hybrid workflow)
# ---------------------------------------------------------------------------

def run_once(force: bool = False, notify: bool = True, manual: bool = False) -> None:
    """Run a single hybrid scan. Skips if the market is closed unless force=True.
    Pass notify=False to run analysis and populate dashboard data without sending
    any WhatsApp alerts (used by the startup scan to avoid re-alerting after
    a Render service restart when the cooldown state file may have been lost).
    Pass manual=True when the scan was explicitly triggered by the user (e.g. the
    "Force Scan" button) — this is recorded as "forced" in the scan history so
    restart-triggered background scans aren't mis-labelled as user-initiated.
    """
    # Prevent concurrent scans (e.g. scheduler + startup job firing simultaneously)
    if not _scan_lock.acquire(blocking=False):
        log.info("Scan already in progress — skipping duplicate run.")
        return

    try:
        _run_once_inner(force=force, notify=notify, manual=manual)
    finally:
        _scan_lock.release()


def _run_once_inner(force: bool = False, notify: bool = True, manual: bool = False) -> None:
    """Internal scan implementation (called only while _scan_lock is held)."""
    # Guard: skip if a scan already ran recently (protects against rapid restarts
    # re-sending alerts before the 48-hour cooldown state is written to disk).
    history = load_scan_history()
    if history:
        try:
            last_ts = datetime.fromisoformat(history[0]["timestamp"])
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            age_minutes = (datetime.now(tz=timezone.utc) - last_ts).total_seconds() / 60
            if age_minutes < MIN_SCAN_INTERVAL_MINUTES:
                log.info(
                    "Last scan was %.1f min ago (< %d min threshold) — skipping.",
                    age_minutes, MIN_SCAN_INTERVAL_MINUTES,
                )
                return
        except Exception:
            pass  # If we can't parse the history, proceed normally

    open_now, reason = is_us_market_open()
    log.info("===== SCAN start — market %s =====", reason)
    if not open_now and not force:
        log.info("Market closed — no checks performed. (%s)", reason)
        return

    # Resolution order for tickers to scan:
    #   1. TICKERS env var (comma-separated Yahoo symbols) — cloud override.
    #   2. Excel portfolio (if EXCEL_FILE is set and the file exists locally).
    #   3. Fallback to TICKER_MAP values from config.yaml — keeps the cloud scan
    #      running when the Excel path points at a local machine the server can't see.
    _tickers_env = os.getenv("TICKERS", "").strip()
    tickers: list[tuple[str, str]] = []
    resolution_note: str = ""

    if _tickers_env:
        tickers = [(t.strip(), t.strip()) for t in _tickers_env.split(",") if t.strip()]
        resolution_note = f"TICKERS env var ({len(tickers)} symbols)"
        log.info("Using TICKERS env var: %s", ", ".join(t for _, t in tickers))
    elif EXCEL_FILE and Path(EXCEL_FILE).exists():
        try:
            names = load_portfolio_names(EXCEL_FILE)
            tickers = resolve_tickers(names)
            resolution_note = f"Excel ({EXCEL_FILE})"
        except Exception as e:
            log.error("Loading Excel failed: %s", e)
            log.debug(traceback.format_exc())
            # Fall through to TICKER_MAP fallback below rather than returning.

    if not tickers:
        # Fallback to TICKER_MAP values — the user's curated portfolio symbols from config.yaml.
        mapped = sorted({v for v in TICKER_MAP.values() if v})
        if mapped:
            tickers = [(sym, sym) for sym in mapped]
            resolution_note = f"TICKER_MAP fallback ({len(tickers)} symbols from config.yaml)"
            log.info(
                "Excel not available — falling back to TICKER_MAP values: %s",
                ", ".join(mapped),
            )

    if not tickers:
        log.error("No tickers configured. Set TICKERS env var, EXCEL_FILE, or add to config.yaml ticker_map.")
        # Persist an empty scan record so the dashboard can show the reason.
        save_scan_record({
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "market_status": reason,
            "forced": manual,
            "tickers_count": 0,
            "results": [],
            "alerts_sent": [],
            "errors": [{"ticker": "-", "error": "No tickers configured (TICKERS / EXCEL_FILE / ticker_map all empty)"}],
        })
        return

    log.info("Monitoring %d symbols (source: %s).", len(tickers), resolution_note)

    state = load_state()

    scan_record: dict = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "market_status": reason,
        "forced": manual,
        "tickers_count": len(tickers),
        "results": [],
        "alerts_sent": [],
        "errors": [],
    }

    for name, ticker in tickers:
        try:
            # ---------- Step 1: Technical filter (3/4 rule, both sides) ----------
            df = fetch_history(ticker)
            if df is None:
                scan_record["errors"].append({"ticker": ticker, "error": "No data from yfinance"})
                continue

            df = compute_indicators(df)
            tech = evaluate_technical_signals(df)

            log.info(
                "%-10s close=%-9.2f rsi=%-6.2f Δsma200=%+6.2f%%  buy=%d/4 sell=%d/4",
                ticker, tech["close"], tech["rsi"],
                (tech["close"] - tech["sma200"]) / tech["sma200"] * 100,
                tech["buy_score"], tech["sell_score"],
            )

            ticker_result = {
                "name": name,
                "ticker": ticker,
                "close": tech["close"],
                "rsi": round(tech["rsi"], 2),
                "sma200_delta_pct": round((tech["close"] - tech["sma200"]) / tech["sma200"] * 100, 2),
                "buy_score": tech["buy_score"],
                "sell_score": tech["sell_score"],
                "buy_passes": tech["buy_passes"],
                "sell_passes": tech["sell_passes"],
            }
            scan_record["results"].append(ticker_result)

            # Evaluate each side independently.
            for side in ("BUY", "SELL"):
                passes = tech["buy_passes"] if side == "BUY" else tech["sell_passes"]
                if not passes:
                    continue

                alert_info = _process_signal_side(
                    name=name,
                    ticker=ticker,
                    side=side,
                    tech=tech,
                    df=df,
                    state=state,
                    notify=notify,
                )
                if alert_info:
                    scan_record["alerts_sent"].append(alert_info)

        except Exception as e:
            log.error("Error processing %s: %s", ticker, e)
            log.debug(traceback.format_exc())
            scan_record["errors"].append({"ticker": ticker, "error": str(e)})

    save_scan_record(scan_record)
    log.info("===== SCAN end =====")


def _process_signal_side(
    name: str,
    ticker: str,
    side: str,
    tech: dict,
    df: pd.DataFrame,
    state: dict,
    notify: bool = True,
) -> Optional[dict]:
    """Run the news → LLM → WhatsApp pipeline for one side (BUY or SELL).
    WhatsApp is sent whenever the technical score passes, regardless
    of what the AI says. Pass notify=False to skip WhatsApp (analysis still runs).
    Returns alert info dict if sent, None otherwise."""
    triggered = tech["buy_triggered"] if side == "BUY" else tech["sell_triggered"]
    score = tech["buy_score"] if side == "BUY" else tech["sell_score"]

    indicator_names = ", ".join(ind["name"] for ind in triggered)
    log.info(
        "[%s] PASSED %s technical filter (%d/4: %s) — fetching news…",
        ticker, side, score, indicator_names,
    )

    # ---------- Step 2: News fetching (optional — alert is sent regardless) ----------
    headlines = fetch_news(ticker, limit=NEWS_HEADLINE_LIMIT)
    llm: Optional[dict] = None

    if not headlines:
        log.info("[%s] No news available — will send alert without AI analysis (%s).", ticker, side)
    else:
        log.info("[%s] %d headline(s) fetched — calling Anthropic for %s…",
                 ticker, len(headlines), side)

        # ---------- Step 3: LLM fundamental analysis ----------
        llm = analyze_with_llm(ticker, headlines, side=side)
        if llm is None:
            log.warning("[%s] LLM analysis failed — sending alert without AI analysis.", ticker)
        else:
            log.info("[%s] LLM sentiment=%s (technical side=%s)",
                     ticker, llm["sentiment"], side)

    # ---------- Step 4: WhatsApp alert (always when technical filter passes) ----------
    if not notify:
        log.info("[%s] notify=False — skipping WhatsApp alert (dashboard-only scan).", ticker)
        return None

    if is_in_cooldown(state, ticker, side):
        log.info(
            "Cooldown active for %s %s — suppressing alert (%dh window).",
            ticker, side, COOLDOWN_HOURS,
        )
        return None

    backtest = run_backtest(df)
    message = format_hybrid_alert(name, ticker, tech, llm, side=side, backtest=backtest)
    log.info("ALERT %s %s\n%s", side, ticker, message)

    sent = send_whatsapp(message)
    if sent:
        mark_alerted(state, ticker, side)
    else:
        log.warning(
            "Alert NOT marked as sent (WhatsApp failed) — will retry next scan."
        )

    return {
        "ticker": ticker,
        "name": name,
        "side": side,
        "score": score,
        "indicators": indicator_names,
        "ai_sentiment": llm["sentiment"] if llm else None,
        "whatsapp_sent": sent,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Portfolio Hybrid (Technical + AI) Monitor")
    parser.add_argument("--test", action="store_true",
                        help="Send a test WhatsApp message and exit.")
    parser.add_argument("--once", action="store_true",
                        help="Run a single scan and exit (for Task Scheduler).")
    parser.add_argument("--force", action="store_true",
                        help="Ignore the market-hours gate and scan anyway.")
    args = parser.parse_args()

    if args.test:
        msg = (
            "✅ Portfolio Hybrid Monitor — test message\n"
            f"Sent at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            "If you see this, Green API is configured correctly."
        )
        ok = send_whatsapp(msg)
        raise SystemExit(0 if ok else 1)

    log.info(
        "Portfolio Hybrid Monitor starting (once=%s, force=%s, model=%s)",
        args.once, args.force, ANTHROPIC_MODEL,
    )

    if args.once:
        try:
            run_once(force=args.force)
        except Exception as e:
            log.error("Fatal error in run_once: %s", e)
            log.debug(traceback.format_exc())
        return

    # Continuous mode (only used if NOT running under Task Scheduler).
    while True:
        try:
            run_once(force=args.force)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            return
        except Exception as e:
            log.error("Unhandled exception in scan loop: %s", e)
            log.debug(traceback.format_exc())

        next_run = datetime.now() + timedelta(seconds=CHECK_INTERVAL_SECONDS)
        log.info(
            "Sleeping %d min… next run at %s",
            CHECK_INTERVAL_SECONDS // 60,
            next_run.strftime("%Y-%m-%d %H:%M:%S"),
        )
        try:
            time.sleep(CHECK_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            return


if __name__ == "__main__":
    main()
