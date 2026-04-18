"""
Portfolio Monitor — Web Dashboard
-----------------------------------
A lightweight Flask app that shows scan history, ticker evaluations,
alert history, and cooldown status from portfolio_monitor.py.

Run:  python dashboard.py
Open: http://localhost:5050
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

app = Flask(__name__)


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
    state = _load_json(STATE_FILE, {})
    if not isinstance(state, dict):
        return {}
    now = datetime.now(tz=timezone.utc)
    result = {}
    for ticker, sides in state.items():
        if not isinstance(sides, dict):
            continue
        for side, iso_str in sides.items():
            try:
                last = datetime.fromisoformat(iso_str)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                expires = last + timedelta(hours=COOLDOWN_HOURS)
                remaining = expires - now
                if remaining.total_seconds() > 0:
                    hours_left = remaining.total_seconds() / 3600
                    result.setdefault(ticker, {})[side] = {
                        "alerted_at": last.strftime("%Y-%m-%d %H:%M UTC"),
                        "expires": expires.strftime("%Y-%m-%d %H:%M UTC"),
                        "hours_left": round(hours_left, 1),
                    }
            except Exception:
                continue
    return result


def _get_recent_logs(n: int = 80) -> list[str]:
    if not LOG_FILE.exists():
        return []
    try:
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return lines[-n:]
    except Exception:
        return []


@app.route("/")
def index():
    scans = _load_json(SCAN_HISTORY_FILE, [])
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
        now=datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
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
        run_once(force=True)
    except Exception as exc:
        response = jsonify({"ok": False, "error": str(exc)[:200]})
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response, 500

    response = jsonify({"ok": True, "message": "Scan completed."})
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.route("/api/data")
def api_data():
    """JSON endpoint for the static Netlify dashboard to fetch live data."""
    scans = _load_json(SCAN_HISTORY_FILE, [])
    cooldowns = _get_cooldowns()
    logs = _get_recent_logs(80)

    all_alerts = []
    for scan in scans:
        for alert in scan.get("alerts_sent", []):
            all_alerts.append(alert)

    response = jsonify({
        "scans": scans[:20],
        "cooldowns": cooldowns,
        "all_alerts": all_alerts[:30],
        "logs": [l.rstrip("\n") for l in logs],
        "now": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    })
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
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio Monitor Dashboard</title>
<style>
:root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #222632;
    --border: #2d3348;
    --text: #e2e4eb;
    --text-dim: #8b8fa3;
    --accent: #6c8cff;
    --green: #34d399;
    --red: #f87171;
    --yellow: #fbbf24;
    --orange: #fb923c;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    padding: 1.5rem;
}
h1 {
    font-size: 1.5rem;
    font-weight: 600;
    margin-bottom: 0.25rem;
}
.subtitle { color: var(--text-dim); font-size: 0.85rem; margin-bottom: 1.5rem; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.25rem; margin-bottom: 1.25rem; }
@media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
.card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1.25rem;
}
.card-title {
    font-size: 0.9rem;
    font-weight: 600;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 1rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
}
.card-full { grid-column: 1 / -1; }
table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
th {
    text-align: left;
    color: var(--text-dim);
    font-weight: 500;
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
}
td {
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
}
tr:last-child td { border-bottom: none; }
tr:hover td { background: var(--surface2); }
.badge {
    display: inline-block;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
}
.badge-buy { background: rgba(52,211,153,0.15); color: var(--green); }
.badge-sell { background: rgba(248,113,113,0.15); color: var(--red); }
.badge-hold { background: rgba(251,191,36,0.15); color: var(--yellow); }
.badge-ok { background: rgba(52,211,153,0.15); color: var(--green); }
.badge-fail { background: rgba(248,113,113,0.15); color: var(--red); }
.badge-forced { background: rgba(108,140,255,0.15); color: var(--accent); }
.score {
    font-weight: 700;
    font-variant-numeric: tabular-nums;
}
.score-high { color: var(--green); }
.score-mid { color: var(--yellow); }
.score-low { color: var(--text-dim); }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 1rem; }
.stat-box { text-align: center; }
.stat-value { font-size: 1.75rem; font-weight: 700; color: var(--accent); }
.stat-label { font-size: 0.75rem; color: var(--text-dim); margin-top: 0.15rem; }
.cooldown-bar {
    background: var(--surface2);
    border-radius: 4px;
    height: 6px;
    overflow: hidden;
    margin-top: 0.25rem;
}
.cooldown-fill {
    height: 100%;
    border-radius: 4px;
    background: var(--orange);
    transition: width 0.3s;
}
pre.logs {
    background: var(--surface2);
    border-radius: 6px;
    padding: 1rem;
    font-size: 0.72rem;
    font-family: 'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace;
    overflow-x: auto;
    max-height: 400px;
    overflow-y: auto;
    line-height: 1.6;
    color: var(--text-dim);
    white-space: pre;
}
.empty { color: var(--text-dim); font-style: italic; padding: 1rem 0; text-align: center; }
.refresh-btn {
    position: fixed;
    bottom: 1.5rem;
    right: 1.5rem;
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 50%;
    width: 48px;
    height: 48px;
    font-size: 1.25rem;
    cursor: pointer;
    box-shadow: 0 4px 12px rgba(0,0,0,0.4);
    transition: transform 0.15s;
}
.refresh-btn:hover { transform: scale(1.1); }

/* ── Header bar ── */
.header-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-bottom: 0.25rem;
}
.header-left {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-wrap: wrap;
}
.greeting {
    font-size: 0.95rem;
    color: var(--accent);
    font-weight: 500;
}
.edit-name-btn {
    background: none;
    border: 1px solid var(--border);
    color: var(--text-dim);
    font-size: 0.72rem;
    padding: 0.2rem 0.5rem;
    border-radius: 4px;
    cursor: pointer;
    transition: color 0.15s, border-color 0.15s;
}
.edit-name-btn:hover {
    color: var(--accent);
    border-color: var(--accent);
}
.header-right a {
    color: var(--text-dim);
    font-size: 0.75rem;
    text-decoration: none;
    border: 1px solid var(--border);
    padding: 0.2rem 0.6rem;
    border-radius: 4px;
    transition: color 0.15s, border-color 0.15s;
}
.header-right a:hover {
    color: var(--accent);
    border-color: var(--accent);
}

/* ── Name modal ── */
.name-modal-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.55);
    z-index: 1000;
    justify-content: center;
    align-items: center;
}
.name-modal-overlay.open { display: flex; }
.name-modal {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1.5rem;
    width: 320px;
    max-width: 90vw;
}
.name-modal h3 {
    font-size: 1rem;
    margin-bottom: 0.75rem;
    color: var(--text);
}
.name-modal input {
    width: 100%;
    padding: 0.5rem 0.75rem;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    font-size: 0.85rem;
    font-family: inherit;
    outline: none;
    margin-bottom: 0.75rem;
}
.name-modal input:focus { border-color: var(--accent); }
.name-modal-btns {
    display: flex;
    gap: 0.5rem;
    justify-content: flex-end;
}
.name-modal-btns button {
    padding: 0.35rem 0.85rem;
    border-radius: 5px;
    border: none;
    font-size: 0.8rem;
    cursor: pointer;
    font-family: inherit;
}
.btn-save { background: var(--accent); color: #fff; }
.btn-cancel { background: var(--surface2); color: var(--text-dim); }

/* ── Upload modal ── */
.upload-modal-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.55);
    z-index: 1000;
    justify-content: center;
    align-items: center;
}
.upload-modal-overlay.open { display: flex; }
.upload-modal {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1.5rem;
    width: 380px;
    max-width: 90vw;
}
.upload-modal h3 { font-size: 1rem; margin-bottom: 0.5rem; color: var(--text); }
.upload-modal p { font-size: 0.8rem; color: var(--text-dim); margin-bottom: 1rem; line-height: 1.5; }
.upload-drop-zone {
    border: 2px dashed var(--border);
    border-radius: 8px;
    padding: 1.5rem;
    text-align: center;
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
    margin-bottom: 0.75rem;
}
.upload-drop-zone:hover, .upload-drop-zone.drag-over {
    border-color: var(--accent);
    background: rgba(108,140,255,0.05);
}
.upload-drop-zone input[type="file"] { display: none; }
.upload-drop-label { font-size: 0.82rem; color: var(--text-dim); }
.upload-drop-label span { color: var(--accent); text-decoration: underline; cursor: pointer; }
.upload-file-name { font-size: 0.78rem; color: var(--green); margin-top: 0.4rem; min-height: 1rem; }
.upload-status { font-size: 0.78rem; margin-top: 0.5rem; min-height: 1rem; }
.upload-status.ok { color: var(--green); }
.upload-status.err { color: var(--red); }
.upload-modal-btns { display: flex; gap: 0.5rem; justify-content: flex-end; margin-top: 0.75rem; }
.upload-modal-btns button {
    padding: 0.35rem 0.85rem;
    border-radius: 5px;
    border: none;
    font-size: 0.8rem;
    cursor: pointer;
    font-family: inherit;
}
.btn-upload { background: var(--accent); color: #fff; }
.btn-upload:disabled { opacity: 0.5; cursor: not-allowed; }

/* ── Ticker mapping wizard (shown inside upload modal) ── */
.upload-modal.mapping-mode { width: 480px; }
.mapping-intro {
    font-size: 0.8rem;
    color: var(--text-dim);
    margin-bottom: 1rem;
    line-height: 1.55;
}
.mapping-intro strong { color: var(--yellow); }
.mapping-scroll {
    max-height: 280px;
    overflow-y: auto;
    margin-bottom: 0.75rem;
    padding-right: 0.25rem;
}
.mapping-row {
    display: grid;
    grid-template-columns: 1fr auto 120px;
    align-items: center;
    gap: 0.5rem;
    padding: 0.45rem 0;
    border-bottom: 1px solid var(--border);
}
.mapping-row:last-child { border-bottom: none; }
.mapping-name {
    font-size: 0.78rem;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.mapping-arrow { color: var(--text-dim); font-size: 0.8rem; }
.mapping-input {
    padding: 0.3rem 0.5rem;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 5px;
    color: var(--text);
    font-size: 0.82rem;
    font-family: 'JetBrains Mono', 'Cascadia Code', monospace;
    letter-spacing: 0.04em;
    outline: none;
    width: 100%;
    text-transform: uppercase;
}
.mapping-input:focus { border-color: var(--accent); }
.mapping-input::placeholder { text-transform: none; color: var(--text-dim); opacity: 0.6; }
.mapping-skip-note {
    font-size: 0.72rem;
    color: var(--text-dim);
    margin-bottom: 0.5rem;
    font-style: italic;
}
</style>
</head>
<body>

<!-- Upload Excel modal (two panels: file picker → mapping wizard) -->
<div class="upload-modal-overlay" id="uploadModal">
    <div class="upload-modal" id="uploadModalInner">

        <!-- Panel 1: file picker -->
        <div id="uploadPanel">
            <h3>Upload Portfolio Excel</h3>
            <p>Upload your Excel export from Excellence brokerage.<br>
               The file will be saved and used for the next scan automatically.</p>
            <div class="upload-drop-zone" id="uploadDropZone">
                <input type="file" id="uploadFileInput" accept=".xlsx,.xls">
                <div class="upload-drop-label">
                    Drag &amp; drop your file here, or <span id="uploadBrowse">browse</span>
                </div>
                <div class="upload-file-name" id="uploadFileName"></div>
            </div>
            <div class="upload-status" id="uploadStatus"></div>
            <div class="upload-modal-btns">
                <button class="btn-cancel" id="uploadCancel">Cancel</button>
                <button class="btn-upload" id="uploadSubmit" disabled>Upload</button>
            </div>
        </div>

        <!-- Panel 2: ticker mapping wizard (hidden until unmapped stocks found) -->
        <div id="mappingPanel" style="display:none">
            <h3>Map New Stocks</h3>
            <p class="mapping-intro">
                These stocks from your Excel have no Yahoo Finance ticker yet.<br>
                Enter the ticker symbol for each one. <strong>Leave blank to skip</strong> a stock
                (it won't be monitored until you add it later).
            </p>
            <div class="mapping-scroll" id="mappingRows"></div>
            <p class="mapping-skip-note">Tip: find tickers at finance.yahoo.com — e.g. AAPL, NVDA, CSPX.L</p>
            <div class="upload-status" id="mappingStatus"></div>
            <div class="upload-modal-btns">
                <button class="btn-cancel" id="mappingCancel">Cancel</button>
                <button class="btn-upload" id="mappingSubmit">Save &amp; Continue</button>
            </div>
        </div>

    </div>
</div>

<!-- Name edit modal -->
<div class="name-modal-overlay" id="nameModal">
    <div class="name-modal">
        <h3>Set your name</h3>
        <input type="text" id="nameInput" placeholder="Enter your name…" maxlength="40" autocomplete="off">
        <div class="name-modal-btns">
            <button class="btn-cancel" id="nameCancel">Cancel</button>
            <button class="btn-save" id="nameSave">Save</button>
        </div>
    </div>
</div>

<div class="header-bar">
    <div class="header-left">
        <h1>Portfolio Monitor</h1>
        <span class="greeting" id="greeting"></span>
        <button class="edit-name-btn" id="editNameBtn" title="Edit name">&#9998; Edit Name</button>
    </div>
    <div class="header-right" style="display:flex;gap:0.5rem;align-items:center;">
        <button class="edit-name-btn" id="uploadBtn" title="Upload Excel file">&#8679; Upload Excel</button>
        <a href="https://monumental-otter-86ec71.netlify.app" target="_blank" rel="noopener">Landing Page ↗</a>
    </div>
</div>
<p class="subtitle">Last refreshed: {{ now }}</p>

<!-- STATS -->
<div class="card" style="margin-bottom: 1.25rem;">
    <div class="stat-grid">
        <div class="stat-box">
            <div class="stat-value">{{ scans | length }}</div>
            <div class="stat-label">Total Scans</div>
        </div>
        <div class="stat-box">
            <div class="stat-value">{{ all_alerts | length }}</div>
            <div class="stat-label">Alerts Sent</div>
        </div>
        <div class="stat-box">
            <div class="stat-value">{{ cooldowns | length }}</div>
            <div class="stat-label">Active Cooldowns</div>
        </div>
        <div class="stat-box">
            <div class="stat-value">{% if scans %}{{ scans[0].results | length }}{% else %}0{% endif %}</div>
            <div class="stat-label">Tickers Tracked</div>
        </div>
    </div>
</div>

<div class="grid">

<!-- RECENT SCANS -->
<div class="card">
    <div class="card-title">Recent Scans</div>
    {% if scans %}
    <table>
    <thead><tr><th>Time</th><th>Market</th><th>Tickers</th><th>Alerts</th><th>Errors</th></tr></thead>
    <tbody>
    {% for s in scans[:15] %}
    <tr>
        <td>{{ s.timestamp[:16].replace('T', ' ') }}</td>
        <td>
            {{ s.market_status }}
            {% if s.forced %}<span class="badge badge-forced">FORCED</span>{% endif %}
        </td>
        <td>{{ s.tickers_count }}</td>
        <td>{{ s.alerts_sent | length }}</td>
        <td>{% if s.errors %}<span style="color:var(--red)">{{ s.errors | length }}</span>{% else %}0{% endif %}</td>
    </tr>
    {% endfor %}
    </tbody>
    </table>
    {% else %}
    <div class="empty">No scans recorded yet. Run portfolio_monitor.py to generate data.</div>
    {% endif %}
</div>

<!-- ACTIVE COOLDOWNS -->
<div class="card">
    <div class="card-title">Active Cooldowns</div>
    {% if cooldowns %}
    <table>
    <thead><tr><th>Ticker</th><th>Side</th><th>Alerted At</th><th>Remaining</th></tr></thead>
    <tbody>
    {% for ticker, sides in cooldowns.items() %}
    {% for side, info in sides.items() %}
    <tr>
        <td><strong>{{ ticker }}</strong></td>
        <td><span class="badge {% if side == 'BUY' %}badge-buy{% else %}badge-sell{% endif %}">{{ side }}</span></td>
        <td>{{ info.alerted_at }}</td>
        <td>
            {{ info.hours_left }}h left
            <div class="cooldown-bar"><div class="cooldown-fill" style="width: {{ (info.hours_left / 48 * 100) | round }}%"></div></div>
        </td>
    </tr>
    {% endfor %}
    {% endfor %}
    </tbody>
    </table>
    {% else %}
    <div class="empty">No active cooldowns.</div>
    {% endif %}
</div>

</div>

<!-- LATEST SCAN RESULTS -->
<div class="card" style="margin-bottom: 1.25rem;">
    <div class="card-title">Latest Scan Results{% if scans %} — {{ scans[0].timestamp[:16].replace('T', ' ') }} UTC{% endif %}</div>
    {% if scans and scans[0].results %}
    <table>
    <thead><tr><th>Ticker</th><th>Name</th><th>Close</th><th>RSI</th><th>vs SMA200</th><th>Buy</th><th>Sell</th></tr></thead>
    <tbody>
    {% for r in scans[0].results %}
    <tr>
        <td><strong>{{ r.ticker }}</strong></td>
        <td>{{ r.name }}</td>
        <td>${{ "%.2f" | format(r.close) }}</td>
        <td>
            <span {% if r.rsi < 35 %}style="color:var(--green)"{% elif r.rsi > 70 %}style="color:var(--red)"{% endif %}>
                {{ "%.1f" | format(r.rsi) }}
            </span>
        </td>
        <td>
            <span {% if r.sma200_delta_pct > 0 %}style="color:var(--green)"{% else %}style="color:var(--red)"{% endif %}>
                {{ "%+.1f" | format(r.sma200_delta_pct) }}%
            </span>
        </td>
        <td>
            <span class="score {% if r.buy_score >= 3 %}score-high{% elif r.buy_score >= 2 %}score-mid{% else %}score-low{% endif %}">
                {{ r.buy_score }}/4
            </span>
        </td>
        <td>
            <span class="score {% if r.sell_score >= 3 %}score-high{% elif r.sell_score >= 2 %}score-mid{% else %}score-low{% endif %}">
                {{ r.sell_score }}/4
            </span>
        </td>
    </tr>
    {% endfor %}
    </tbody>
    </table>
    {% else %}
    <div class="empty">No scan results yet.</div>
    {% endif %}
</div>

<!-- ALERT HISTORY -->
<div class="card" style="margin-bottom: 1.25rem;">
    <div class="card-title">Alert History</div>
    {% if all_alerts %}
    <table>
    <thead><tr><th>Time</th><th>Ticker</th><th>Side</th><th>Score</th><th>Indicators</th><th>AI</th><th>Sent</th></tr></thead>
    <tbody>
    {% for a in all_alerts %}
    <tr>
        <td>{{ a.timestamp[:16].replace('T', ' ') }}</td>
        <td><strong>{{ a.ticker }}</strong></td>
        <td><span class="badge {% if a.side == 'BUY' %}badge-buy{% else %}badge-sell{% endif %}">{{ a.side }}</span></td>
        <td class="score">{{ a.score }}/4</td>
        <td>{{ a.indicators }}</td>
        <td>
            {% if a.ai_sentiment %}
            <span class="badge {% if a.ai_sentiment == 'BUY' %}badge-buy{% elif a.ai_sentiment == 'SELL' %}badge-sell{% else %}badge-hold{% endif %}">
                {{ a.ai_sentiment }}
            </span>
            {% else %}
            <span style="color:var(--text-dim)">—</span>
            {% endif %}
        </td>
        <td>
            {% if a.whatsapp_sent %}
            <span class="badge badge-ok">OK</span>
            {% else %}
            <span class="badge badge-fail">FAIL</span>
            {% endif %}
        </td>
    </tr>
    {% endfor %}
    </tbody>
    </table>
    {% else %}
    <div class="empty">No alerts sent yet.</div>
    {% endif %}
</div>

<!-- RECENT LOGS -->
<div class="card">
    <div class="card-title">Recent Logs</div>
    {% if logs %}
    <pre class="logs">{% for line in logs %}{{ line }}{% endfor %}</pre>
    {% else %}
    <div class="empty">No log entries yet.</div>
    {% endif %}
</div>

<div style="margin-top:2rem;padding:1.25rem;background:var(--surface);border:1px solid var(--border);border-radius:8px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.5rem;">
    <span style="font-size:0.7rem;color:var(--text-dim);">Built with Claude Code</span>
    <a href="https://monumental-otter-86ec71.netlify.app" target="_blank" rel="noopener" style="font-size:0.7rem;color:var(--accent);text-decoration:none;">Landing Page &nearr;</a>
</div>
<p style="text-align:center;font-size:0.8rem;color:var(--text-dim);margin-top:0.75rem;margin-bottom:1rem;">&copy; 2026 Ariel Guralnick &mdash; All rights reserved. For educational purposes only. Not financial advice.</p>

<button class="refresh-btn" onclick="location.reload()" title="Refresh">&#x21bb;</button>

<script>
(function() {
  const KEY = 'portfolio_user_name';
  const greetingEl = document.getElementById('greeting');
  const editBtn    = document.getElementById('editNameBtn');
  const modal      = document.getElementById('nameModal');
  const nameInput  = document.getElementById('nameInput');
  const saveBtn    = document.getElementById('nameSave');
  const cancelBtn  = document.getElementById('nameCancel');

  function updateGreeting() {
    const name = localStorage.getItem(KEY);
    if (name) {
      greetingEl.textContent = 'Welcome back, ' + name;
      editBtn.innerHTML = '&#9998;';
      editBtn.title = 'Change name';
    } else {
      greetingEl.textContent = '';
      editBtn.innerHTML = '&#9998; Edit Name';
      editBtn.title = 'Set your name';
    }
  }

  function openModal() {
    const current = localStorage.getItem(KEY) || '';
    nameInput.value = current;
    modal.classList.add('open');
    setTimeout(() => nameInput.focus(), 50);
  }

  function closeModal() { modal.classList.remove('open'); }

  function saveName() {
    const val = nameInput.value.trim();
    if (val) {
      localStorage.setItem(KEY, val);
    } else {
      localStorage.removeItem(KEY);
    }
    closeModal();
    updateGreeting();
  }

  editBtn.addEventListener('click', openModal);
  cancelBtn.addEventListener('click', closeModal);
  saveBtn.addEventListener('click', saveName);
  nameInput.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') saveName();
    if (e.key === 'Escape') closeModal();
  });
  modal.addEventListener('click', function(e) {
    if (e.target === modal) closeModal();
  });

  updateGreeting();

  // Inject user name into log lines displayed in RECENT LOGS
  const name = localStorage.getItem(KEY);
  if (name) {
    const logPre = document.querySelector('pre.logs');
    if (logPre) {
      const text = logPre.textContent;
      // Prepend a synthetic log line showing who is viewing
      const ts = new Date().toISOString().slice(0, 19).replace('T', ' ');
      const viewerLine = '[INFO] User ' + name + ' initiated scan view at ' + ts + '\n';
      logPre.textContent = viewerLine + text;
    }
  }

  // Auto-refresh every 60 seconds
  setTimeout(function() { location.reload(); }, 60000);
})();

// ── Upload Excel modal (two-panel: file picker → ticker mapping wizard) ──
(function() {
  const overlay       = document.getElementById('uploadModal');
  const modalInner    = document.getElementById('uploadModalInner');
  const openBtn       = document.getElementById('uploadBtn');

  // Panel 1 elements
  const uploadPanel   = document.getElementById('uploadPanel');
  const cancelBtn     = document.getElementById('uploadCancel');
  const submitBtn     = document.getElementById('uploadSubmit');
  const fileInput     = document.getElementById('uploadFileInput');
  const dropZone      = document.getElementById('uploadDropZone');
  const fileNameEl    = document.getElementById('uploadFileName');
  const statusEl      = document.getElementById('uploadStatus');
  const browseLink    = document.getElementById('uploadBrowse');

  // Panel 2 elements
  const mappingPanel  = document.getElementById('mappingPanel');
  const mappingRows   = document.getElementById('mappingRows');
  const mappingStatus = document.getElementById('mappingStatus');
  const mappingCancel = document.getElementById('mappingCancel');
  const mappingSubmit = document.getElementById('mappingSubmit');

  let selectedFile = null;
  let pendingPath  = null;   // path returned by /upload when needs_mapping=true

  // ── helpers ──────────────────────────────────────────────────────────────

  function escHtml(s) {
    return String(s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;')
      .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function showPanel(name) {
    uploadPanel.style.display  = (name === 'upload')  ? '' : 'none';
    mappingPanel.style.display = (name === 'mapping') ? '' : 'none';
    if (name === 'mapping') {
      modalInner.classList.add('mapping-mode');
    } else {
      modalInner.classList.remove('mapping-mode');
    }
  }

  function resetUploadPanel() {
    selectedFile = null;
    pendingPath  = null;
    fileInput.value = '';
    fileNameEl.textContent = '';
    statusEl.textContent = '';
    statusEl.className = 'upload-status';
    submitBtn.disabled = true;
  }

  function openModal() {
    resetUploadPanel();
    showPanel('upload');
    overlay.classList.add('open');
  }

  function closeModal() {
    overlay.classList.remove('open');
    // Reset both panels so the modal is clean next time
    resetUploadPanel();
    mappingRows.innerHTML = '';
    mappingStatus.textContent = '';
    mappingStatus.className = 'upload-status';
    mappingSubmit.disabled = false;
    showPanel('upload');
  }

  // ── Panel 1: file picking ────────────────────────────────────────────────

  function setFile(file) {
    if (!file) return;
    const ext = file.name.split('.').pop().toLowerCase();
    if (ext !== 'xlsx' && ext !== 'xls') {
      statusEl.textContent = 'Only .xlsx or .xls files are allowed.';
      statusEl.className = 'upload-status err';
      submitBtn.disabled = true;
      return;
    }
    selectedFile = file;
    fileNameEl.textContent = file.name;
    statusEl.textContent = '';
    statusEl.className = 'upload-status';
    submitBtn.disabled = false;
  }

  openBtn.addEventListener('click', openModal);
  cancelBtn.addEventListener('click', closeModal);
  browseLink.addEventListener('click', function() { fileInput.click(); });
  dropZone.addEventListener('click', function(e) {
    if (e.target !== browseLink) fileInput.click();
  });
  fileInput.addEventListener('change', function() { setFile(fileInput.files[0]); });

  dropZone.addEventListener('dragover', function(e) {
    e.preventDefault(); dropZone.classList.add('drag-over');
  });
  dropZone.addEventListener('dragleave', function() {
    dropZone.classList.remove('drag-over');
  });
  dropZone.addEventListener('drop', function(e) {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
    setFile(e.dataTransfer.files[0]);
  });

  overlay.addEventListener('click', function(e) {
    if (e.target === overlay) closeModal();
  });

  submitBtn.addEventListener('click', function() {
    if (!selectedFile) return;
    const formData = new FormData();
    formData.append('file', selectedFile);
    submitBtn.disabled = true;
    statusEl.textContent = 'Uploading and reading portfolio…';
    statusEl.className = 'upload-status';

    fetch('/upload', { method: 'POST', body: formData })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (!data.ok) {
          statusEl.textContent = 'Error: ' + (data.error || 'Unknown error');
          statusEl.className = 'upload-status err';
          submitBtn.disabled = false;
          return;
        }
        if (data.needs_mapping) {
          // Transition to the wizard panel
          pendingPath = data.path;
          buildMappingRows(data.unmapped);
          showPanel('mapping');
        } else {
          statusEl.textContent = 'Uploaded successfully! The next scan will use this file.';
          statusEl.className = 'upload-status ok';
          setTimeout(closeModal, 2000);
        }
      })
      .catch(function() {
        statusEl.textContent = 'Upload failed. Is the server running?';
        statusEl.className = 'upload-status err';
        submitBtn.disabled = false;
      });
  });

  // ── Panel 2: mapping wizard ───────────────────────────────────────────────

  function buildMappingRows(unmapped) {
    mappingRows.innerHTML = '';
    unmapped.forEach(function(name) {
      const row = document.createElement('div');
      row.className = 'mapping-row';
      row.innerHTML =
        '<span class="mapping-name" title="' + escHtml(name) + '">' + escHtml(name) + '</span>' +
        '<span class="mapping-arrow">&rarr;</span>' +
        '<input class="mapping-input" type="text" data-name="' + escHtml(name) + '" ' +
               'placeholder="e.g. AAPL" autocomplete="off" spellcheck="false">';
      mappingRows.appendChild(row);
    });
    // Focus first input after short delay
    setTimeout(function() {
      var first = mappingRows.querySelector('.mapping-input');
      if (first) first.focus();
    }, 80);
  }

  mappingCancel.addEventListener('click', closeModal);

  mappingSubmit.addEventListener('click', function() {
    var inputs = mappingRows.querySelectorAll('.mapping-input');
    var mappings = {};
    inputs.forEach(function(inp) {
      var ticker = inp.value.trim().toUpperCase();
      if (ticker) mappings[inp.dataset.name] = ticker;
    });

    mappingStatus.textContent = 'Saving ticker mappings…';
    mappingStatus.className = 'upload-status';
    mappingSubmit.disabled = true;

    fetch('/update_ticker_map', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mappings: mappings, path: pendingPath })
    })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.ok) {
          var added = data.added || 0;
          var skipped = Object.keys(mappings).length === 0
            ? ' All stocks skipped — add tickers later via config.yaml.'
            : '';
          mappingStatus.textContent =
            (added ? added + ' ticker(s) saved. ' : '') +
            'File is ready for the next scan.' + skipped;
          mappingStatus.className = 'upload-status ok';
          setTimeout(closeModal, 2500);
        } else {
          mappingStatus.textContent = 'Error: ' + (data.error || 'Unknown error');
          mappingStatus.className = 'upload-status err';
          mappingSubmit.disabled = false;
        }
      })
      .catch(function() {
        mappingStatus.textContent = 'Request failed. Is the server running?';
        mappingStatus.className = 'upload-status err';
        mappingSubmit.disabled = false;
      });
  });

  // Tab between mapping inputs with Enter key
  mappingRows.addEventListener('keydown', function(e) {
    if (e.key !== 'Enter') return;
    var inputs = Array.from(mappingRows.querySelectorAll('.mapping-input'));
    var idx = inputs.indexOf(document.activeElement);
    if (idx >= 0 && idx < inputs.length - 1) {
      inputs[idx + 1].focus();
    } else {
      mappingSubmit.click();
    }
  });
})();
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

    # Run every 30 min Mon–Fri 09:30–16:00 ET. run_once() self-filters if market is closed.
    scheduler.add_job(
        run_once,
        CronTrigger(day_of_week="mon-fri", hour="9-15", minute="0,30", timezone="America/New_York"),
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
        "Scheduler started — initial scan in ~5s, then every 30 min Mon–Fri 09:30–16:00 ET"
    )


if __name__ == "__main__":
    _start_scheduler()
    print("Dashboard running at http://localhost:5050")
    print("Landing page:  https://monumental-otter-86ec71.netlify.app")
    app.run(host="0.0.0.0", port=5050, debug=False, use_reloader=False)
else:
    # Running under gunicorn — start scheduler once (gunicorn --workers 1)
    _start_scheduler()
