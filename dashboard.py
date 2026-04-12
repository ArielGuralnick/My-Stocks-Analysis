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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, render_template_string

BASE_DIR = Path(__file__).resolve().parent
# On Render the persistent disk is mounted at /app/data; fall back to project root locally.
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SCAN_HISTORY_FILE = DATA_DIR / "scan_history.json"
STATE_FILE = DATA_DIR / "signals_state.json"
LOG_FILE = DATA_DIR / "trading_bot.log"
COOLDOWN_HOURS = 48

app = Flask(__name__)


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
</style>
</head>
<body>

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
    <div class="header-right">
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

    scheduler.start()
    logging.getLogger("dashboard").info(
        "Scheduler started — scans run every 30 min Mon–Fri 09:30–16:00 ET"
    )


if __name__ == "__main__":
    _start_scheduler()
    print("Dashboard running at http://localhost:5050")
    print("Landing page:  https://monumental-otter-86ec71.netlify.app")
    app.run(host="0.0.0.0", port=5050, debug=False, use_reloader=False)
else:
    # Running under gunicorn — start scheduler once (gunicorn --workers 1)
    _start_scheduler()
