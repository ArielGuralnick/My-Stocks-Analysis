"""
Portfolio Monitor — Web Dashboard
-----------------------------------
A lightweight Flask app that shows scan history, ticker evaluations,
alert history, and cooldown status from portfolio_monitor.py.

Run:  python dashboard.py
Open: http://localhost:5050
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

# All displayed times (dashboard, logs synthesized from scan history, cooldown
# expiry labels, "last refreshed" stamp) are rendered in Jerusalem local time.
# Storage remains UTC ISO — only the presentation layer converts.
JERUSALEM_TZ = ZoneInfo("Asia/Jerusalem")


def _fmt_jerusalem(iso_str: str, with_tz: bool = True) -> str:
    """Convert a stored UTC ISO timestamp to 'YYYY-MM-DD HH:MM IDT/IST'.
    Falls back to a raw slice if parsing fails."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(JERUSALEM_TZ)
        if with_tz:
            tz_abbr = local.strftime("%Z") or "Jerusalem"
            return local.strftime(f"%Y-%m-%d %H:%M {tz_abbr}")
        return local.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str[:16].replace("T", " ")


def _now_jerusalem_str() -> str:
    now = datetime.now(tz=JERUSALEM_TZ)
    tz_abbr = now.strftime("%Z") or "Jerusalem"
    return now.strftime(f"%Y-%m-%d %H:%M {tz_abbr}")

import pandas as pd
import yaml
from flask import Flask, jsonify, render_template_string, request
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
# On Render the persistent disk is mounted at /app/data; fall back to project root locally.
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SCAN_HISTORY_FILE = DATA_DIR / "scan_history.json"
STATE_FILE = DATA_DIR / "signals_state.json"
LOG_FILE = DATA_DIR / "trading_bot.log"
COOLDOWN_HOURS = 48

CONFIG_FILE = BASE_DIR / "config.yaml"
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
ALLOWED_EXTENSIONS = {".xlsx", ".xls"}

# GitHub persistence — keeps scan_history.json alive across Render free-tier restarts.
# Set GITHUB_TOKEN (classic token with repo scope) in Render's Environment tab.
_GH_TOKEN  = os.getenv("GITHUB_TOKEN", "")
_GH_REPO   = os.getenv("GITHUB_REPO", "ArielGuralnick/My-Stocks-Analysis")
_GH_BRANCH = os.getenv("GITHUB_BRANCH", "main")
_GH_PATH   = "data/scan_history.json"
_GH_API    = f"https://api.github.com/repos/{_GH_REPO}/contents/{_GH_PATH}"
_log       = logging.getLogger("dashboard")

# In-memory OHLCV cache keyed by (ticker, period); entries expire after 5 minutes
_ohlcv_cache: dict = {}
_OHLCV_TTL = 300  # seconds


def _github_pull() -> bool:
    """Download scan_history.json from GitHub into the local DATA_DIR.
    Returns True if data was loaded, False otherwise."""
    if not _GH_TOKEN:
        return False
    try:
        import requests as _req
        headers = {"Authorization": f"token {_GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        r = _req.get(_GH_API, headers=headers, params={"ref": _GH_BRANCH}, timeout=10)
        if r.status_code == 404:
            _log.info("GitHub: no scan_history.json yet — starting fresh.")
            return False
        r.raise_for_status()
        content = base64.b64decode(r.json()["content"]).decode("utf-8")
        parsed = json.loads(content)
        SCAN_HISTORY_FILE.write_text(content, encoding="utf-8")
        _log.info("GitHub pull: loaded %d scans.", len(parsed))
        return True
    except Exception as exc:
        _log.warning("GitHub pull failed: %s", exc)
        return False


def _github_push() -> None:
    """Upload the current scan_history.json to GitHub."""
    if not _GH_TOKEN or not SCAN_HISTORY_FILE.exists():
        return
    try:
        import requests as _req
        content_bytes = SCAN_HISTORY_FILE.read_bytes()
        encoded = base64.b64encode(content_bytes).decode("utf-8")
        headers = {"Authorization": f"token {_GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        # Fetch current SHA so GitHub accepts the update
        r = _req.get(_GH_API, headers=headers, params={"ref": _GH_BRANCH}, timeout=10)
        sha = r.json().get("sha") if r.status_code == 200 else None
        payload: dict = {
            "message": f"chore: scan history {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "content": encoded,
            "branch": _GH_BRANCH,
        }
        if sha:
            payload["sha"] = sha
        r = _req.put(_GH_API, headers=headers, json=payload, timeout=15)
        if r.status_code in (200, 201):
            _log.info("GitHub push: scan history saved.")
        else:
            _log.warning("GitHub push failed: %s %s", r.status_code, r.text[:200])
    except Exception as exc:
        _log.warning("GitHub push failed: %s", exc)


app = Flask(__name__)


@app.after_request
def _add_cors(response):
    """Add CORS headers to every /api/* response so Netlify can call Render."""
    if request.path.startswith("/api/"):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


def _allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def _load_config() -> dict:
    """Return the current config.yaml as a dict (empty dict on any error)."""
    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}


def _save_config(cfg: dict) -> None:
    """Persist cfg back to config.yaml."""
    with CONFIG_FILE.open("w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _update_config_excel_path(file_path: str) -> None:
    """Write the uploaded file path into config.yaml's excel.file field."""
    cfg = _load_config()
    if "excel" not in cfg or not isinstance(cfg.get("excel"), dict):
        cfg["excel"] = {}
    cfg["excel"]["file"] = file_path
    _save_config(cfg)


def _normalize(s: str) -> str:
    """Collapse all whitespace so column names match regardless of spacing."""
    return re.sub(r"\s+", " ", str(s)).strip()


def _extract_excel_names(file_path: str, ticker_column: str, header_row: int) -> list[str]:
    """Read the Excel and return the list of security names from ticker_column."""
    df = pd.read_excel(file_path, header=header_row)
    df.columns = [_normalize(c) for c in df.columns]
    target = _normalize(ticker_column)
    if target not in df.columns:
        raise ValueError(
            f"Column '{ticker_column}' not found in Excel. "
            f"Available columns: {list(df.columns)}"
        )
    return [str(n).strip() for n in df[target].dropna().tolist()]


def _find_unmapped(names: list[str], ticker_map: dict) -> list[str]:
    """Return names that have no entry in ticker_map."""
    return [n for n in names if n not in ticker_map]


def _load_json(path: Path, default=None):
    if not path.exists():
        return default if default is not None else []
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else []


def _get_cooldowns() -> dict:
    """Compute active cooldowns.

    Derived from scan_history.json rather than signals_state.json: the scan
    history is the persisted source of truth (pushed to GitHub and pulled
    back on startup), while signals_state.json lives on Render's ephemeral
    disk and is wiped on every restart. We walk the retained scans and, for
    each (ticker, side) pair, keep the most recent alert timestamp where the
    WhatsApp send actually succeeded. If that timestamp + COOLDOWN_HOURS is
    still in the future, the cooldown is active.
    """
    now = datetime.now(tz=timezone.utc)
    scans = _load_json(SCAN_HISTORY_FILE, [])
    if not isinstance(scans, list):
        scans = []

    # Map (ticker, side) -> most recent successful alert datetime
    latest: dict[tuple[str, str], datetime] = {}
    for scan in scans:
        for alert in scan.get("alerts_sent", []) or []:
            if not alert.get("whatsapp_sent"):
                continue
            ticker = alert.get("ticker")
            side = alert.get("side")
            ts_str = alert.get("timestamp")
            if not (ticker and side and ts_str):
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            key = (ticker, side)
            if key not in latest or ts > latest[key]:
                latest[key] = ts

    # Merge in legacy signals_state.json entries (useful for local dev where
    # the state file is the only source).
    state = _load_json(STATE_FILE, {})
    if isinstance(state, dict):
        for ticker, sides in state.items():
            if not isinstance(sides, dict):
                continue
            for side, iso_str in sides.items():
                try:
                    ts = datetime.fromisoformat(iso_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                key = (ticker, side)
                if key not in latest or ts > latest[key]:
                    latest[key] = ts

    result: dict = {}
    for (ticker, side), last in latest.items():
        expires = last + timedelta(hours=COOLDOWN_HOURS)
        remaining = expires - now
        if remaining.total_seconds() <= 0:
            continue
        hours_left = remaining.total_seconds() / 3600
        last_local = last.astimezone(JERUSALEM_TZ)
        expires_local = expires.astimezone(JERUSALEM_TZ)
        tz_abbr_last = last_local.strftime("%Z") or "Jerusalem"
        tz_abbr_exp = expires_local.strftime("%Z") or "Jerusalem"
        result.setdefault(ticker, {})[side] = {
            "alerted_at": last_local.strftime(f"%Y-%m-%d %H:%M {tz_abbr_last}"),
            "expires": expires_local.strftime(f"%Y-%m-%d %H:%M {tz_abbr_exp}"),
            "hours_left": round(hours_left, 1),
        }
    return result


def _synthesize_logs_from_history(n: int) -> list[str]:
    """Build recent-log lines from scan_history.json.

    Used when the real trading_bot.log file is missing or empty (e.g. on
    Render's ephemeral FS after a restart — the scan history is pulled back
    from GitHub but the log file is gone). The scan record already contains
    timestamps, market status, alerts, and errors, so we can reconstruct a
    useful activity view without the original log file.
    """
    scans = _load_json(SCAN_HISTORY_FILE, [])
    if not isinstance(scans, list) or not scans:
        return []

    lines: list[str] = []
    # scan_history is newest-first; walk oldest-first so logs read chronologically
    for scan in reversed(scans):
        ts_str = scan.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts_fmt = ts.astimezone(JERUSALEM_TZ).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            ts_fmt = ts_str[:19].replace("T", " ") if ts_str else "????-??-?? ??:??:??"

        market = scan.get("market_status", "?")
        tickers_count = scan.get("tickers_count", 0)
        forced_tag = " [MANUAL]" if scan.get("forced") else ""
        lines.append(
            f"{ts_fmt} | INFO    | ===== SCAN start — market {market} "
            f"({tickers_count} tickers){forced_tag} =====\n"
        )

        for alert in scan.get("alerts_sent", []) or []:
            ticker = alert.get("ticker", "?")
            side = alert.get("side", "?")
            score = alert.get("score", "?")
            indicators = alert.get("indicators", "")
            ai = alert.get("ai_sentiment") or "n/a"
            sent = "OK" if alert.get("whatsapp_sent") else "FAIL"
            lines.append(
                f"{ts_fmt} | INFO    | ALERT {side} {ticker} "
                f"(score {score}/4: {indicators}) AI={ai} WhatsApp={sent}\n"
            )

        for err in scan.get("errors", []) or []:
            ticker = err.get("ticker", "?")
            msg = err.get("error", "")
            lines.append(
                f"{ts_fmt} | ERROR   | {ticker}: {msg}\n"
            )

        lines.append(f"{ts_fmt} | INFO    | ===== SCAN end =====\n")

    return lines[-n:]


def _get_recent_logs(n: int = 80) -> list[str]:
    """Return the last `n` log lines.

    Prefers the real trading_bot.log file when it exists and has content.
    Falls back to synthesizing lines from scan_history.json (which is pushed
    to GitHub and survives Render restarts, unlike the log file itself).
    """
    if LOG_FILE.exists():
        try:
            with LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if lines:
                return lines[-n:]
        except Exception:
            pass
    return _synthesize_logs_from_history(n)


def _attach_display_times(scans: list) -> None:
    """Pre-compute a Jerusalem-local 'display_time' string on each scan and
    its alerts, so the Jinja template doesn't have to do any timezone math."""
    for scan in scans:
        scan["display_time"] = _fmt_jerusalem(scan.get("timestamp", ""), with_tz=False)
        for alert in scan.get("alerts_sent", []) or []:
            alert["display_time"] = _fmt_jerusalem(alert.get("timestamp", ""), with_tz=False)


def _get_chart_tickers() -> list[str]:
    """Resolve the set of tickers available for charting.

    Priority: TICKERS env var > config.yaml ticker_map values > last scan results.
    Returns a sorted, deduplicated list of uppercase ticker symbols.
    """
    tickers: set[str] = set()

    env_tickers = os.getenv("TICKERS", "")
    if env_tickers:
        for t in env_tickers.split(","):
            t = t.strip().upper()
            if t:
                tickers.add(t)

    cfg = _load_config()
    for t in (cfg.get("ticker_map") or {}).values():
        if isinstance(t, str) and t.strip():
            tickers.add(t.strip().upper())

    scans = _load_json(SCAN_HISTORY_FILE, [])
    if scans and isinstance(scans, list):
        for result in (scans[0].get("results") or []):
            t = result.get("ticker", "")
            if t:
                tickers.add(t.strip().upper())

    return sorted(tickers)


@app.route("/")
def index():
    scans = _load_json(SCAN_HISTORY_FILE, [])
    _attach_display_times(scans)
    cooldowns = _get_cooldowns()
    logs = _get_recent_logs(80)

    # Gather all alerts across scans
    all_alerts = []
    for scan in scans:
        for alert in scan.get("alerts_sent", []):
            all_alerts.append(alert)

    return render_template_string(
        DASHBOARD_HTML,
        scans=scans,
        cooldowns=cooldowns,
        all_alerts=all_alerts[:30],
        logs=logs,
        now=_now_jerusalem_str(),
        chart_tickers=_get_chart_tickers(),
    )


@app.route("/api/quotes")
def api_quotes():
    """Return live quotes for a comma-separated list of Yahoo Finance symbols.

    Usage:  /api/quotes?symbols=NVDA,AAPL,MSFT
    Response: { "quotes": [ {"symbol": "NVDA", "price": 874.21, "change_pct": 2.14}, ... ] }
    """
    symbols_raw = request.args.get("symbols", "")
    symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
    if not symbols:
        return jsonify({"ok": False, "error": "No symbols provided"}), 400

    try:
        import yfinance as yf
    except ImportError:
        return jsonify({"ok": False, "error": "yfinance not installed"}), 500

    quotes = []
    for sym in symbols:
        try:
            hist = yf.Ticker(sym).history(period="2d", auto_adjust=False)
            if hist is None or hist.empty or len(hist) < 1:
                quotes.append({"symbol": sym, "error": "no data"})
                continue
            last_close = float(hist["Close"].iloc[-1])
            if len(hist) >= 2:
                prev_close = float(hist["Close"].iloc[-2])
                change_pct = ((last_close - prev_close) / prev_close) * 100.0 if prev_close else 0.0
            else:
                change_pct = 0.0
            quotes.append({
                "symbol": sym,
                "price": round(last_close, 2),
                "change_pct": round(change_pct, 2),
            })
        except Exception as exc:
            quotes.append({"symbol": sym, "error": str(exc)[:120]})

    response = jsonify({
        "ok": True,
        "quotes": quotes,
        "fetched_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    })
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.route("/api/ohlcv")
def api_ohlcv():
    """Return OHLCV + technical indicator data for charting.

    Usage:  /api/ohlcv?ticker=NVDA&period=6mo
    Accepted periods: 1mo, 3mo, 6mo, 1y, 2y  (default: 6mo)
    """
    import time as _time

    VALID_PERIODS = {"1mo", "3mo", "6mo", "1y", "2y"}
    ticker = request.args.get("ticker", "").strip().upper()
    period = request.args.get("period", "6mo").strip().lower()

    if not ticker:
        return jsonify({"ok": False, "error": "No ticker provided"}), 400
    if period not in VALID_PERIODS:
        return jsonify({"ok": False, "error": f"Invalid period. Use: {', '.join(sorted(VALID_PERIODS))}"}), 400

    allowed = set(_get_chart_tickers())
    if allowed and ticker not in allowed:
        return jsonify({"ok": False, "error": f"Ticker {ticker!r} not in portfolio"}), 400

    cache_key = (ticker, period)
    now_ts = _time.time()
    if cache_key in _ohlcv_cache:
        entry = _ohlcv_cache[cache_key]
        if now_ts - entry["ts"] < _OHLCV_TTL:
            return jsonify(entry["data"])

    try:
        import yfinance as yf
        from portfolio_monitor import compute_indicators
    except ImportError as exc:
        return jsonify({"ok": False, "error": f"Dependency missing: {exc}"}), 500

    try:
        df = yf.download(ticker, period=period, interval="1d", auto_adjust=True, progress=False)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Download failed: {str(exc)[:200]}"}), 502

    if df is None or df.empty:
        return jsonify({"ok": False, "error": f"No data for {ticker} ({period})"}), 404

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = compute_indicators(df)

    def _v(val):
        try:
            f = float(val)
            return None if (math.isnan(f) or math.isinf(f)) else round(f, 4)
        except Exception:
            return None

    candles, sma200, bb_upper, bb_mid, bb_lower = [], [], [], [], []
    rsi14, macd_line, macd_signal, macd_hist = [], [], [], []

    for date, row in df.iterrows():
        t = str(date)[:10]
        candles.append({
            "time": t,
            "open":  _v(row.get("Open")),
            "high":  _v(row.get("High")),
            "low":   _v(row.get("Low")),
            "close": _v(row.get("Close")),
            "volume": int(row.get("Volume", 0) or 0),
        })
        sma200.append({"time": t, "value": _v(row.get("SMA_200"))})
        bb_upper.append({"time": t, "value": _v(row.get("BB_UPPER"))})
        bb_mid.append({"time": t, "value": _v(row.get("BB_MID"))})
        bb_lower.append({"time": t, "value": _v(row.get("BB_LOWER"))})
        rsi14.append({"time": t, "value": _v(row.get("RSI_14"))})
        macd_line.append({"time": t, "value": _v(row.get("MACD"))})
        macd_signal.append({"time": t, "value": _v(row.get("MACD_SIGNAL"))})
        macd_hist.append({"time": t, "value": _v(row.get("MACD_HIST"))})

    result = {
        "ok": True,
        "ticker": ticker,
        "period": period,
        "candles": candles,
        "sma200": sma200,
        "bb_upper": bb_upper,
        "bb_mid": bb_mid,
        "bb_lower": bb_lower,
        "rsi14": rsi14,
        "macd": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
    }
    _ohlcv_cache[cache_key] = {"data": result, "ts": now_ts}
    return jsonify(result)


@app.route("/api/force_scan", methods=["POST", "OPTIONS"])
def api_force_scan():
    """Manually trigger a portfolio scan (useful when the scheduler hasn't run yet
    or the market is closed but the user wants to populate the dashboard).
    """
    if request.method == "OPTIONS":
        response = app.make_default_options_response()
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    try:
        from portfolio_monitor import run_once
        run_once(force=True, manual=True)
        _github_push()
    except Exception as exc:
        response = jsonify({"ok": False, "error": str(exc)[:200]})
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response, 500

    response = jsonify({"ok": True, "message": "Scan completed."})
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


def _sanitize_json(obj):
    """Recursively replace NaN/Infinity floats with None so the output is
    valid RFC-8259 JSON (JS JSON.parse rejects NaN/Infinity)."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_json(v) for v in obj]
    return obj


@app.route("/api/data")
def api_data():
    """JSON endpoint for the static Netlify dashboard to fetch live data."""
    scans = _load_json(SCAN_HISTORY_FILE, [])
    _attach_display_times(scans)
    cooldowns = _get_cooldowns()
    logs = _get_recent_logs(80)

    all_alerts = []
    for scan in scans:
        for alert in scan.get("alerts_sent", []):
            all_alerts.append(alert)

    response = jsonify(_sanitize_json({
        "scans": scans[:20],
        "cooldowns": cooldowns,
        "all_alerts": all_alerts[:30],
        "logs": [l.rstrip("\n") for l in logs],
        "now": _now_jerusalem_str(),
    }))
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.route("/upload", methods=["POST"])
def upload_excel():
    """Save the uploaded Excel, then check for unmapped stock names.

    Returns one of:
      {"ok": True, "needs_mapping": False, "path": "..."}
        — all stocks already in ticker_map; config updated, ready to scan.
      {"ok": True, "needs_mapping": True, "unmapped": [...], "path": "..."}
        — some stocks have no Yahoo ticker yet; frontend should show the wizard.
      {"ok": False, "error": "..."}
        — file rejected or unreadable.
    """
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file part in request"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"ok": False, "error": "No file selected"}), 400
    if not _allowed_file(file.filename):
        return jsonify({"ok": False, "error": "Only .xlsx / .xls files are allowed"}), 400

    filename = secure_filename(file.filename)
    dest = UPLOADS_DIR / filename
    file.save(str(dest))
    logging.getLogger("dashboard").info("Excel saved: %s", dest)

    cfg = _load_config()
    cfg_excel = cfg.get("excel") or {}
    ticker_column = cfg_excel.get("ticker_column") or "שם נייר"
    header_row = int(cfg_excel.get("header_row") if cfg_excel.get("header_row") is not None else 9)
    ticker_map = cfg.get("ticker_map") or {}

    try:
        names = _extract_excel_names(str(dest), ticker_column, header_row)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    unmapped = _find_unmapped(names, ticker_map)
    if unmapped:
        return jsonify({
            "ok": True,
            "needs_mapping": True,
            "unmapped": unmapped,
            "path": str(dest),
        })

    # All stocks are already mapped — persist the path and we're done.
    _update_config_excel_path(str(dest))
    return jsonify({"ok": True, "needs_mapping": False, "path": str(dest)})


@app.route("/update_ticker_map", methods=["POST"])
def update_ticker_map():
    """Receive new name→ticker mappings, merge them into config.yaml, and
    record the uploaded Excel path so the next scan uses it.

    Expected JSON body:
      {
        "mappings": {"STOCK NAME": "TICK", ...},  // empty values are ignored
        "path": "/abs/path/to/uploaded.xlsx"
      }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False, "error": "No JSON body"}), 400

    raw_mappings: dict = data.get("mappings") or {}
    file_path: str = data.get("path", "").strip()

    # Strip whitespace and upper-case the ticker; drop entries with no ticker.
    clean = {
        name.strip(): ticker.strip().upper()
        for name, ticker in raw_mappings.items()
        if ticker and ticker.strip()
    }

    cfg = _load_config()
    if not isinstance(cfg.get("ticker_map"), dict):
        cfg["ticker_map"] = {}
    cfg["ticker_map"].update(clean)

    if not isinstance(cfg.get("excel"), dict):
        cfg["excel"] = {}
    if file_path:
        cfg["excel"]["file"] = file_path

    _save_config(cfg)
    logging.getLogger("dashboard").info(
        "Ticker map updated: %d new mapping(s) added.", len(clean)
    )
    return jsonify({"ok": True, "added": len(clean)})


# ---------------------------------------------------------------------------
# TEMPLATE
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Portfolio Sentinel — Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>

<style>
/* ═══════════════════════════════════════════════════
   DESIGN TOKENS
═══════════════════════════════════════════════════ */
:root {
  /* Backgrounds */
  --bg-base:        #09090f;
  --bg-surface:     #111118;
  --bg-elevated:    #17171f;
  --bg-overlay:     #1e1e28;

  /* Borders */
  --border:         rgba(255, 255, 255, 0.06);
  --border-mid:     rgba(255, 255, 255, 0.10);
  --border-hi:      rgba(255, 255, 255, 0.16);

  /* Accents — same chroma, varied hue */
  --green:          oklch(66% 0.17 145);
  --green-dim:      oklch(66% 0.17 145 / 0.15);
  --green-glow:     oklch(66% 0.17 145 / 0.25);
  --red:            oklch(60% 0.20 25);
  --red-dim:        oklch(60% 0.20 25 / 0.15);
  --amber:          oklch(72% 0.16 72);
  --amber-dim:      oklch(72% 0.16 72 / 0.15);
  --blue:           oklch(70% 0.16 235);
  --blue-dim:       oklch(70% 0.16 235 / 0.15);
  --purple:         oklch(68% 0.16 290);

  /* Text */
  --text-primary:   #f0effa;
  --text-secondary: rgba(240, 239, 250, 0.65);
  --text-tertiary:  rgba(240, 239, 250, 0.38);
  --text-ghost:     rgba(240, 239, 250, 0.18);

  /* Typography */
  --f-sans: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  --f-mono: 'JetBrains Mono', 'Courier New', monospace;

  /* Spacing */
  --pad-x: clamp(1.25rem, 4vw, 3rem);
  --max-w: 1400px;
  --radius: 10px;
  --radius-sm: 6px;
  --radius-lg: 14px;

  /* Transitions */
  --ease: cubic-bezier(0.25, 0.46, 0.45, 0.94);
  --ease-spring: cubic-bezier(0.16, 1, 0.3, 1);
}

/* ═══════════════════════════════════════════════════
   RESET & BASE
═══════════════════════════════════════════════════ */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 16px; -webkit-font-smoothing: antialiased; scroll-behavior: smooth; }
html, body { overflow-x: hidden; }

body {
  font-family: var(--f-sans);
  background: var(--bg-base);
  color: var(--text-primary);
  min-height: 100vh;
  line-height: 1.5;
  background-image:
    radial-gradient(ellipse 80% 40% at 10% 0%, oklch(66% 0.17 145 / 0.035) 0%, transparent 60%),
    radial-gradient(ellipse 60% 50% at 90% 100%, oklch(70% 0.16 235 / 0.03) 0%, transparent 60%);
}

a { color: inherit; text-decoration: none; }
button, input, select { font: inherit; }

/* ═══════════════════════════════════════════════════
   NAV
═══════════════════════════════════════════════════ */
.nav {
  position: sticky;
  top: 0;
  z-index: 600;
  background: rgba(9, 9, 15, 0.85);
  backdrop-filter: blur(20px) saturate(1.4);
  border-bottom: 1px solid var(--border);
  height: 56px;
}
.nav__inner {
  max-width: var(--max-w);
  margin: 0 auto;
  padding: 0 var(--pad-x);
  height: 100%;
  display: flex;
  align-items: center;
  gap: 1rem;
}
.nav__logo {
  display: flex;
  align-items: center;
  gap: 0.65rem;
  flex-shrink: 0;
}
.nav__logo-mark {
  width: 28px;
  height: 28px;
  background: linear-gradient(135deg, var(--green), oklch(66% 0.17 145 / 0.5));
  border-radius: 7px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.7rem;
  font-weight: 700;
  color: #09090f;
  letter-spacing: -0.02em;
  flex-shrink: 0;
}
.nav__brand {
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--text-primary);
  letter-spacing: -0.01em;
}
.nav__sep {
  width: 1px;
  height: 18px;
  background: var(--border-mid);
  flex-shrink: 0;
}
.nav__status {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  font-size: 0.72rem;
  color: var(--text-tertiary);
  font-weight: 500;
}
.nav__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 6px var(--green);
  animation: pulse 2.5s ease-in-out infinite;
  flex-shrink: 0;
}
@keyframes pulse {
  0%, 100% { opacity: 1; box-shadow: 0 0 6px var(--green); }
  50% { opacity: 0.4; box-shadow: 0 0 2px var(--green); }
}
.nav__spacer { flex: 1; }
.nav__actions {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.nav__refresh-time {
  font-size: 0.7rem;
  color: var(--text-tertiary);
  font-family: var(--f-mono);
  font-weight: 400;
  letter-spacing: 0.02em;
}

.btn {
  display: inline-flex;
  align-items: center;
  gap: 0.4em;
  padding: 0.45rem 0.9rem;
  border-radius: var(--radius-sm);
  font-size: 0.75rem;
  font-weight: 500;
  cursor: pointer;
  border: 1px solid transparent;
  transition: all 0.18s var(--ease);
  white-space: nowrap;
  letter-spacing: -0.01em;
}
.btn-ghost {
  background: transparent;
  border-color: var(--border-mid);
  color: var(--text-secondary);
}
.btn-ghost:hover {
  background: var(--bg-elevated);
  border-color: var(--border-hi);
  color: var(--text-primary);
}
.btn-primary {
  background: var(--green);
  border-color: var(--green);
  color: #09090f;
  font-weight: 600;
}
.btn-primary:hover {
  filter: brightness(1.1);
  box-shadow: 0 0 20px var(--green-glow);
  transform: translateY(-1px);
}
.btn-icon { font-size: 0.85rem; }

/* ═══════════════════════════════════════════════════
   MAIN CONTAINER
═══════════════════════════════════════════════════ */
.dash {
  max-width: var(--max-w);
  margin: 0 auto;
  padding: 2rem var(--pad-x) 4rem;
}

/* ═══════════════════════════════════════════════════
   PAGE HEADER
═══════════════════════════════════════════════════ */
.dash__header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 1rem;
  margin-bottom: 1.75rem;
}
.dash__heading {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.dash__eyebrow {
  font-size: 0.7rem;
  font-weight: 500;
  color: var(--green);
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.dash__title {
  font-size: clamp(1.5rem, 3vw, 2.1rem);
  font-weight: 700;
  letter-spacing: -0.035em;
  color: var(--text-primary);
  line-height: 1.15;
}
.dash__subtitle {
  font-size: 0.78rem;
  color: var(--text-tertiary);
  font-family: var(--f-mono);
  font-weight: 400;
  margin-top: 0.1rem;
}

/* ═══════════════════════════════════════════════════
   STAT STRIP
═══════════════════════════════════════════════════ */
.stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 1px;
  background: var(--border);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  margin-bottom: 1.5rem;
}
.stat {
  background: var(--bg-surface);
  padding: 1.25rem 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  position: relative;
  transition: background 0.2s;
  overflow: hidden;
}
.stat::before {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(135deg, var(--stat-color, var(--green)) 0%, transparent 60%);
  opacity: 0;
  transition: opacity 0.25s;
}
.stat:hover::before { opacity: 0.04; }
.stat:hover { background: var(--bg-elevated); }
.stat__icon {
  width: 28px;
  height: 28px;
  border-radius: var(--radius-sm);
  background: var(--stat-color-dim, var(--green-dim));
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.8rem;
  margin-bottom: 0.25rem;
}
.stat__value {
  font-size: clamp(1.6rem, 3vw, 2.1rem);
  font-weight: 700;
  font-family: var(--f-mono);
  letter-spacing: -0.04em;
  color: var(--text-primary);
  line-height: 1;
}
.stat__label {
  font-size: 0.72rem;
  font-weight: 500;
  color: var(--text-tertiary);
  letter-spacing: 0.01em;
}
.stat__delta {
  font-size: 0.68rem;
  font-weight: 500;
  font-family: var(--f-mono);
  color: var(--stat-color, var(--green));
  display: flex;
  align-items: center;
  gap: 0.2em;
  margin-top: 0.1rem;
}

@media (max-width: 640px) {
  .stats { grid-template-columns: repeat(2, 1fr); border-radius: var(--radius); }
}

/* ═══════════════════════════════════════════════════
   TECHNICAL SUMMARY BAND
═══════════════════════════════════════════════════ */
.summary-band {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 1.25rem 1.5rem;
  margin-bottom: 1.5rem;
  display: flex;
  align-items: center;
  gap: 1.5rem;
  flex-wrap: wrap;
  animation: fadeInUp 0.4s var(--ease-spring) both;
}
.summary-band__label {
  font-size: 0.7rem;
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  flex-shrink: 0;
}
.summary-band__items {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
  flex: 1;
}
.summary-badge {
  display: inline-flex;
  align-items: center;
  gap: 0.4em;
  padding: 0.35rem 0.75rem;
  border-radius: 999px;
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.01em;
  border: 1px solid transparent;
  transition: all 0.18s;
  cursor: default;
}
.summary-badge--buy { background: var(--green-dim); color: var(--green); border-color: oklch(66% 0.17 145 / 0.25); }
.summary-badge--sell { background: var(--red-dim); color: var(--red); border-color: oklch(60% 0.20 25 / 0.25); }
.summary-badge--hold { background: var(--amber-dim); color: var(--amber); border-color: oklch(72% 0.16 72 / 0.25); }
.summary-badge--neutral { background: oklch(68% 0.05 270 / 0.12); color: var(--text-secondary); border-color: var(--border-mid); }
.summary-badge__dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.summary-band__divider { width: 1px; height: 28px; background: var(--border); flex-shrink: 0; }
.summary-band__scan-info {
  font-size: 0.72rem;
  color: var(--text-tertiary);
  font-family: var(--f-mono);
}

/* ═══════════════════════════════════════════════════
   GRID
═══════════════════════════════════════════════════ */
.grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
  margin-bottom: 1rem;
}
@media (max-width: 860px) { .grid { grid-template-columns: 1fr; } }

/* ═══════════════════════════════════════════════════
   CARD
═══════════════════════════════════════════════════ */
.card {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  animation: fadeInUp 0.45s var(--ease-spring) both;
  transition: border-color 0.2s, box-shadow 0.2s;
}
.card:hover {
  border-color: var(--border-mid);
  box-shadow: 0 4px 24px rgba(0,0,0,0.28);
}
.card--full { grid-column: 1 / -1; }
.card--mb { margin-bottom: 1rem; }
.card__head {
  padding: 1rem 1.25rem;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 0.6rem;
  flex-wrap: wrap;
}
.card__title {
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--text-primary);
  letter-spacing: -0.01em;
}
.card__count {
  margin-left: auto;
  font-size: 0.68rem;
  font-weight: 500;
  color: var(--text-tertiary);
  font-family: var(--f-mono);
  background: var(--bg-overlay);
  padding: 0.18rem 0.5rem;
  border-radius: 4px;
}
.card__icon {
  width: 22px;
  height: 22px;
  border-radius: 5px;
  background: var(--bg-elevated);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.72rem;
  flex-shrink: 0;
}

/* ═══════════════════════════════════════════════════
   TABLE
═══════════════════════════════════════════════════ */
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; min-width: 420px; }
th {
  text-align: left;
  font-size: 0.68rem;
  font-weight: 600;
  color: var(--text-tertiary);
  padding: 0.65rem 1rem;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  background: var(--bg-elevated);
}
td {
  font-size: 0.78rem;
  padding: 0.7rem 1rem;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
  color: var(--text-secondary);
  vertical-align: middle;
}
tr:last-child td { border-bottom: none; }
tbody tr {
  transition: background 0.12s;
  cursor: default;
}
tbody tr:hover td { background: var(--bg-elevated); }
tbody tr[data-ticker] { cursor: pointer; }

.td-ticker {
  font-weight: 700;
  color: var(--text-primary);
  font-family: var(--f-mono);
  font-size: 0.82rem;
  letter-spacing: 0.02em;
}
.td-mono {
  font-family: var(--f-mono);
  font-size: 0.76rem;
}
.td-name {
  color: var(--text-tertiary);
  font-size: 0.74rem;
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ═══════════════════════════════════════════════════
   BADGES
═══════════════════════════════════════════════════ */
.badge {
  display: inline-flex;
  align-items: center;
  gap: 0.3em;
  padding: 0.22rem 0.55rem;
  border-radius: 5px;
  font-size: 0.68rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  white-space: nowrap;
}
.badge-buy    { background: var(--green-dim);  color: var(--green); }
.badge-sell   { background: var(--red-dim);    color: var(--red);   }
.badge-hold   { background: var(--amber-dim);  color: var(--amber); }
.badge-ok     { background: var(--green-dim);  color: var(--green); }
.badge-fail   { background: var(--red-dim);    color: var(--red);   }
.badge-forced { background: var(--amber-dim);  color: var(--amber); }
.badge-info   { background: var(--blue-dim);   color: var(--blue);  }
.badge::before { content: ''; }

/* ═══════════════════════════════════════════════════
   SCORE PIPS (replacing X/4)
═══════════════════════════════════════════════════ */
.pips {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}
.pip {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  background: var(--border-hi);
  transition: background 0.2s;
}
.pip--filled-buy  { background: var(--green); box-shadow: 0 0 4px var(--green-glow); }
.pip--filled-sell { background: var(--red);   box-shadow: 0 0 4px oklch(60% 0.20 25 / 0.4); }
.pips-label {
  font-size: 0.68rem;
  font-family: var(--f-mono);
  font-weight: 600;
  margin-left: 5px;
  color: var(--text-tertiary);
}
.pips-label--high { color: var(--green); }
.pips-label--mid  { color: var(--amber); }

/* ═══════════════════════════════════════════════════
   RSI / DELTA COLORS
═══════════════════════════════════════════════════ */
.val-up    { color: var(--green); font-family: var(--f-mono); font-weight: 600; }
.val-down  { color: var(--red);   font-family: var(--f-mono); font-weight: 600; }
.val-mid   { color: var(--amber); font-family: var(--f-mono); font-weight: 600; }
.val-neutral { color: var(--text-secondary); font-family: var(--f-mono); }

/* ═══════════════════════════════════════════════════
   COOLDOWN BAR
═══════════════════════════════════════════════════ */
.cooldown-wrap {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 120px;
}
.cooldown-label {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.cooldown-text {
  font-size: 0.7rem;
  font-family: var(--f-mono);
  color: var(--text-secondary);
}
.cooldown-track {
  height: 5px;
  border-radius: 999px;
  background: var(--bg-overlay);
  overflow: hidden;
}
.cooldown-fill {
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(90deg, var(--amber), oklch(72% 0.16 55));
  transition: width 0.6s var(--ease-spring);
  box-shadow: 0 0 6px oklch(72% 0.16 72 / 0.5);
}

/* ═══════════════════════════════════════════════════
   EMPTY STATE
═══════════════════════════════════════════════════ */
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.5rem;
  padding: 2.5rem 1.5rem;
  color: var(--text-tertiary);
  font-size: 0.78rem;
  text-align: center;
}
.empty-state__icon {
  font-size: 1.5rem;
  opacity: 0.4;
  margin-bottom: 0.25rem;
}

/* ═══════════════════════════════════════════════════
   LOGS
═══════════════════════════════════════════════════ */
.logs-wrap {
  background: var(--bg-base);
  border-radius: 0 0 var(--radius-lg) var(--radius-lg);
  padding: 1rem 1.25rem;
  max-height: 380px;
  overflow: auto;
}
pre.logs {
  font-family: var(--f-mono);
  font-size: 0.72rem;
  line-height: 1.75;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}
pre.logs .log-info  { color: var(--blue); }
pre.logs .log-warn  { color: var(--amber); }
pre.logs .log-error { color: var(--red); }
pre.logs .log-ok    { color: var(--green); }

/* Scrollbar styling */
.logs-wrap::-webkit-scrollbar { width: 6px; height: 6px; }
.logs-wrap::-webkit-scrollbar-track { background: transparent; }
.logs-wrap::-webkit-scrollbar-thumb { background: var(--border-hi); border-radius: 3px; }

/* ═══════════════════════════════════════════════════
   CHART
═══════════════════════════════════════════════════ */
.chart-controls {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-left: auto;
  flex-wrap: wrap;
}
.chart-select {
  padding: 0.32rem 0.7rem;
  background: var(--bg-elevated);
  border: 1px solid var(--border-mid);
  color: var(--text-primary);
  border-radius: var(--radius-sm);
  font-size: 0.74rem;
  font-weight: 500;
  outline: none;
  cursor: pointer;
  transition: border-color 0.15s;
  -webkit-appearance: none;
  appearance: none;
  padding-right: 1.75rem;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='rgba(240,239,250,0.4)' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 0.5rem center;
}
.chart-select:focus { border-color: var(--green); box-shadow: 0 0 0 2px var(--green-dim); }
.chart-select option { background: var(--bg-elevated); }
.chart-panel-label {
  font-size: 0.66rem;
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  padding: 0.4rem 1.25rem 0.2rem;
}
#chart-status {
  font-size: 0.72rem;
  font-family: var(--f-mono);
  color: var(--text-tertiary);
  padding: 0.4rem 1.25rem;
  min-height: 1.5rem;
}

/* ═══════════════════════════════════════════════════
   UPLOAD MODAL
═══════════════════════════════════════════════════ */
.modal-overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(9, 9, 15, 0.8);
  z-index: 1000;
  justify-content: center;
  align-items: center;
  backdrop-filter: blur(8px);
  animation: fadeIn 0.2s ease;
}
.modal-overlay.open { display: flex; }
.modal {
  background: var(--bg-elevated);
  border: 1px solid var(--border-mid);
  border-radius: var(--radius-lg);
  padding: 1.75rem;
  width: 420px;
  max-width: 94vw;
  max-height: 90vh;
  overflow-y: auto;
  box-shadow: 0 24px 80px rgba(0,0,0,0.6);
  animation: slideUp 0.3s var(--ease-spring);
}
.modal.mapping-mode { width: 500px; }
.modal__title {
  font-size: 1.05rem;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--text-primary);
  margin-bottom: 0.35rem;
}
.modal__desc {
  font-size: 0.78rem;
  color: var(--text-secondary);
  line-height: 1.6;
  margin-bottom: 1.25rem;
}
.drop-zone {
  border: 1.5px dashed var(--border-hi);
  border-radius: var(--radius);
  padding: 1.75rem 1.25rem;
  text-align: center;
  cursor: pointer;
  transition: all 0.2s;
  background: var(--bg-surface);
  margin-bottom: 0.75rem;
}
.drop-zone:hover, .drop-zone.drag-over {
  border-color: var(--green);
  background: var(--green-dim);
}
.drop-zone input[type="file"] { display: none; }
.drop-zone__label {
  font-size: 0.76rem;
  color: var(--text-secondary);
}
.drop-zone__label span {
  color: var(--green);
  text-decoration: underline;
  cursor: pointer;
  font-weight: 500;
}
.drop-zone__name {
  font-size: 0.74rem;
  color: var(--green);
  margin-top: 0.5rem;
  min-height: 1em;
  font-family: var(--f-mono);
}
.upload-status { font-size: 0.74rem; margin-top: 0.5rem; min-height: 1rem; font-family: var(--f-mono); }
.upload-status.ok  { color: var(--green); }
.upload-status.err { color: var(--red); }
.modal__actions {
  display: flex;
  gap: 0.5rem;
  justify-content: flex-end;
  margin-top: 1rem;
}
.btn-cancel-modal {
  background: transparent;
  border: 1px solid var(--border-mid);
  color: var(--text-secondary);
  padding: 0.5rem 1rem;
  border-radius: var(--radius-sm);
  font-size: 0.76rem;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
}
.btn-cancel-modal:hover { background: var(--bg-overlay); color: var(--text-primary); }
.btn-submit-modal {
  background: var(--green);
  border: 1px solid var(--green);
  color: #09090f;
  padding: 0.5rem 1.1rem;
  border-radius: var(--radius-sm);
  font-size: 0.76rem;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
}
.btn-submit-modal:hover { filter: brightness(1.1); }
.btn-submit-modal:disabled { opacity: 0.45; cursor: not-allowed; filter: none; }

/* Mapping */
.mapping-scroll { max-height: 260px; overflow-y: auto; margin-bottom: 0.75rem; }
.mapping-row {
  display: grid;
  grid-template-columns: 1fr auto 130px;
  align-items: center;
  gap: 0.5rem;
  padding: 0.45rem 0;
  border-bottom: 1px solid var(--border);
}
.mapping-row:last-child { border-bottom: none; }
.mapping-name { font-size: 0.74rem; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mapping-arrow { color: var(--text-tertiary); }
.mapping-input {
  padding: 0.35rem 0.6rem;
  background: var(--bg-surface);
  border: 1px solid var(--border-mid);
  color: var(--text-primary);
  border-radius: var(--radius-sm);
  font-size: 0.74rem;
  font-family: var(--f-mono);
  text-transform: uppercase;
  outline: none;
  width: 100%;
  transition: border-color 0.15s;
}
.mapping-input:focus { border-color: var(--green); box-shadow: 0 0 0 2px var(--green-dim); }
.mapping-skip-note { font-size: 0.68rem; color: var(--text-tertiary); margin-bottom: 0.5rem; font-style: italic; }
.mapping-intro strong { color: var(--amber); }

/* ═══════════════════════════════════════════════════
   REFRESH BUTTON (FAB)
═══════════════════════════════════════════════════ */
.fab {
  position: fixed;
  bottom: 1.5rem;
  right: 1.5rem;
  width: 44px;
  height: 44px;
  border-radius: 50%;
  background: var(--bg-elevated);
  border: 1px solid var(--border-mid);
  color: var(--text-secondary);
  font-size: 1rem;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4px 20px rgba(0,0,0,0.4);
  transition: all 0.2s;
  z-index: 100;
}
.fab:hover {
  background: var(--bg-overlay);
  border-color: var(--green);
  color: var(--green);
  transform: rotate(20deg) scale(1.08);
  box-shadow: 0 4px 24px rgba(0,0,0,0.5), 0 0 16px var(--green-glow);
}

/* ═══════════════════════════════════════════════════
   FOOTER
═══════════════════════════════════════════════════ */
.foot {
  margin-top: 3rem;
  border-top: 1px solid var(--border);
  padding: 1.5rem var(--pad-x);
}
.foot__inner {
  max-width: var(--max-w);
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.75rem;
}
.foot__left {
  font-size: 0.72rem;
  color: var(--text-tertiary);
}
.foot__right {
  font-size: 0.72rem;
  color: var(--text-tertiary);
  transition: color 0.2s;
}
.foot__right:hover { color: var(--text-primary); }
.foot__copy {
  max-width: var(--max-w);
  margin: 0.75rem auto 0;
  font-size: 0.68rem;
  color: var(--text-ghost);
  text-align: center;
}

/* ═══════════════════════════════════════════════════
   SKELETON LOADER
═══════════════════════════════════════════════════ */
.skeleton {
  background: linear-gradient(90deg, var(--bg-elevated) 25%, var(--bg-overlay) 50%, var(--bg-elevated) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 4px;
}
@keyframes shimmer {
  from { background-position: 200% 0; }
  to   { background-position: -200% 0; }
}

/* ═══════════════════════════════════════════════════
   ANIMATIONS
═══════════════════════════════════════════════════ */
@keyframes fadeInUp {
  from { opacity: 0; transform: translateY(12px); }
  to   { opacity: 1; transform: translateY(0); }
}
@keyframes fadeIn {
  from { opacity: 0; }
  to   { opacity: 1; }
}
@keyframes slideUp {
  from { opacity: 0; transform: translateY(20px) scale(0.97); }
  to   { opacity: 1; transform: translateY(0) scale(1); }
}

/* Staggered card animation */
.card:nth-child(1) { animation-delay: 0.05s; }
.card:nth-child(2) { animation-delay: 0.1s; }
.card:nth-child(3) { animation-delay: 0.15s; }
.card:nth-child(4) { animation-delay: 0.2s; }
.card:nth-child(5) { animation-delay: 0.25s; }
.card:nth-child(6) { animation-delay: 0.3s; }

/* ═══════════════════════════════════════════════════
   RESPONSIVE
═══════════════════════════════════════════════════ */
@media (max-width: 768px) {
  .dash { padding: 1.25rem var(--pad-x) 3rem; }
  .dash__title { font-size: 1.4rem; }
  .stat { padding: 1rem 1.1rem; }
  .nav__refresh-time { display: none; }
}
@media (max-width: 520px) {
  .nav__brand { display: none; }
  .nav__sep { display: none; }
  .foot__inner { flex-direction: column; text-align: center; }
}
</style>
</head>
<body>

<!-- ── Upload Modal ─────────────────────────────────────────── -->
<div class="modal-overlay" id="uploadModal">
  <div class="modal" id="uploadModalInner">
    <div id="uploadPanel">
      <h3 class="modal__title">Upload Portfolio Excel</h3>
      <p class="modal__desc">Upload your Excel export from your brokerage. The file will be saved and used in the next scan automatically.</p>
      <div class="drop-zone" id="uploadDropZone">
        <input type="file" id="uploadFileInput" accept=".xlsx,.xls">
        <div class="drop-zone__label">Drag &amp; drop, or <span id="uploadBrowse">browse files</span></div>
        <div class="drop-zone__name" id="uploadFileName"></div>
      </div>
      <div class="upload-status" id="uploadStatus"></div>
      <div class="modal__actions">
        <button class="btn-cancel-modal" id="uploadCancel">Cancel</button>
        <button class="btn-submit-modal" id="uploadSubmit" disabled>Upload</button>
      </div>
    </div>
    <div id="mappingPanel" style="display:none">
      <h3 class="modal__title">Map New Stocks</h3>
      <p class="modal__desc mapping-intro">These stocks from your Excel have no Yahoo Finance ticker yet. Enter the symbol for each one. <strong>Leave blank to skip.</strong></p>
      <div class="mapping-scroll" id="mappingRows"></div>
      <p class="mapping-skip-note">Tip: find tickers at finance.yahoo.com — e.g. AAPL, NVDA, CSPX.L</p>
      <div class="upload-status" id="mappingStatus"></div>
      <div class="modal__actions">
        <button class="btn-cancel-modal" id="mappingCancel">Cancel</button>
        <button class="btn-submit-modal" id="mappingSubmit">Save &amp; Continue</button>
      </div>
    </div>
  </div>
</div>

<!-- ── Nav ──────────────────────────────────────────────────── -->
<nav class="nav">
  <div class="nav__inner">
    <div class="nav__logo">
      <div class="nav__logo-mark">PS</div>
      <span class="nav__brand">Portfolio Sentinel</span>
    </div>
    <div class="nav__sep"></div>
    <div class="nav__status">
      <div class="nav__dot"></div>
      <span>Dashboard</span>
    </div>
    <div class="nav__spacer"></div>
    <span class="nav__refresh-time" id="lastRefreshed">—</span>
    <div class="nav__actions">
      <button class="btn btn-ghost" id="uploadBtn"><span class="btn-icon">↑</span> Upload Excel</button>
      <a class="btn btn-ghost" href="index.html">← Landing</a>
    </div>
  </div>
</nav>

<!-- ── Main ─────────────────────────────────────────────────── -->
<div class="dash">

  <!-- Header -->
  <div class="dash__header">
    <div class="dash__heading">
      <span class="dash__eyebrow">Live Monitoring</span>
      <h1 class="dash__title">Portfolio Monitor</h1>
      <span class="dash__subtitle" id="lastRefreshedFull">Fetching latest data…</span>
    </div>
  </div>

  <!-- Stats -->
  <div class="stats">
    <div class="stat" style="--stat-color: var(--blue); --stat-color-dim: var(--blue-dim);">
      <div class="stat__icon">📊</div>
      <div class="stat__value" id="statScans">—</div>
      <div class="stat__label">Total Scans</div>
    </div>
    <div class="stat" style="--stat-color: var(--amber); --stat-color-dim: var(--amber-dim);">
      <div class="stat__icon">🔔</div>
      <div class="stat__value" id="statAlerts">—</div>
      <div class="stat__label">Alerts Sent</div>
    </div>
    <div class="stat" style="--stat-color: var(--red); --stat-color-dim: var(--red-dim);">
      <div class="stat__icon">⏱</div>
      <div class="stat__value" id="statCooldowns">—</div>
      <div class="stat__label">Active Cooldowns</div>
    </div>
    <div class="stat" style="--stat-color: var(--green); --stat-color-dim: var(--green-dim);">
      <div class="stat__icon">📈</div>
      <div class="stat__value" id="statTickers">—</div>
      <div class="stat__label">Tickers Tracked</div>
    </div>
  </div>

  <!-- Technical Summary Band -->
  <div class="summary-band card" id="summaryBand">
    <span class="summary-band__label">Technical Summary</span>
    <div class="summary-band__items" id="summaryItems">
      <span class="summary-badge summary-badge--neutral">Awaiting scan data…</span>
    </div>
    <div class="summary-band__divider"></div>
    <span class="summary-band__scan-info" id="summaryInfo">—</span>
  </div>

  <!-- 2-col grid -->
  <div class="grid">
    <!-- Recent Scans -->
    <div class="card">
      <div class="card__head">
        <div class="card__icon">🕒</div>
        <span class="card__title">Recent Scans</span>
        <span class="card__count" id="scansCount">0</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>Time</th><th>Market</th><th>Tickers</th><th>Alerts</th><th>Errors</th>
          </tr></thead>
          <tbody id="scansBody">
            <tr><td colspan="5"><div class="empty-state"><div class="empty-state__icon">📭</div>Waiting for scan data…</div></td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Active Cooldowns -->
    <div class="card">
      <div class="card__head">
        <div class="card__icon">⏱</div>
        <span class="card__title">Active Cooldowns</span>
        <span class="card__count" id="cooldownsCount">0</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr>
            <th>Ticker</th><th>Side</th><th>Alerted At</th><th>Remaining</th>
          </tr></thead>
          <tbody id="cooldownsBody">
            <tr><td colspan="4"><div class="empty-state"><div class="empty-state__icon">✅</div>No active cooldowns</div></td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Latest Scan Results -->
  <div class="card card--full card--mb">
    <div class="card__head">
      <div class="card__icon">🔍</div>
      <span class="card__title" id="scanResultsTitle">Latest Scan Results</span>
      <span class="card__count" id="resultsCount">0</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Ticker</th><th>Name</th><th>Close</th><th>RSI</th><th>vs SMA200</th><th>Buy Signal</th><th>Sell Signal</th>
        </tr></thead>
        <tbody id="resultsBody">
          <tr><td colspan="7"><div class="empty-state"><div class="empty-state__icon">🔍</div>No scan results yet</div></td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Alert History -->
  <div class="card card--full card--mb">
    <div class="card__head">
      <div class="card__icon">📣</div>
      <span class="card__title">Alert History</span>
      <span class="card__count" id="alertsCount">0</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Time</th><th>Ticker</th><th>Signal</th><th>Score</th><th>Indicators</th><th>AI Verdict</th><th>Sent</th>
        </tr></thead>
        <tbody id="alertsBody">
          <tr><td colspan="7"><div class="empty-state"><div class="empty-state__icon">📭</div>No alerts sent yet</div></td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Technical Chart -->
  <div class="card card--full card--mb" id="chart-card">
    <div class="card__head">
      <div class="card__icon">📉</div>
      <span class="card__title">Technical Chart</span>
      <div class="chart-controls">
        <select class="chart-select" id="chart-ticker" aria-label="Select ticker"></select>
        <select class="chart-select" id="chart-period" aria-label="Select period">
          <option value="1mo">1M</option>
          <option value="3mo">3M</option>
          <option value="6mo" selected>6M</option>
          <option value="1y">1Y</option>
          <option value="2y">2Y</option>
        </select>
      </div>
    </div>
    <div class="chart-panel-label">Price · SMA200 · Bollinger Bands</div>
    <div id="chart-price" style="height:320px;"></div>
    <div class="chart-panel-label" style="margin-top:2px;">Volume</div>
    <div id="chart-volume" style="height:72px;"></div>
    <div class="chart-panel-label" style="margin-top:2px;">RSI 14</div>
    <div id="chart-rsi" style="height:90px;"></div>
    <div class="chart-panel-label" style="margin-top:2px;">MACD (12, 26, 9)</div>
    <div id="chart-macd" style="height:90px;"></div>
    <div id="chart-status"></div>
  </div>

  <!-- Recent Logs -->
  <div class="card card--full">
    <div class="card__head">
      <div class="card__icon">📋</div>
      <span class="card__title">Recent Logs</span>
    </div>
    <div class="logs-wrap">
      <pre class="logs" id="logsContent">No log entries yet.</pre>
    </div>
  </div>

</div>

<!-- FAB Refresh -->
<button class="fab" onclick="location.reload()" title="Refresh dashboard">↻</button>

<!-- Footer -->
<footer class="foot">
  <div class="foot__inner">
    <span class="foot__left">Built with Claude Code</span>
    <a class="foot__right" href="https://www.linkedin.com/in/ariel-guralnick-b01802206/" target="_blank" rel="noopener">→ Ariel Guralnick / LinkedIn</a>
  </div>
  <p class="foot__copy">© 2026 Ariel Guralnick — All rights reserved. For educational purposes only. Not financial advice.</p>
</footer>

<script>
(function () {
  "use strict";

  var BACKEND_URL = '';

  /* ── Helpers ── */
  function esc(s) {
    return String(s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;')
      .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  /* Build pip HTML */
  function pips(score, max, side) {
    max = max || 4;
    var filledClass = side === 'sell' ? 'pip--filled-sell' : 'pip--filled-buy';
    var html = '<span class="pips">';
    for (var i = 0; i < max; i++) {
      html += '<span class="pip' + (i < score ? ' ' + filledClass : '') + '"></span>';
    }
    var labelClass = score >= 3 ? 'pips-label--high' : (score >= 2 ? 'pips-label--mid' : '');
    html += '<span class="pips-label ' + labelClass + '">' + score + '/' + max + '</span>';
    html += '</span>';
    return html;
  }

  /* Animate count-up for stat values */
  function countUp(el, target) {
    if (isNaN(target)) { el.textContent = target; return; }
    var start = 0;
    var duration = 600;
    var startTime = null;
    function step(ts) {
      if (!startTime) startTime = ts;
      var progress = Math.min((ts - startTime) / duration, 1);
      var ease = 1 - Math.pow(1 - progress, 3);
      el.textContent = Math.round(start + (target - start) * ease);
      if (progress < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  /* ── Loading state ── */
  function setLoading() {
    document.getElementById('lastRefreshedFull').textContent = 'Loading live data…';
    ['statScans','statAlerts','statCooldowns','statTickers'].forEach(function(id) {
      document.getElementById(id).textContent = '—';
    });
  }

  /* ── Error state ── */
  function setError(msg) {
    document.getElementById('lastRefreshedFull').textContent = 'Error: ' + msg;
    var errHtml = '<tr><td colspan="99"><div class="empty-state"><div class="empty-state__icon">⚠️</div>Could not reach backend — ' + esc(msg) + '</div></td></tr>';
    ['scansBody','resultsBody','alertsBody','cooldownsBody'].forEach(function(id) {
      document.getElementById(id).innerHTML = errHtml;
    });
    document.getElementById('logsContent').textContent = 'Could not reach backend.';
    ['statScans','statAlerts','statCooldowns','statTickers'].forEach(function(id) {
      document.getElementById(id).textContent = '—';
    });
  }

  /* ── Build Technical Summary Band ── */
  function buildSummary(data) {
    var latestScan = data.scans && data.scans[0];
    if (!latestScan || !latestScan.results || !latestScan.results.length) return;

    var results = latestScan.results;
    var buyCount = 0, sellCount = 0, holdCount = 0;
    var topBuys = [], topSells = [];

    results.forEach(function(r) {
      var buyScore = r.buy_score || 0;
      var sellScore = r.sell_score || 0;
      if (buyScore >= 3) { buyCount++; topBuys.push(r.ticker); }
      else if (sellScore >= 3) { sellCount++; topSells.push(r.ticker); }
      else holdCount++;
    });

    var html = '';
    if (buyCount > 0) {
      topBuys.slice(0,4).forEach(function(t) {
        html += '<span class="summary-badge summary-badge--buy"><span class="summary-badge__dot"></span>' + esc(t) + ' BUY</span>';
      });
    }
    if (sellCount > 0) {
      topSells.slice(0,4).forEach(function(t) {
        html += '<span class="summary-badge summary-badge--sell"><span class="summary-badge__dot"></span>' + esc(t) + ' SELL</span>';
      });
    }
    if (html === '') {
      html = '<span class="summary-badge summary-badge--neutral">No active signals — all within threshold</span>';
    }

    document.getElementById('summaryItems').innerHTML = html;
    var ts = (latestScan.timestamp || '').slice(0,16).replace('T',' ');
    document.getElementById('summaryInfo').textContent = results.length + ' tickers · ' + ts + ' UTC';
  }

  /* ── Populate dashboard ── */
  function populateDashboard(data) {
    var now = data.now || '';
    document.getElementById('lastRefreshed').textContent = now;
    document.getElementById('lastRefreshedFull').textContent = 'Last refreshed: ' + now;

    /* Stats */
    countUp(document.getElementById('statScans'),     data.scans.length);
    countUp(document.getElementById('statAlerts'),    data.all_alerts.length);
    var cdCount = Object.keys(data.cooldowns || {}).length;
    countUp(document.getElementById('statCooldowns'), cdCount);
    var tickerCount = (data.scans.length > 0 && data.scans[0].results) ? data.scans[0].results.length : 0;
    countUp(document.getElementById('statTickers'),   tickerCount);

    /* Technical Summary */
    buildSummary(data);

    /* ── Recent Scans ── */
    if (data.scans.length > 0) {
      document.getElementById('scansCount').textContent = data.scans.length;
      var scansHtml = '';
      data.scans.slice(0, 15).forEach(function(s) {
        var ts = (s.timestamp || '').slice(0, 16).replace('T', ' ');
        var errCount = (s.errors || []).length;
        var forcedBadge = s.forced ? ' <span class="badge badge-forced">Forced</span>' : '';
        scansHtml += '<tr>'
          + '<td class="td-mono">' + esc(ts) + '</td>'
          + '<td>' + esc(s.market_status || '') + forcedBadge + '</td>'
          + '<td class="td-mono">' + (s.tickers_count || 0) + '</td>'
          + '<td class="td-mono">' + (s.alerts_sent ? s.alerts_sent.length : 0) + '</td>'
          + '<td class="td-mono">' + (errCount ? '<span style="color:var(--red)">' + errCount + '</span>' : '<span style="color:var(--green)">0</span>') + '</td>'
          + '</tr>';
      });
      document.getElementById('scansBody').innerHTML = scansHtml;
    } else {
      document.getElementById('scansBody').innerHTML =
        '<tr><td colspan="5"><div class="empty-state"><div class="empty-state__icon">📭</div>No scans recorded yet</div></td></tr>';
    }

    /* ── Scan Results ── */
    var latestScan = data.scans.length > 0 ? data.scans[0] : null;
    if (latestScan && latestScan.results && latestScan.results.length > 0) {
      var scanTs = (latestScan.timestamp || '').slice(0, 16).replace('T', ' ');
      document.getElementById('scanResultsTitle').textContent = 'Latest Scan Results';
      document.getElementById('resultsCount').textContent = latestScan.results.length;
      var resHtml = '';
      latestScan.results.forEach(function(r) {
        var rsiVal = parseFloat(r.rsi || 0);
        var rsiClass = rsiVal < 35 ? 'val-up' : (rsiVal > 70 ? 'val-down' : 'val-neutral');
        var smaVal = r.sma200_delta_pct || 0;
        var smaClass = smaVal >= 0 ? 'val-up' : 'val-down';
        var smaText = (smaVal >= 0 ? '+' : '') + parseFloat(smaVal).toFixed(1) + '%';
        var buyScore = r.buy_score || 0;
        var sellScore = r.sell_score || 0;
        resHtml += '<tr>'
          + '<td class="td-ticker">' + esc(r.ticker || '') + '</td>'
          + '<td class="td-name">' + esc(r.name || '') + '</td>'
          + '<td class="td-mono">$' + parseFloat(r.close || 0).toFixed(2) + '</td>'
          + '<td><span class="' + rsiClass + '">' + rsiVal.toFixed(1) + '</span></td>'
          + '<td><span class="' + smaClass + '">' + esc(smaText) + '</span></td>'
          + '<td>' + pips(buyScore, 4, 'buy') + '</td>'
          + '<td>' + pips(sellScore, 4, 'sell') + '</td>'
          + '</tr>';
      });
      document.getElementById('resultsBody').innerHTML = resHtml;
    } else {
      document.getElementById('resultsBody').innerHTML =
        '<tr><td colspan="7"><div class="empty-state"><div class="empty-state__icon">🔍</div>No scan results yet</div></td></tr>';
    }

    /* ── Alerts ── */
    var alerts = data.all_alerts || [];
    document.getElementById('alertsCount').textContent = alerts.length;
    if (alerts.length > 0) {
      var alertHtml = '';
      alerts.forEach(function(a) {
        var sideClass = a.side === 'BUY' ? 'badge-buy' : 'badge-sell';
        var ai = a.ai_sentiment || '';
        var aiClass = ai === 'BUY' ? 'badge-buy' : (ai === 'SELL' ? 'badge-sell' : 'badge-hold');
        var aiCell = ai
          ? '<span class="badge ' + aiClass + '">' + esc(ai) + '</span>'
          : '<span style="color:var(--text-tertiary)">—</span>';
        var sentCell = a.whatsapp_sent
          ? '<span class="badge badge-ok">✓ Sent</span>'
          : '<span class="badge badge-fail">✗ Failed</span>';
        var ts = (a.timestamp || '').slice(0, 16).replace('T', ' ');
        var score = a.score || 0;
        alertHtml += '<tr data-ticker="' + esc(a.ticker || '') + '" title="Click to chart ' + esc(a.ticker || '') + '">'
          + '<td class="td-mono">' + esc(ts) + '</td>'
          + '<td class="td-ticker">' + esc(a.ticker || '') + '</td>'
          + '<td><span class="badge ' + sideClass + '">' + esc(a.side || '') + '</span></td>'
          + '<td>' + pips(score, 4, (a.side || '').toLowerCase()) + '</td>'
          + '<td style="color:var(--text-secondary);font-size:0.74rem;">' + esc(a.indicators || '—') + '</td>'
          + '<td>' + aiCell + '</td>'
          + '<td>' + sentCell + '</td>'
          + '</tr>';
      });
      document.getElementById('alertsBody').innerHTML = alertHtml;
    } else {
      document.getElementById('alertsBody').innerHTML =
        '<tr><td colspan="7"><div class="empty-state"><div class="empty-state__icon">📭</div>No alerts sent yet</div></td></tr>';
    }

    /* ── Cooldowns ── */
    var cooldowns = data.cooldowns || {};
    var cdTickers = Object.keys(cooldowns);
    document.getElementById('cooldownsCount').textContent = cdTickers.length;
    if (cdTickers.length > 0) {
      var cdHtml = '';
      cdTickers.forEach(function(ticker) {
        var sides = cooldowns[ticker];
        Object.keys(sides).forEach(function(side) {
          var info = sides[side];
          var pct = Math.min(100, Math.round((info.hours_left / 48) * 100));
          var sideClass = side === 'BUY' ? 'badge-buy' : 'badge-sell';
          cdHtml += '<tr>'
            + '<td class="td-ticker">' + esc(ticker) + '</td>'
            + '<td><span class="badge ' + sideClass + '">' + esc(side) + '</span></td>'
            + '<td class="td-mono" style="font-size:0.72rem;">' + esc(info.alerted_at || '') + '</td>'
            + '<td><div class="cooldown-wrap">'
              + '<div class="cooldown-label"><span class="cooldown-text">' + info.hours_left + 'h left</span></div>'
              + '<div class="cooldown-track"><div class="cooldown-fill" style="width:' + pct + '%"></div></div>'
              + '</div></td>'
            + '</tr>';
        });
      });
      document.getElementById('cooldownsBody').innerHTML = cdHtml;
    } else {
      document.getElementById('cooldownsBody').innerHTML =
        '<tr><td colspan="4"><div class="empty-state"><div class="empty-state__icon">✅</div>No active cooldowns</div></td></tr>';
    }

    /* ── Logs ── */
    var logLines = (data.logs || []);
    var logHtml = logLines.join('\n').replace(/\[INFO\]/g, '<span class="log-info">[INFO]</span>')
                                     .replace(/\[WARN\]/g, '<span class="log-warn">[WARN]</span>')
                                     .replace(/\[ERROR\]/g, '<span class="log-error">[ERROR]</span>')
                                     .replace(/(✓|sent|ok)/gi, '<span class="log-ok">$1</span>');
    document.getElementById('logsContent').innerHTML = logHtml || 'No log entries yet.';

    /* Chart ticker dropdown */
    if (typeof window._chartSetTickers === 'function') {
      var tickers = [];
      if (data.scans && data.scans[0] && data.scans[0].results) {
        data.scans[0].results.forEach(function(r) { if (r.ticker) tickers.push(r.ticker); });
      }
      window._chartSetTickers(tickers.sort());
    }
  }

  /* ── Cache ── */
  var CACHE_KEY = 'sps_dashboard_v2';
  var CACHE_MAX_AGE_MS = 30 * 60 * 1000;

  function readCache() {
    try {
      var raw = localStorage.getItem(CACHE_KEY);
      if (!raw) return null;
      var obj = JSON.parse(raw);
      if (!obj || !obj.ts || !obj.data) return null;
      if (Date.now() - obj.ts > CACHE_MAX_AGE_MS) return null;
      return obj.data;
    } catch(e) { return null; }
  }

  function writeCache(data) {
    try { localStorage.setItem(CACHE_KEY, JSON.stringify({ ts: Date.now(), data: data })); }
    catch(e) {}
  }

  /* ── Boot ── */
  var cached = readCache();
  if (cached) {
    populateDashboard(cached);
    document.getElementById('lastRefreshedFull').textContent = 'Last refreshed: ' + cached.now + ' (cached — updating…)';
  } else {
    setLoading();
  }

  var autoScanTriggered = sessionStorage.getItem('sps_auto_scan_triggered') === '1';

  function autoTriggerScanIfEmpty(data) {
    if (autoScanTriggered) return;
    var isEmpty = !data || !data.scans || data.scans.length === 0;
    if (!isEmpty) return;
    autoScanTriggered = true;
    sessionStorage.setItem('sps_auto_scan_triggered', '1');
    document.getElementById('lastRefreshedFull').textContent = 'No scans yet — running one now (~20–60s)…';
    fetch(BACKEND_URL + '/api/force_scan', { method: 'POST' })
      .then(function(r) { return r.json().catch(function() { return {}; }); })
      .then(function(res) {
        if (res && res.ok) { location.reload(); }
        else {
          document.getElementById('lastRefreshedFull').textContent = 'Auto-scan failed: ' + ((res && res.error) || 'unknown error');
        }
      })
      .catch(function(err) {
        document.getElementById('lastRefreshedFull').textContent = 'Auto-scan failed — ' + (err.message || 'network error');
      });
  }

  fetch(BACKEND_URL + '/api/data')
    .then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function(data) {
      writeCache(data);
      populateDashboard(data);
      autoTriggerScanIfEmpty(data);
    })
    .catch(function(err) {
      if (cached) {
        document.getElementById('lastRefreshedFull').textContent =
          'Last refreshed: ' + cached.now + ' (cached — backend offline: ' + (err.message || 'network error') + ')';
      } else {
        setError(err.message || 'network error');
      }
    });

  setTimeout(function() { location.reload(); }, 60000);

  /* ── Upload Modal ── */
  (function() {
    var overlay      = document.getElementById('uploadModal');
    var modalInner   = document.getElementById('uploadModalInner');
    var openBtn      = document.getElementById('uploadBtn');
    var uploadPanel  = document.getElementById('uploadPanel');
    var cancelBtn    = document.getElementById('uploadCancel');
    var submitBtn    = document.getElementById('uploadSubmit');
    var fileInput    = document.getElementById('uploadFileInput');
    var dropZone     = document.getElementById('uploadDropZone');
    var fileNameEl   = document.getElementById('uploadFileName');
    var statusEl     = document.getElementById('uploadStatus');
    var browseLink   = document.getElementById('uploadBrowse');
    var mappingPanel  = document.getElementById('mappingPanel');
    var mappingRows   = document.getElementById('mappingRows');
    var mappingStatus = document.getElementById('mappingStatus');
    var mappingCancel = document.getElementById('mappingCancel');
    var mappingSubmit = document.getElementById('mappingSubmit');
    var selectedFile = null;
    var pendingPath  = null;

    function showPanel(name) {
      uploadPanel.style.display  = name === 'upload'  ? '' : 'none';
      mappingPanel.style.display = name === 'mapping' ? '' : 'none';
      if (name === 'mapping') modalInner.classList.add('mapping-mode');
      else modalInner.classList.remove('mapping-mode');
    }

    function resetUploadPanel() {
      selectedFile = null; pendingPath = null;
      fileInput.value = ''; fileNameEl.textContent = '';
      statusEl.textContent = ''; statusEl.className = 'upload-status';
      submitBtn.disabled = true;
    }

    function openModal() {
      resetUploadPanel(); showPanel('upload'); overlay.classList.add('open');
    }

    function closeModal() {
      overlay.classList.remove('open'); resetUploadPanel();
      mappingRows.innerHTML = '';
      mappingStatus.textContent = ''; mappingStatus.className = 'upload-status';
      mappingSubmit.disabled = false; showPanel('upload');
    }

    function setFile(file) {
      if (!file) return;
      var ext = file.name.split('.').pop().toLowerCase();
      if (ext !== 'xlsx' && ext !== 'xls') {
        statusEl.textContent = 'Only .xlsx or .xls files are allowed.';
        statusEl.className = 'upload-status err';
        submitBtn.disabled = true; return;
      }
      selectedFile = file; fileNameEl.textContent = file.name;
      statusEl.textContent = ''; statusEl.className = 'upload-status';
      submitBtn.disabled = false;
    }

    openBtn.addEventListener('click', openModal);
    cancelBtn.addEventListener('click', closeModal);
    browseLink.addEventListener('click', function() { fileInput.click(); });
    dropZone.addEventListener('click', function(e) { if (e.target !== browseLink) fileInput.click(); });
    fileInput.addEventListener('change', function() { setFile(fileInput.files[0]); });
    dropZone.addEventListener('dragover', function(e) { e.preventDefault(); dropZone.classList.add('drag-over'); });
    dropZone.addEventListener('dragleave', function() { dropZone.classList.remove('drag-over'); });
    dropZone.addEventListener('drop', function(e) { e.preventDefault(); dropZone.classList.remove('drag-over'); setFile(e.dataTransfer.files[0]); });
    overlay.addEventListener('click', function(e) { if (e.target === overlay) closeModal(); });

    submitBtn.addEventListener('click', function() {
      if (!selectedFile) return;
      var formData = new FormData(); formData.append('file', selectedFile);
      submitBtn.disabled = true; statusEl.textContent = 'Uploading…'; statusEl.className = 'upload-status';
      fetch(BACKEND_URL + '/upload', { method: 'POST', body: formData })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (!data.ok) { statusEl.textContent = 'Error: ' + (data.error || 'Unknown'); statusEl.className = 'upload-status err'; submitBtn.disabled = false; return; }
          if (data.needs_mapping) { pendingPath = data.path; buildMappingRows(data.unmapped); showPanel('mapping'); }
          else { statusEl.textContent = 'Uploaded! Next scan will use this file.'; statusEl.className = 'upload-status ok'; setTimeout(closeModal, 2000); }
        })
        .catch(function() { statusEl.textContent = 'Upload failed. Is the backend running?'; statusEl.className = 'upload-status err'; submitBtn.disabled = false; });
    });

    function buildMappingRows(unmapped) {
      mappingRows.innerHTML = '';
      unmapped.forEach(function(name) {
        var row = document.createElement('div');
        row.className = 'mapping-row';
        row.innerHTML = '<span class="mapping-name" title="' + esc(name) + '">' + esc(name) + '</span>'
          + '<span class="mapping-arrow">→</span>'
          + '<input class="mapping-input" type="text" data-name="' + esc(name) + '" placeholder="e.g. AAPL" autocomplete="off" spellcheck="false">';
        mappingRows.appendChild(row);
      });
      setTimeout(function() { var f = mappingRows.querySelector('.mapping-input'); if (f) f.focus(); }, 80);
    }

    mappingCancel.addEventListener('click', closeModal);
    mappingSubmit.addEventListener('click', function() {
      var inputs = mappingRows.querySelectorAll('.mapping-input');
      var mappings = {};
      inputs.forEach(function(inp) { var t = inp.value.trim().toUpperCase(); if (t) mappings[inp.dataset.name] = t; });
      mappingStatus.textContent = 'Saving…'; mappingStatus.className = 'upload-status';
      mappingSubmit.disabled = true;
      fetch(BACKEND_URL + '/update_ticker_map', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mappings: mappings, path: pendingPath }) })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (data.ok) { mappingStatus.textContent = (data.added ? data.added + ' ticker(s) saved. ' : '') + 'File ready for next scan.'; mappingStatus.className = 'upload-status ok'; setTimeout(closeModal, 2500); }
          else { mappingStatus.textContent = 'Error: ' + (data.error || 'Unknown'); mappingStatus.className = 'upload-status err'; mappingSubmit.disabled = false; }
        })
        .catch(function() { mappingStatus.textContent = 'Request failed.'; mappingStatus.className = 'upload-status err'; mappingSubmit.disabled = false; });
    });

    mappingRows.addEventListener('keydown', function(e) {
      if (e.key !== 'Enter') return;
      var inputs = Array.from(mappingRows.querySelectorAll('.mapping-input'));
      var idx = inputs.indexOf(document.activeElement);
      if (idx >= 0 && idx < inputs.length - 1) inputs[idx + 1].focus();
      else mappingSubmit.click();
    });
  }());

})();

/* ── Technical Chart ─────────────────────────────────────────────── */
(function () {
  if (typeof LightweightCharts === 'undefined') return;

  var BACKEND = '';
  var tickerSel = document.getElementById('chart-ticker');
  var periodSel = document.getElementById('chart-period');
  var statusEl  = document.getElementById('chart-status');

  var THEME = {
    layout: { background: { color: '#09090f' }, textColor: 'rgba(240,239,250,0.45)' },
    grid:   { vertLines: { color: 'rgba(255,255,255,0.04)' }, horzLines: { color: 'rgba(255,255,255,0.04)' } },
    crosshair: { mode: 1 },
    rightPriceScale: { borderColor: 'rgba(255,255,255,0.06)' },
    timeScale: { borderColor: 'rgba(255,255,255,0.06)', timeVisible: true, secondsVisible: false },
  };

  function makeChart(id, h, extra) {
    var el = document.getElementById(id);
    if (!el) return null;
    return LightweightCharts.createChart(el, Object.assign({}, THEME, { width: el.clientWidth, height: h }, extra || {}));
  }

  var priceChart = makeChart('chart-price', 320);
  var volChart   = makeChart('chart-volume', 72,  { timeScale: { visible: false } });
  var rsiChart   = makeChart('chart-rsi',    90, { timeScale: { visible: false } });
  var macdChart  = makeChart('chart-macd',   90, { timeScale: { visible: false } });
  if (!priceChart) return;

  var upColor   = 'oklch(66% 0.17 145)';
  var downColor = 'oklch(60% 0.20 25)';
  var upColorFallback = '#4ade80';
  var downColorFallback = '#f87171';

  try { var _t = new Option(); _t.style.color = upColor; if (!_t.style.color) throw 0; }
  catch(e) { upColor = upColorFallback; downColor = downColorFallback; }

  var sSeries = priceChart.addCandlestickSeries({
    upColor: upColor, downColor: downColor,
    borderUpColor: upColor, borderDownColor: downColor,
    wickUpColor: upColor, wickDownColor: downColor
  });
  var sSma   = priceChart.addLineSeries({ color: 'oklch(72% 0.16 72)', lineWidth: 1.5, title: 'SMA200', priceLineVisible: false, lastValueVisible: false });
  var sBbUp  = priceChart.addLineSeries({ color: 'oklch(70% 0.16 235)', lineWidth: 1, lineStyle: 1, title: 'BB↑', priceLineVisible: false, lastValueVisible: false });
  var sBbMid = priceChart.addLineSeries({ color: 'oklch(70% 0.16 235)', lineWidth: 1, lineStyle: 2, title: 'BBmid', priceLineVisible: false, lastValueVisible: false });
  var sBbLo  = priceChart.addLineSeries({ color: 'oklch(70% 0.16 235)', lineWidth: 1, lineStyle: 1, title: 'BB↓', priceLineVisible: false, lastValueVisible: false });

  var sVol = volChart ? volChart.addHistogramSeries({ color: '#4ade8060', priceFormat: { type: 'volume' }, priceScaleId: 'vol' }) : null;
  if (volChart && sVol) volChart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.1, bottom: 0 } });

  var sRsi = rsiChart ? rsiChart.addLineSeries({ color: 'oklch(68% 0.16 290)', lineWidth: 2, title: 'RSI', priceLineVisible: false, lastValueVisible: true }) : null;
  if (sRsi) {
    sRsi.createPriceLine({ price: 70, color: downColor, lineWidth: 1, lineStyle: 1, axisLabelVisible: true, title: 'OB' });
    sRsi.createPriceLine({ price: 30, color: upColor,   lineWidth: 1, lineStyle: 1, axisLabelVisible: true, title: 'OS' });
  }

  var sMacd   = macdChart ? macdChart.addLineSeries({ color: 'oklch(70% 0.16 235)', lineWidth: 1.5, title: 'MACD', priceLineVisible: false, lastValueVisible: false }) : null;
  var sSignal = macdChart ? macdChart.addLineSeries({ color: 'oklch(72% 0.16 72)',  lineWidth: 1.5, title: 'Signal', priceLineVisible: false, lastValueVisible: false }) : null;
  var sHist   = macdChart ? macdChart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false }) : null;

  var charts = [priceChart, volChart, rsiChart, macdChart].filter(Boolean);
  var syncing = false;
  charts.forEach(function(c) {
    c.timeScale().subscribeVisibleLogicalRangeChange(function() {
      if (syncing) return;
      syncing = true;
      var r = c.timeScale().getVisibleLogicalRange();
      if (r) charts.forEach(function(o) { if (o !== c) o.timeScale().setVisibleLogicalRange(r); });
      syncing = false;
    });
  });

  window.addEventListener('resize', function() {
    var el = document.getElementById('chart-price');
    if (!el) return;
    var w = el.clientWidth;
    [320, 72, 90, 90].forEach(function(h, i) { if (charts[i]) charts[i].resize(w, h); });
  });

  function noNull(arr) {
    return arr.filter(function(d) { return d.value !== null && d.value !== undefined; });
  }

  function loadChart(ticker, period) {
    if (!ticker) return;
    if (statusEl) statusEl.textContent = 'Loading ' + ticker + '…';
    fetch(BACKEND + '/api/ohlcv?ticker=' + encodeURIComponent(ticker) + '&period=' + encodeURIComponent(period))
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (!d.ok) { if (statusEl) statusEl.textContent = 'Error: ' + (d.error || 'failed'); return; }
        sSeries.setData(d.candles.filter(function(c) { return c.open !== null; }));
        sSma.setData(noNull(d.sma200));
        sBbUp.setData(noNull(d.bb_upper));
        sBbMid.setData(noNull(d.bb_mid));
        sBbLo.setData(noNull(d.bb_lower));
        if (sVol) sVol.setData(d.candles.filter(function(c) { return c.volume > 0; }).map(function(c) {
          return { time: c.time, value: c.volume, color: c.close >= c.open ? '#4ade8050' : '#f8717150' };
        }));
        if (sRsi) sRsi.setData(noNull(d.rsi14));
        if (sMacd) sMacd.setData(noNull(d.macd));
        if (sSignal) sSignal.setData(noNull(d.macd_signal));
        if (sHist) sHist.setData(noNull(d.macd_hist).map(function(h) {
          return { time: h.time, value: h.value, color: h.value >= 0 ? '#4ade8070' : '#f8717170' };
        }));
        syncing = true;
        charts.forEach(function(c) { c.timeScale().fitContent(); });
        syncing = false;
        if (statusEl) statusEl.textContent = '';
      })
      .catch(function() { if (statusEl) statusEl.textContent = 'Network error loading chart.'; });
  }

  window._chartSetTickers = function(tickers) {
    var current = tickerSel.value;
    tickerSel.innerHTML = '';
    tickers.forEach(function(t) {
      var opt = document.createElement('option'); opt.value = t; opt.textContent = t;
      if (t === current) opt.selected = true;
      tickerSel.appendChild(opt);
    });
    if (!current && tickers.length) loadChart(tickers[0], periodSel.value);
  };

  tickerSel.addEventListener('change', function() { loadChart(tickerSel.value, periodSel.value); });
  periodSel.addEventListener('change', function() { loadChart(tickerSel.value, periodSel.value); });

  document.addEventListener('click', function(e) {
    var row = e.target.closest('tr[data-ticker]');
    if (!row) return;
    var t = row.dataset.ticker; if (!t) return;
    var found = false;
    for (var i = 0; i < tickerSel.options.length; i++) {
      if (tickerSel.options[i].value === t) { tickerSel.selectedIndex = i; found = true; break; }
    }
    if (!found) {
      var opt = document.createElement('option'); opt.value = t; opt.textContent = t;
      tickerSel.appendChild(opt); tickerSel.value = t;
    }
    var cc = document.getElementById('chart-card');
    if (cc) cc.scrollTop = 0;
    window.scrollTo({ top: cc.getBoundingClientRect().top + window.scrollY - 70, behavior: 'smooth' });
    loadChart(t, periodSel.value);
  });
}());
</script>
</body>
</html>

"""

def _start_scheduler() -> None:
    """Start APScheduler to run portfolio scans automatically during market hours."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from portfolio_monitor import run_once
    except ImportError as e:
        logging.getLogger("dashboard").warning("Scheduler not available: %s", e)
        return

    scheduler = BackgroundScheduler(timezone="America/New_York")

    def _scan_and_push():
        _github_pull()
        run_once()
        _github_push()

    # Run every 2 hours Mon–Fri 09:30–16:00 ET. run_once() self-filters if market is closed.
    scheduler.add_job(
        _scan_and_push,
        CronTrigger(day_of_week="mon-fri", hour="9,11,13,15", minute="30", timezone="America/New_York"),
        id="portfolio_scan",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # One-shot startup scan: runs ~5s after the service comes up so the dashboard
    # has fresh data on first load (Render spins down free services when idle).
    # Only fires if no scan has run in the last 25 minutes — prevents duplicate
    # alerts when Render restarts the service on wake-up.
    # force=True so it still populates outside market hours.
    from datetime import datetime as _dt, timedelta as _td

    def _startup_scan_if_stale():
        # Always pull latest scan history from GitHub first — ensures cooldowns and history
        # are current even when the disk has stale data from a previous session.
        _github_pull()
        history = _load_json(SCAN_HISTORY_FILE, [])
        if history:
            try:
                from datetime import timezone as _tz
                last_ts = _dt.fromisoformat(history[0]["timestamp"])
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=_tz.utc)
                age_minutes = (_dt.now(tz=_tz.utc) - last_ts).total_seconds() / 60
                if age_minutes < 25:
                    logging.getLogger("dashboard").info(
                        "Startup scan skipped — last scan was %.1f min ago.", age_minutes
                    )
                    return
            except Exception:
                pass
        # notify=False: populate dashboard data without re-sending WhatsApp alerts.
        # This prevents duplicate alerts when Render restarts the service and the
        # cooldown state file (signals_state.json) has been lost on the ephemeral FS.
        run_once(force=True, notify=False)
        _github_push()

    scheduler.add_job(
        _startup_scan_if_stale,
        trigger="date",
        run_date=_dt.now() + _td(seconds=5),
        id="portfolio_scan_startup",
        replace_existing=True,
        misfire_grace_time=120,
    )

    scheduler.start()
    logging.getLogger("dashboard").info(
        "Scheduler started — initial scan in ~5s, then every 2h Mon–Fri 09:30–16:00 ET"
    )


if __name__ == "__main__":
    _start_scheduler()
    print("Dashboard running at http://localhost:5050")
    print("Landing page:  https://monumental-otter-86ec71.netlify.app")
    app.run(host="0.0.0.0", port=5050, debug=False, use_reloader=False)
else:
    # Running under gunicorn — start scheduler once (gunicorn --workers 1)
    _start_scheduler()
