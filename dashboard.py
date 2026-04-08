"""
dashboard.py - Local web dashboard.  Port is read from CLAUDE_USAGE_PORT
(default 8080).  Refresh interval is driven by CLAUDE_USAGE_SCAN_INTERVAL.
"""

import json
import sqlite3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime

from datetime import timedelta

from config import DB_PATH, PRICING, SCAN_INTERVAL_SECS, DASHBOARD_PORT, DAILY_LIMIT_USD


def get_dashboard_data(db_path=DB_PATH):
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ── All models (for filter UI) ────────────────────────────────────────────
    model_rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') as model
        FROM turns
        GROUP BY model
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """).fetchall()
    all_models = [r["model"] for r in model_rows]

    # ── Daily per-model, ALL history (client filters by range) ────────────────
    daily_rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 10)   as day,
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            COUNT(*)                   as turns
        FROM turns
        GROUP BY day, model
        ORDER BY day, model
    """).fetchall()

    daily_by_model = [{
        "day":            r["day"],
        "model":          r["model"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":          r["turns"] or 0,
    } for r in daily_rows]

    # ── All sessions (client filters by range and model) ──────────────────────
    session_rows = conn.execute("""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp,
            git_branch, total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation, model, turn_count
        FROM sessions
        ORDER BY last_timestamp DESC
    """).fetchall()

    sessions_all = []
    for r in session_rows:
        try:
            t1 = datetime.fromisoformat(r["first_timestamp"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(r["last_timestamp"].replace("Z", "+00:00"))
            duration_min = round((t2 - t1).total_seconds() / 60, 1)
        except Exception:
            duration_min = 0
        sessions_all.append({
            "session_id":    r["session_id"][:8],
            "project":       r["project_name"] or "unknown",
            "branch":        r["git_branch"] or "",
            "last":          (r["last_timestamp"] or "")[:16].replace("T", " "),
            "last_date":     (r["last_timestamp"] or "")[:10],
            "duration_min":  duration_min,
            "model":         r["model"] or "unknown",
            "turns":         r["turn_count"] or 0,
            "input":         r["total_input_tokens"] or 0,
            "output":        r["total_output_tokens"] or 0,
            "cache_read":    r["total_cache_read"] or 0,
            "cache_creation": r["total_cache_creation"] or 0,
        })

    # ── Burn rate: tokens in the last 15 minutes ─────────────────────────────
    fifteen_ago = (datetime.utcnow() - timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%S')
    burn_row = conn.execute("""
        SELECT SUM(input_tokens + output_tokens) as tokens
        FROM turns WHERE timestamp >= ?
    """, (fifteen_ago,)).fetchone()
    burn_rate_per_min = round((burn_row["tokens"] or 0) / 15, 1)

    # ── Hourly activity for the last 48 hours (client filters by model) ───────
    hourly_rows = conn.execute("""
        SELECT
            substr(timestamp, 1, 13)   as hour,
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            COUNT(*)                   as turns
        FROM turns
        WHERE timestamp >= datetime('now', '-48 hours')
        GROUP BY hour, model
        ORDER BY hour
    """).fetchall()
    hourly_by_model = [
        {"hour": r["hour"], "model": r["model"],
         "input": r["input"] or 0, "output": r["output"] or 0, "turns": r["turns"] or 0}
        for r in hourly_rows
    ]

    # ── All-time peak day ─────────────────────────────────────────────────────
    peak_row = conn.execute("""
        SELECT substr(timestamp, 1, 10) as day,
               SUM(input_tokens + output_tokens) as total
        FROM turns GROUP BY day ORDER BY total DESC LIMIT 1
    """).fetchone()
    peak_day = {"day": peak_row["day"], "tokens": peak_row["total"]} if peak_row else None

    # ── Tool usage per day / model (client filters by range + model) ─────────
    tool_rows = conn.execute("""
        SELECT
            tool_name,
            substr(timestamp, 1, 10)   as day,
            COALESCE(model, 'unknown') as model,
            COUNT(*)                   as count
        FROM turns
        WHERE tool_name IS NOT NULL
        GROUP BY tool_name, day, model
        ORDER BY day
    """).fetchall()
    tools_daily = [
        {"tool": r["tool_name"], "day": r["day"], "model": r["model"], "count": r["count"]}
        for r in tool_rows
    ]

    conn.close()

    return {
        "all_models":     all_models,
        "daily_by_model": daily_by_model,
        "sessions_all":   sessions_all,
        "tools_daily":      tools_daily,
        "hourly_by_model":  hourly_by_model,
        "burn_rate_per_min": burn_rate_per_min,
        "peak_day":         peak_day,
        "daily_limit_usd":  DAILY_LIMIT_USD,
        "pricing":          dict(PRICING),
        "refresh_ms":       SCAN_INTERVAL_SECS * 1000,
        "ui_limits": {
            "sessions_table":  20,
            "tools_chart":     15,
            "projects_chart":  10,
        },
        "generated_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Usage Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg:     #1E1E1E;
    --card:   #262320;
    --border: #3A3733;
    --text:   #EAEAEA;
    --muted:  #8C8580;
    --accent: #E07A5F;
    --blue:   #7BA8D4;
    --green:  #6DBF8A;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }

  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--accent); }
  header .meta { color: var(--muted); font-size: 12px; }

  #filter-bar { background: var(--card); border-bottom: 1px solid var(--border); padding: 10px 24px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .filter-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); white-space: nowrap; }
  .filter-sep { width: 1px; height: 22px; background: var(--border); flex-shrink: 0; }
  #model-checkboxes { display: flex; flex-wrap: wrap; gap: 6px; }
  .model-cb-label { display: flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 20px; border: 1px solid var(--border); cursor: pointer; font-size: 12px; color: var(--muted); transition: border-color 0.15s, color 0.15s, background 0.15s; user-select: none; }
  .model-cb-label:hover { border-color: var(--accent); color: var(--text); }
  .model-cb-label.checked { background: rgba(224,122,95,0.12); border-color: var(--accent); color: var(--text); }
  .model-cb-label input { display: none; }
  .filter-btn { padding: 3px 10px; border-radius: 4px; border: 1px solid var(--border); background: transparent; color: var(--muted); font-size: 11px; cursor: pointer; white-space: nowrap; }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  .range-group { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; flex-shrink: 0; }
  .range-btn { padding: 4px 13px; background: transparent; border: none; border-right: 1px solid var(--border); color: var(--muted); font-size: 12px; cursor: pointer; transition: background 0.15s, color 0.15s; }
  .range-btn:last-child { border-right: none; }
  .range-btn:hover { background: rgba(255,255,255,0.04); color: var(--text); }
  .range-btn.active { background: rgba(224,122,95,0.15); color: var(--accent); font-weight: 600; }

  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }
  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .stat-card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  .stat-card .value { font-size: 22px; font-weight: 700; }
  .stat-card .sub { color: var(--muted); font-size: 11px; margin-top: 4px; }

  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  .chart-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }
  .chart-card.wide { grid-column: 1 / -1; }
  .chart-card h2 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 16px; }
  .chart-wrap { position: relative; height: 240px; }
  .chart-wrap.tall { height: 300px; }

  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); border-bottom: 1px solid var(--border); }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  .model-tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; background: rgba(123,168,212,0.15); color: var(--blue); }
  .cost { color: var(--green); font-family: monospace; }
  .cost-na { color: var(--muted); font-family: monospace; font-size: 11px; }
  .num { font-family: monospace; }
  .muted { color: var(--muted); }
  .section-title { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
  .table-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 24px; overflow-x: auto; }

  footer { border-top: 1px solid var(--border); padding: 20px 24px; margin-top: 8px; }
  .footer-content { max-width: 1400px; margin: 0 auto; }
  .footer-content p { color: var(--muted); font-size: 12px; line-height: 1.7; margin-bottom: 4px; }
  .footer-content p:last-child { margin-bottom: 0; }
  .footer-content a { color: var(--blue); text-decoration: none; }
  .footer-content a:hover { text-decoration: underline; }

  .toggle-group { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; flex-shrink: 0; }
  .toggle-btn { padding: 4px 13px; background: transparent; border: none; border-right: 1px solid var(--border); color: var(--muted); font-size: 12px; cursor: pointer; transition: background 0.15s, color 0.15s; }
  .toggle-btn:last-child { border-right: none; }
  .toggle-btn:hover { background: rgba(255,253,250,0.04); color: var(--text); }
  .toggle-btn.active { background: rgba(224,122,95,0.15); color: var(--accent); font-weight: 600; }
  .chart-card-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
  .chart-card-header h2 { margin-bottom: 0; }
  #project-filter { background: var(--card); border: 1px solid var(--border); color: var(--muted); font-size: 12px; border-radius: 6px; padding: 4px 8px; cursor: pointer; max-width: 200px; }
  #project-filter:hover, #project-filter:focus { border-color: var(--accent); color: var(--text); outline: none; }

  @media (max-width: 768px) { .charts-grid { grid-template-columns: 1fr; } .chart-card.wide { grid-column: 1; } }
</style>
</head>
<body>
<header>
  <h1>Claude Code Usage Dashboard</h1>
  <div class="meta" id="meta">Loading...</div>
</header>

<div id="filter-bar">
  <div class="filter-label">Models</div>
  <div id="model-checkboxes"></div>
  <button class="filter-btn" onclick="selectAllModels()">All</button>
  <button class="filter-btn" onclick="clearAllModels()">None</button>
  <div class="filter-sep"></div>
  <div class="filter-label">Range</div>
  <div class="range-group">
    <button class="range-btn" data-range="7d"  onclick="setRange('7d')">7d</button>
    <button class="range-btn" data-range="30d" onclick="setRange('30d')">30d</button>
    <button class="range-btn" data-range="90d" onclick="setRange('90d')">90d</button>
    <button class="range-btn" data-range="all" onclick="setRange('all')">All</button>
  </div>
  <div class="filter-sep"></div>
  <div class="filter-label">Project</div>
  <select id="project-filter" onchange="onProjectChange(this.value)">
    <option value="all">All Projects</option>
  </select>
</div>

<div class="container">
  <div class="stats-row" id="stats-row"></div>
  <div class="charts-grid">
    <div class="chart-card wide">
      <div class="chart-card-header">
        <h2 id="daily-chart-title">Daily Token Usage</h2>
        <div class="toggle-group">
          <button class="toggle-btn active" data-mode="tokens" onclick="setChartMode('tokens')">Tokens</button>
          <button class="toggle-btn"        data-mode="cost"   onclick="setChartMode('cost')">Cost ($)</button>
        </div>
      </div>
      <div class="chart-wrap tall"><canvas id="chart-daily"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>By Model</h2>
      <div class="chart-wrap"><canvas id="chart-model"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>Top Projects by Tokens</h2>
      <div class="chart-wrap"><canvas id="chart-project"></canvas></div>
    </div>
    <div class="chart-card wide">
      <h2>Most Used Tools</h2>
      <div class="chart-wrap"><canvas id="chart-tools"></canvas></div>
    </div>
    <div class="chart-card wide">
      <h2 id="hourly-chart-title">Hourly Activity — Last 48 Hours</h2>
      <div class="chart-wrap tall"><canvas id="chart-hourly"></canvas></div>
    </div>
  </div>
  <div class="table-card">
    <div class="section-title">Recent Sessions</div>
    <table>
      <thead><tr>
        <th>Session</th><th>Project</th><th>Branch</th><th>Last Active</th><th>Duration</th>
        <th>Model</th><th>Turns</th><th>Input</th><th>Output</th><th>Est. Cost</th>
      </tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-title">Cost by Model</div>
    <table>
      <thead><tr>
        <th>Model</th><th>Turns</th><th>Input</th><th>Output</th>
        <th>Cache Read</th><th>Cache Creation</th><th>Est. Cost</th>
      </tr></thead>
      <tbody id="model-cost-body"></tbody>
    </table>
  </div>
</div>

<footer>
  <div class="footer-content">
    <p>Cost estimates based on Anthropic API pricing (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>). Pricing is loaded from <code>config.py</code> — edit it there when rates change. Only models with an explicit entry in the pricing table are included in cost calculations. Actual costs for Max/Pro subscribers differ from API pricing.</p>
    <p>
      GitHub: <a href="https://github.com/phuryn/claude-usage" target="_blank">https://github.com/phuryn/claude-usage</a>
      &nbsp;&middot;&nbsp;
      Created by: <a href="https://www.productcompass.pm" target="_blank">The Product Compass Newsletter</a>
      &nbsp;&middot;&nbsp;
      License: MIT
    </p>
  </div>
</footer>

<script>
// ── XSS protection ────────────────────────────────────────────────────────
function escapeHTML(str) {
  if (str == null) return '';
  return String(str).replace(/[&<>'"]/g, tag => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  }[tag]));
}

// ── State ──────────────────────────────────────────────────────────────────
let rawData       = null;
let selectedModels  = new Set();
let selectedRange   = '30d';
let selectedProject = 'all';
let chartMode       = 'tokens';   // 'tokens' | 'cost'
let charts          = {};         // charts.dailyMode tracks current daily chart type

// ── Pricing (served from config.py — no hardcoded values here) ─────────────
function isBillable(model) {
  // A model is billable if it has an explicit entry in the server-supplied
  // pricing table (excludes the 'default' fallback key).
  if (!model || !rawData || !rawData.pricing) return false;
  const p = rawData.pricing;
  if (p[model]) return true;
  return Object.keys(p).some(k => k !== 'default' && model.startsWith(k));
}

function getPricing(model) {
  const pricing = rawData && rawData.pricing;
  if (!pricing || !model) return null;
  if (pricing[model]) return pricing[model];
  for (const key of Object.keys(pricing)) {
    if (key !== 'default' && model.startsWith(key)) return pricing[key];
  }
  const m = model.toLowerCase();
  if (m.includes('opus'))   return pricing['claude-opus-4-6']   || pricing['default'];
  if (m.includes('sonnet')) return pricing['claude-sonnet-4-6'] || pricing['default'];
  if (m.includes('haiku'))  return pricing['claude-haiku-4-5']  || pricing['default'];
  return pricing['default'] || null;
}

function calcCost(model, inp, out, cacheRead, cacheCreation) {
  if (!isBillable(model)) return 0;
  const p = getPricing(model);
  if (!p) return 0;
  return (
    inp           * p.input       / 1e6 +
    out           * p.output      / 1e6 +
    cacheRead     * p.cache_read  / 1e6 +
    cacheCreation * p.cache_write / 1e6
  );
}

// ── Formatting ─────────────────────────────────────────────────────────────
function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(2)+'B';
  if (n >= 1e6) return (n/1e6).toFixed(2)+'M';
  if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
  return n.toLocaleString();
}
function fmtCost(c)    { return '$' + c.toFixed(4); }
function fmtCostBig(c) { return '$' + c.toFixed(2); }

// ── Chart colors ───────────────────────────────────────────────────────────
const TOKEN_COLORS = {
  input:          'rgba(123,168,212,0.85)',
  output:         'rgba(178,152,210,0.85)',
  cache_read:     'rgba(109,191,138,0.70)',
  cache_creation: 'rgba(224,184,109,0.70)',
};
const MODEL_COLORS = ['#E07A5F','#7BA8D4','#6DBF8A','#B498DC','#E0B86D','#DC8FB0','#5BB89A','#8AAFD4'];

// ── Time range ─────────────────────────────────────────────────────────────
const RANGE_LABELS = { '7d': 'Last 7 Days', '30d': 'Last 30 Days', '90d': 'Last 90 Days', 'all': 'All Time' };
const RANGE_TICKS  = { '7d': 7, '30d': 15, '90d': 13, 'all': 12 };

function getRangeCutoff(range) {
  if (range === 'all') return null;
  const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

function readURLRange() {
  const p = new URLSearchParams(window.location.search).get('range');
  return ['7d', '30d', '90d', 'all'].includes(p) ? p : '30d';
}

function setRange(range) {
  selectedRange = range;
  document.querySelectorAll('.range-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.range === range)
  );
  updateURL();
  applyFilter();
}

// ── Model filter ───────────────────────────────────────────────────────────
function modelPriority(m) {
  const ml = m.toLowerCase();
  if (ml.includes('opus'))   return 0;
  if (ml.includes('sonnet')) return 1;
  if (ml.includes('haiku'))  return 2;
  return 3;
}

function readURLModels(allModels) {
  const param = new URLSearchParams(window.location.search).get('models');
  if (!param) return new Set(allModels.filter(m => isBillable(m)));
  const fromURL = new Set(param.split(',').map(s => s.trim()).filter(Boolean));
  return new Set(allModels.filter(m => fromURL.has(m)));
}

function isDefaultModelSelection(allModels) {
  const billable = allModels.filter(m => isBillable(m));
  if (selectedModels.size !== billable.length) return false;
  return billable.every(m => selectedModels.has(m));
}

function buildFilterUI(allModels) {
  const sorted = [...allModels].sort((a, b) => {
    const pa = modelPriority(a), pb = modelPriority(b);
    return pa !== pb ? pa - pb : a.localeCompare(b);
  });
  selectedModels = readURLModels(allModels);
  const container = document.getElementById('model-checkboxes');
  container.innerHTML = sorted.map(m => {
    const checked = selectedModels.has(m);
    const em = escapeHTML(m);
    return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-model="${em}">
      <input type="checkbox" value="${em}" ${checked ? 'checked' : ''} onchange="onModelToggle(this)">
      ${em}
    </label>`;
  }).join('');
}

function onModelToggle(cb) {
  const label = cb.closest('label');
  if (cb.checked) { selectedModels.add(cb.value);    label.classList.add('checked'); }
  else            { selectedModels.delete(cb.value); label.classList.remove('checked'); }
  updateURL();
  applyFilter();
}

function selectAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = true; selectedModels.add(cb.value); cb.closest('label').classList.add('checked');
  });
  updateURL(); applyFilter();
}

function clearAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = false; selectedModels.delete(cb.value); cb.closest('label').classList.remove('checked');
  });
  updateURL(); applyFilter();
}

// ── Project filter ─────────────────────────────────────────────────────────
function buildProjectFilter(sessions) {
  const projects = [...new Set(sessions.map(s => s.project))].sort();
  const select   = document.getElementById('project-filter');
  const current  = selectedProject;
  select.innerHTML = '<option value="all">All Projects</option>' +
    projects.map(p => `<option value="${escapeHTML(p)}">${escapeHTML(p)}</option>`).join('');
  select.value = projects.includes(current) ? current : 'all';
  selectedProject  = select.value;
}

function onProjectChange(val) {
  selectedProject = val;
  applyFilter();
}

// ── Chart-mode toggle ──────────────────────────────────────────────────────
function setChartMode(mode) {
  chartMode = mode;
  document.querySelectorAll('.toggle-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.mode === mode)
  );
  if (rawData) applyFilter();
}

// ── URL persistence ────────────────────────────────────────────────────────
function updateURL() {
  const allModels = Array.from(document.querySelectorAll('#model-checkboxes input')).map(cb => cb.value);
  const params = new URLSearchParams();
  if (selectedRange !== '30d') params.set('range', selectedRange);
  if (!isDefaultModelSelection(allModels)) params.set('models', Array.from(selectedModels).join(','));
  const search = params.toString() ? '?' + params.toString() : '';
  history.replaceState(null, '', window.location.pathname + search);
}

// ── Aggregation & filtering ────────────────────────────────────────────────
function applyFilter() {
  if (!rawData) return;

  const cutoff = getRangeCutoff(selectedRange);

  // ── Daily token aggregation (model + date range, no project filter) ──────
  const filteredDaily = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && (!cutoff || r.day >= cutoff)
  );

  const dailyMap = {};
  for (const r of filteredDaily) {
    if (!dailyMap[r.day]) dailyMap[r.day] = { day: r.day, input: 0, output: 0, cache_read: 0, cache_creation: 0 };
    const d = dailyMap[r.day];
    d.input          += r.input;
    d.output         += r.output;
    d.cache_read     += r.cache_read;
    d.cache_creation += r.cache_creation;
  }
  const daily = Object.values(dailyMap).sort((a, b) => a.day.localeCompare(b.day));

  // ── Daily cost per day (for cost-toggle mode) ─────────────────────────────
  const dailyCostMap = {};
  for (const r of filteredDaily) {
    dailyCostMap[r.day] = (dailyCostMap[r.day] || 0) +
      calcCost(r.model, r.input, r.output, r.cache_read, r.cache_creation);
  }
  const dailyCost = Object.entries(dailyCostMap)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([day, cost]) => ({ day, cost }));

  // ── By model (from daily data, no project filter) ─────────────────────────
  const modelMap = {};
  for (const r of filteredDaily) {
    if (!modelMap[r.model]) modelMap[r.model] = { model: r.model, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0 };
    const m = modelMap[r.model];
    m.input += r.input; m.output += r.output;
    m.cache_read += r.cache_read; m.cache_creation += r.cache_creation;
    m.turns += r.turns;
  }

  // ── Sessions filtered by model + date + project ───────────────────────────
  const filteredSessions = rawData.sessions_all.filter(s =>
    selectedModels.has(s.model) &&
    (!cutoff || s.last_date >= cutoff) &&
    (selectedProject === 'all' || s.project === selectedProject)
  );

  for (const s of filteredSessions) {
    if (modelMap[s.model]) modelMap[s.model].sessions++;
  }
  const byModel = Object.values(modelMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // ── By project ────────────────────────────────────────────────────────────
  const projMap = {};
  for (const s of filteredSessions) {
    if (!projMap[s.project]) projMap[s.project] = { project: s.project, input: 0, output: 0, turns: 0 };
    projMap[s.project].input  += s.input;
    projMap[s.project].output += s.output;
    projMap[s.project].turns  += s.turns;
  }
  const byProject = Object.values(projMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // ── Cost by model — project-aware ─────────────────────────────────────────
  let byModelForCost = byModel;
  if (selectedProject !== 'all') {
    const sm = {};
    for (const s of filteredSessions) {
      if (!sm[s.model]) sm[s.model] = { model: s.model, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0 };
      const m = sm[s.model];
      m.input += s.input; m.output += s.output;
      m.cache_read += s.cache_read; m.cache_creation += s.cache_creation;
      m.turns += s.turns;
    }
    byModelForCost = Object.values(sm).sort((a, b) => (b.input + b.output) - (a.input + a.output));
  }

  // ── Tools aggregation (model + date range filter) ─────────────────────────
  const filteredTools = rawData.tools_daily.filter(r =>
    selectedModels.has(r.model) && (!cutoff || r.day >= cutoff)
  );
  const toolMap = {};
  for (const r of filteredTools) toolMap[r.tool] = (toolMap[r.tool] || 0) + r.count;
  const byTool = Object.entries(toolMap)
    .map(([tool, count]) => ({ tool, count }))
    .sort((a, b) => b.count - a.count)
    .slice(0, rawData.ui_limits.tools_chart);

  // ── Totals ────────────────────────────────────────────────────────────────
  const totals = {
    sessions:       filteredSessions.length,
    turns:          byModel.reduce((s, m) => s + m.turns, 0),
    input:          byModel.reduce((s, m) => s + m.input, 0),
    output:         byModel.reduce((s, m) => s + m.output, 0),
    cache_read:     byModel.reduce((s, m) => s + m.cache_read, 0),
    cache_creation: byModel.reduce((s, m) => s + m.cache_creation, 0),
    cost:           byModel.reduce((s, m) => s + calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation), 0),
  };

  const modeLabel = chartMode === 'cost' ? 'Daily Cost' : 'Daily Token Usage';
  document.getElementById('daily-chart-title').textContent = modeLabel + ' \u2014 ' + RANGE_LABELS[selectedRange];

  // ── Hourly chart (last 48h, model-filtered) ───────────────────────────────
  const filteredHourly = rawData.hourly_by_model.filter(r => selectedModels.has(r.model));
  const hourlyMap = {};
  for (const r of filteredHourly) {
    if (!hourlyMap[r.hour]) hourlyMap[r.hour] = { hour: r.hour, input: 0, output: 0 };
    hourlyMap[r.hour].input  += r.input;
    hourlyMap[r.hour].output += r.output;
  }
  const byHour = Object.values(hourlyMap).sort((a, b) => a.hour.localeCompare(b.hour));

  renderStats(totals);
  renderDailyChart(daily, dailyCost);
  renderModelChart(byModel);
  renderProjectChart(byProject);
  renderToolsChart(byTool);
  renderHourlyChart(byHour);
  renderSessionsTable(filteredSessions.slice(0, rawData.ui_limits.sessions_table));
  renderModelCostTable(byModelForCost);
}

// ── Renderers ──────────────────────────────────────────────────────────────
function renderStats(t) {
  const rangeLabel = RANGE_LABELS[selectedRange].toLowerCase();
  const stats = [
    { label: 'Sessions',       value: t.sessions.toLocaleString(), sub: rangeLabel },
    { label: 'Turns',          value: fmt(t.turns),                sub: rangeLabel },
    { label: 'Input Tokens',   value: fmt(t.input),                sub: rangeLabel },
    { label: 'Output Tokens',  value: fmt(t.output),               sub: rangeLabel },
    { label: 'Cache Read',     value: fmt(t.cache_read),           sub: 'from prompt cache' },
    { label: 'Cache Creation', value: fmt(t.cache_creation),       sub: 'writes to prompt cache' },
    { label: 'Est. Cost',      value: fmtCostBig(t.cost),          sub: 'see config.py for rates', color: '#6DBF8A' },
  ];
  if (rawData.burn_rate_per_min > 0) {
    stats.push({ label: 'Burn Rate',    value: fmt(Math.round(rawData.burn_rate_per_min)) + '/min', sub: 'last 15 minutes' });
  }
  if (rawData.peak_day) {
    stats.push({ label: 'Peak Day',     value: fmt(rawData.peak_day.tokens), sub: rawData.peak_day.day });
  }
  if (rawData.daily_limit_usd > 0) {
    const pct       = Math.min(100, (t.cost / rawData.daily_limit_usd) * 100).toFixed(1);
    const remaining = Math.max(0, rawData.daily_limit_usd - t.cost);
    stats.push({ label: 'Daily Budget', value: '$' + remaining.toFixed(2) + ' left',
      sub: pct + '% of $' + rawData.daily_limit_usd.toFixed(2) + ' used',
      color: remaining < rawData.daily_limit_usd * 0.2 ? '#E07A5F' : undefined });
  }
  document.getElementById('stats-row').innerHTML = stats.map(s => `
    <div class="stat-card">
      <div class="label">${escapeHTML(s.label)}</div>
      <div class="value" style="${s.color ? 'color:' + s.color : ''}">${escapeHTML(s.value)}</div>
      ${s.sub ? `<div class="sub">${escapeHTML(s.sub)}</div>` : ''}
    </div>
  `).join('');
}

function renderDailyChart(daily, dailyCost) {
  const ctx = document.getElementById('chart-daily').getContext('2d');

  if (chartMode === 'cost') {
    if (charts.daily && charts.dailyMode === 'cost') {
      charts.daily.data.labels = dailyCost.map(d => d.day);
      charts.daily.data.datasets[0].data = dailyCost.map(d => d.cost);
      charts.daily.options.scales.x.ticks.maxTicksLimit = RANGE_TICKS[selectedRange];
      charts.daily.update('none');
      return;
    }
    if (charts.daily) { charts.daily.destroy(); charts.daily = null; }
    charts.dailyMode = 'cost';
    charts.daily = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: dailyCost.map(d => d.day),
        datasets: [{ label: 'Est. Cost ($)', data: dailyCost.map(d => d.cost), backgroundColor: 'rgba(224,122,95,0.75)' }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { color: '#8C8580', boxWidth: 12 } } },
        scales: {
          x: { ticks: { color: '#8C8580', maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: '#3A3733' } },
          y: { ticks: { color: '#8C8580', callback: v => '$' + v.toFixed(3) }, grid: { color: '#3A3733' } },
        }
      }
    });
    return;
  }

  // tokens mode
  if (charts.daily && charts.dailyMode === 'tokens') {
    charts.daily.data.labels = daily.map(d => d.day);
    charts.daily.data.datasets[0].data = daily.map(d => d.input);
    charts.daily.data.datasets[1].data = daily.map(d => d.output);
    charts.daily.data.datasets[2].data = daily.map(d => d.cache_read);
    charts.daily.data.datasets[3].data = daily.map(d => d.cache_creation);
    charts.daily.options.scales.x.ticks.maxTicksLimit = RANGE_TICKS[selectedRange];
    charts.daily.update('none');
    return;
  }
  if (charts.daily) { charts.daily.destroy(); charts.daily = null; }
  charts.dailyMode = 'tokens';
  charts.daily = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: daily.map(d => d.day),
      datasets: [
        { label: 'Input',          data: daily.map(d => d.input),          backgroundColor: TOKEN_COLORS.input,          stack: 'tokens' },
        { label: 'Output',         data: daily.map(d => d.output),         backgroundColor: TOKEN_COLORS.output,         stack: 'tokens' },
        { label: 'Cache Read',     data: daily.map(d => d.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     stack: 'tokens' },
        { label: 'Cache Creation', data: daily.map(d => d.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, stack: 'tokens' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8C8580', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#8C8580', maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: '#3A3733' } },
        y: { ticks: { color: '#8C8580', callback: v => fmt(v) }, grid: { color: '#3A3733' } },
      }
    }
  });
}

function renderModelChart(byModel) {
  if (!byModel.length) {
    if (charts.model) { charts.model.destroy(); charts.model = null; }
    return;
  }
  if (charts.model) {
    charts.model.data.labels = byModel.map(m => m.model);
    charts.model.data.datasets[0].data = byModel.map(m => m.input + m.output);
    charts.model.update('none');
    return;
  }
  const ctx = document.getElementById('chart-model').getContext('2d');
  charts.model = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: byModel.map(m => m.model),
      datasets: [{ data: byModel.map(m => m.input + m.output), backgroundColor: MODEL_COLORS, borderWidth: 2, borderColor: '#262320' }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { color: '#8C8580', boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${fmt(ctx.raw)} tokens` } }
      }
    }
  });
}

function renderProjectChart(byProject) {
  const top = byProject.slice(0, rawData.ui_limits.projects_chart);
  const labels = top.map(p => p.project.length > 22 ? '\u2026' + p.project.slice(-20) : p.project);
  if (!top.length) {
    if (charts.project) { charts.project.destroy(); charts.project = null; }
    return;
  }
  if (charts.project) {
    charts.project.data.labels = labels;
    charts.project.data.datasets[0].data = top.map(p => p.input);
    charts.project.data.datasets[1].data = top.map(p => p.output);
    charts.project.update('none');
    return;
  }
  const ctx = document.getElementById('chart-project').getContext('2d');
  charts.project = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Input',  data: top.map(p => p.input),  backgroundColor: TOKEN_COLORS.input },
        { label: 'Output', data: top.map(p => p.output), backgroundColor: TOKEN_COLORS.output },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8C8580', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#8C8580', callback: v => fmt(v) }, grid: { color: '#3A3733' } },
        y: { ticks: { color: '#8C8580', font: { size: 11 } }, grid: { color: '#3A3733' } },
      }
    }
  });
}

function renderToolsChart(byTool) {
  if (!byTool.length) {
    if (charts.tools) { charts.tools.destroy(); charts.tools = null; }
    return;
  }
  if (charts.tools) {
    charts.tools.data.labels = byTool.map(t => t.tool);
    charts.tools.data.datasets[0].data = byTool.map(t => t.count);
    charts.tools.update('none');
    return;
  }
  const ctx = document.getElementById('chart-tools').getContext('2d');
  charts.tools = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: byTool.map(t => t.tool),
      datasets: [{
        label: 'Uses', data: byTool.map(t => t.count),
        backgroundColor: 'rgba(224,122,95,0.75)', borderColor: 'rgba(224,122,95,1)', borderWidth: 1,
      }]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: '#8C8580', callback: v => fmt(v) }, grid: { color: '#3A3733' } },
        y: { ticks: { color: '#8C8580', font: { size: 11 } }, grid: { color: '#3A3733' } },
      }
    }
  });
}

function renderHourlyChart(byHour) {
  function fmtHour(h) {
    const today = new Date().toISOString().slice(0, 10);
    const yest  = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
    const d = h.slice(0, 10), hr = h.slice(11, 13);
    if (d === today) return hr + ':00';
    if (d === yest)  return 'Y ' + hr + ':00';
    return h.slice(5, 10) + ' ' + hr;
  }
  const labels = byHour.map(h => fmtHour(h.hour));
  if (!byHour.length) {
    if (charts.hourly) { charts.hourly.destroy(); charts.hourly = null; }
    return;
  }
  if (charts.hourly) {
    charts.hourly.data.labels = labels;
    charts.hourly.data.datasets[0].data = byHour.map(h => h.input);
    charts.hourly.data.datasets[1].data = byHour.map(h => h.output);
    charts.hourly.update('none');
    return;
  }
  const ctx = document.getElementById('chart-hourly').getContext('2d');
  charts.hourly = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Input',  data: byHour.map(h => h.input),  backgroundColor: TOKEN_COLORS.input,  stack: 'tokens' },
        { label: 'Output', data: byHour.map(h => h.output), backgroundColor: TOKEN_COLORS.output, stack: 'tokens' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8C8580', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#8C8580', maxTicksLimit: 24, font: { size: 10 } }, grid: { color: '#3A3733' } },
        y: { ticks: { color: '#8C8580', callback: v => fmt(v) }, grid: { color: '#3A3733' } },
      }
    }
  });
}

function renderSessionsTable(sessions) {
  document.getElementById('sessions-body').innerHTML = sessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    const costCell = isBillable(s.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const branchCell = s.branch
      ? `<td class="muted" style="font-size:11px;font-family:monospace">${escapeHTML(s.branch)}</td>`
      : `<td class="muted">—</td>`;
    return `<tr>
      <td class="muted" style="font-family:monospace">${escapeHTML(s.session_id)}&hellip;</td>
      <td>${escapeHTML(s.project)}</td>
      ${branchCell}
      <td class="muted">${escapeHTML(s.last)}</td>
      <td class="muted">${escapeHTML(String(s.duration_min))}m</td>
      <td><span class="model-tag">${escapeHTML(s.model)}</span></td>
      <td class="num">${s.turns}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      ${costCell}
    </tr>`;
  }).join('');
}

function renderModelCostTable(byModel) {
  document.getElementById('model-cost-body').innerHTML = byModel.map(m => {
    const cost = calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation);
    const costCell = isBillable(m.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    return `<tr>
      <td><span class="model-tag">${escapeHTML(m.model)}</span></td>
      <td class="num">${fmt(m.turns)}</td>
      <td class="num">${fmt(m.input)}</td>
      <td class="num">${fmt(m.output)}</td>
      <td class="num">${fmt(m.cache_read)}</td>
      <td class="num">${fmt(m.cache_creation)}</td>
      ${costCell}
    </tr>`;
  }).join('');
}

// ── Data loading ───────────────────────────────────────────────────────────
let _refreshTimer = null;

async function loadData() {
  try {
    const resp = await fetch('/api/data');
    const d = await resp.json();
    if (d.error) {
      document.body.innerHTML = '<div style="padding:40px;color:#f87171">' + escapeHTML(d.error) + '</div>';
      return;
    }

    const isFirstLoad = rawData === null;
    rawData = d;

    // Start the auto-refresh timer exactly once, using the server-supplied interval.
    if (!_refreshTimer) {
      _refreshTimer = setInterval(loadData, d.refresh_ms);
    }

    const refreshSecs = Math.round(d.refresh_ms / 1000);
    document.getElementById('meta').textContent =
      'Updated: ' + d.generated_at + ' \u00b7 Auto-refresh in ' + refreshSecs + 's';

    if (isFirstLoad) {
      selectedRange = readURLRange();
      document.querySelectorAll('.range-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.range === selectedRange)
      );
      buildFilterUI(d.all_models);
      buildProjectFilter(d.sessions_all);
    }

    applyFilter();
  } catch(e) {
    console.error(e);
  }
}

loadData();
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))

        elif self.path == "/api/data":
            data = get_dashboard_data()
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()


def serve(port=None):
    if port is None:
        port = DASHBOARD_PORT
    server = ThreadingHTTPServer(("localhost", port), DashboardHandler)
    print(f"Dashboard running at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
