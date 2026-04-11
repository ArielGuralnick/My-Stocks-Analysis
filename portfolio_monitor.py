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
import logging
import os
import re
import sys
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

# Green API (WhatsApp)
GREEN_API_ID_INSTANCE = os.getenv("GREEN_API_ID_INSTANCE")
GREEN_API_TOKEN_INSTANCE = os.getenv("GREEN_API_TOKEN_INSTANCE")
WHATSAPP_PHONE_NUMBER = os.getenv("WHATSAPP_PHONE_NUMBER")  # e.g. 972501234567

# Anthropic API (LLM fundamental analysis)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

EXCEL_FILE = r"C:\Users\ArielGuralnick\Downloads\Excellence_040426.xlsx"
TICKER_COLUMN = "שם נייר"             # Column in the Excel file holding the security name
HEADER_ROW = 9                         # 0-indexed row where headers are located
CHECK_INTERVAL_SECONDS = 2 * 60 * 60   # 2 hours (continuous mode)

STATE_FILE = BASE_DIR / "signals_state.json"
LOG_FILE = BASE_DIR / "trading_bot.log"

COOLDOWN_HOURS = 48                    # Per-ticker cooldown
MARKET_TZ = ZoneInfo("America/New_York")

NEWS_HEADLINE_LIMIT = 5
TECHNICAL_SCORE_THRESHOLD = 2          # ≥ 2 of 4 indicators must fire

# Portfolio names (as they appear in the Excel) → Yahoo Finance tickers.
TICKER_MAP: dict[str, str] = {
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
    # Israeli securities — add matching .TA tickers here if desired.
}

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
        log.warning("No Yahoo ticker mapping for %d names (add to TICKER_MAP):", len(skipped))
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

    A side passes the filter when at least TECHNICAL_SCORE_THRESHOLD (=2) of
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

    try:
        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=500,
            system=_LLM_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": _build_llm_user_prompt(ticker, headlines, side)},
            ],
        )
    except Exception as e:
        log.error("Anthropic API call failed for %s: %s", ticker, e)
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

    try:
        r = requests.post(url, json=payload, timeout=20)
    except requests.exceptions.Timeout:
        log.error("WhatsApp send timed out after 20s")
        return False
    except requests.exceptions.ConnectionError as e:
        log.error("WhatsApp connection error: %s", e)
        return False
    except Exception as e:
        log.error("WhatsApp unexpected exception: %s", e)
        return False

    try:
        if r.status_code == 200:
            mid = r.json().get("idMessage", "ok")
            log.info("WhatsApp sent (idMessage=%s)", mid)
            return True
        log.error("WhatsApp send failed: HTTP %s — %s", r.status_code, r.text[:300])
        return False
    except Exception as e:
        log.error("WhatsApp response parse error: %s", e)
        return False


def format_hybrid_alert(
    name: str,
    ticker: str,
    tech: dict,
    llm: dict,
    side: str,
) -> str:
    """Format the WhatsApp BUY/SELL alert. Headlines are NEVER included —
    only the AI's distilled analysis."""
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
    return (
        f"{header_emoji} *{side} SIGNAL* — {name} ({ticker})\n"
        f"📅 Date: {tech['date']}\n"
        f"💵 Close: ${tech['close']:.2f}\n"
        f"\n"
        f"📊 *Technical Score: {score}/4*\n"
        f"Triggered indicators:\n{triggered_lines}\n"
        f"\n"
        f"🤖 *AI Fundamental Analysis*\n"
        f"{llm['analysis']}"
    )


# ---------------------------------------------------------------------------
# SCAN (hybrid workflow)
# ---------------------------------------------------------------------------

def run_once(force: bool = False) -> None:
    """Run a single hybrid scan. Skips if the market is closed unless force=True."""
    open_now, reason = is_us_market_open()
    log.info("===== SCAN start — market %s =====", reason)
    if not open_now and not force:
        log.info("Market closed — no checks performed. (%s)", reason)
        return

    try:
        names = load_portfolio_names(EXCEL_FILE)
    except Exception as e:
        log.error("Loading Excel failed: %s", e)
        log.debug(traceback.format_exc())
        return

    tickers = resolve_tickers(names)
    log.info("Monitoring %d symbols.", len(tickers))

    state = load_state()

    for name, ticker in tickers:
        try:
            # ---------- Step 1: Technical filter (2/4 rule, both sides) ----------
            df = fetch_history(ticker)
            if df is None:
                continue

            df = compute_indicators(df)
            tech = evaluate_technical_signals(df)

            log.info(
                "%-10s close=%-9.2f rsi=%-6.2f Δsma200=%+6.2f%%  buy=%d/4 sell=%d/4",
                ticker, tech["close"], tech["rsi"],
                (tech["close"] - tech["sma200"]) / tech["sma200"] * 100,
                tech["buy_score"], tech["sell_score"],
            )

            # Evaluate each side independently. (Theoretically both could fire
            # in the same scan — e.g. mixed signals — so we check each.)
            for side in ("BUY", "SELL"):
                passes = tech["buy_passes"] if side == "BUY" else tech["sell_passes"]
                if not passes:
                    continue

                _process_signal_side(
                    name=name,
                    ticker=ticker,
                    side=side,
                    tech=tech,
                    state=state,
                )

        except Exception as e:
            log.error("Error processing %s: %s", ticker, e)
            log.debug(traceback.format_exc())

    log.info("===== SCAN end =====")


def _process_signal_side(
    name: str,
    ticker: str,
    side: str,
    tech: dict,
    state: dict,
) -> None:
    """Run the news → LLM → WhatsApp pipeline for one side (BUY or SELL)."""
    triggered = tech["buy_triggered"] if side == "BUY" else tech["sell_triggered"]
    score = tech["buy_score"] if side == "BUY" else tech["sell_score"]

    indicator_names = ", ".join(ind["name"] for ind in triggered)
    log.info(
        "[%s] PASSED %s technical filter (%d/4: %s) — fetching news…",
        ticker, side, score, indicator_names,
    )

    # ---------- Step 2: News fetching ----------
    headlines = fetch_news(ticker, limit=NEWS_HEADLINE_LIMIT)
    if not headlines:
        log.info("[%s] No news available — skipping LLM analysis (%s).", ticker, side)
        return

    log.info("[%s] %d headline(s) fetched — calling Anthropic for %s…",
             ticker, len(headlines), side)

    # ---------- Step 3: LLM fundamental analysis ----------
    llm = analyze_with_llm(ticker, headlines, side=side)
    if llm is None:
        log.warning("[%s] LLM analysis failed — no %s alert.", ticker, side)
        return

    log.info("[%s] LLM sentiment=%s (technical side=%s)",
             ticker, llm["sentiment"], side)

    # ---------- Step 4: WhatsApp alert (only when LLM agrees with side) ----------
    if llm["sentiment"] != side:
        log.info(
            "[%s] LLM did not confirm %s (sentiment=%s) — no alert.",
            ticker, side, llm["sentiment"],
        )
        return

    if is_in_cooldown(state, ticker, side):
        log.info(
            "Cooldown active for %s %s — suppressing alert (%dh window).",
            ticker, side, COOLDOWN_HOURS,
        )
        return

    message = format_hybrid_alert(name, ticker, tech, llm, side=side)
    log.info("ALERT %s %s\n%s", side, ticker, message)

    if send_whatsapp(message):
        mark_alerted(state, ticker, side)
    else:
        log.warning(
            "Alert NOT marked as sent (WhatsApp failed) — will retry next scan."
        )


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
