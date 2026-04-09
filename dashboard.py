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

from config import DB_PATH, PRICING, SCAN_INTERVAL_SECS, DASHBOARD_PORT, DAILY_LIMIT_USD, ACTIVE_USER


def _calc_cost(model, inp, out, cr, cc):
    p = PRICING.get(model)
    if p is None:
        for key in PRICING:
            if key != "default" and model and model.startswith(key):
                p = PRICING[key]
                break
    if p is None:
        p = PRICING.get("default", {})
    return (
        inp * p.get("input", 0) / 1e6 + out * p.get("output", 0) / 1e6 +
        cr * p.get("cache_read", 0) / 1e6 + cc * p.get("cache_write", 0) / 1e6
    )


def get_dashboard_data(db_path=DB_PATH):
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ── System health info ────────────────────────────────────────────────────
    db_size_bytes = db_path.stat().st_size if db_path.exists() else 0
    file_count_row = conn.execute("SELECT COUNT(*) as cnt FROM processed_files").fetchone()
    total_files_tracked = file_count_row["cnt"] if file_count_row else 0

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

    # ── Turn-level data for recent sessions (for expandable rows) ────────────
    recent_session_ids = [s["session_id"] for s in session_rows[:20]]
    session_turns_map = {}
    if recent_session_ids:
        placeholders = ",".join("?" for _ in recent_session_ids)
        turn_detail_rows = conn.execute(f"""
            SELECT session_id, timestamp, model, input_tokens, output_tokens,
                   cache_read_tokens, cache_creation_tokens, tool_name
            FROM turns
            WHERE session_id IN ({placeholders})
            ORDER BY timestamp ASC
        """, recent_session_ids).fetchall()
        for tr in turn_detail_rows:
            sid = tr["session_id"]
            if sid not in session_turns_map:
                session_turns_map[sid] = []
            session_turns_map[sid].append({
                "ts": (tr["timestamp"] or "")[:19].replace("T", " "),
                "model": tr["model"] or "unknown",
                "input": tr["input_tokens"] or 0,
                "output": tr["output_tokens"] or 0,
                "cache_read": tr["cache_read_tokens"] or 0,
                "cache_creation": tr["cache_creation_tokens"] or 0,
                "tool": tr["tool_name"] or "",
            })

    # ── Git branch usage breakdown ────────────────────────────────────────────
    branch_rows = conn.execute("""
        SELECT COALESCE(git_branch, '(none)') as branch,
               SUM(total_input_tokens) as inp,
               SUM(total_output_tokens) as out,
               SUM(total_cache_read) as cr,
               SUM(total_cache_creation) as cc,
               SUM(turn_count) as turns,
               COUNT(*) as sessions
        FROM sessions
        GROUP BY branch
        ORDER BY inp + out DESC
        LIMIT 20
    """).fetchall()
    branches = []
    for r in branch_rows:
        cost = _calc_cost("default", r["inp"] or 0, r["out"] or 0,
                          r["cr"] or 0, r["cc"] or 0)
        branches.append({
            "branch": r["branch"], "input": r["inp"] or 0,
            "output": r["out"] or 0, "turns": r["turns"] or 0,
            "sessions": r["sessions"], "cost": round(cost, 4),
        })

    # ── Recent anomalies ──────────────────────────────────────────────────────
    anomalies = []
    try:
        anomaly_rows = conn.execute("""
            SELECT id, detected_at, metric, value, baseline, factor,
                   severity, message, acknowledged
            FROM anomalies
            WHERE detected_at >= datetime('now', '-7 days')
            ORDER BY detected_at DESC
            LIMIT 20
        """).fetchall()
        anomalies = [dict(r) for r in anomaly_rows]
    except Exception:
        pass

    # ── Cost forecast ─────────────────────────────────────────────────────────
    today_str = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now()
    today_total_row = conn.execute("""
        SELECT SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc
        FROM turns WHERE substr(timestamp, 1, 10) = ?
    """, (today_str,)).fetchone()

    cut2h = (datetime.utcnow() - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%S')
    burn_2h_row = conn.execute(
        "SELECT SUM(input_tokens + output_tokens) as t FROM turns WHERE timestamp >= ?",
        (cut2h,)
    ).fetchone()

    today_inp = today_total_row["inp"] or 0
    today_out = today_total_row["out"] or 0
    today_cr = today_total_row["cr"] or 0
    today_cc = today_total_row["cc"] or 0
    today_cost = _calc_cost("default", today_inp, today_out, today_cr, today_cc)
    today_tokens = today_inp + today_out
    burn_pm = (burn_2h_row["t"] or 0) / 120
    cpt = today_cost / max(today_tokens, 1)
    hours_left = max(0, 24 - now.hour - now.minute / 60)
    proj_cost = today_cost + burn_pm * 60 * hours_left * cpt
    proj_tokens = today_tokens + burn_pm * 60 * hours_left

    forecast = {
        "today_cost": round(today_cost, 4),
        "today_tokens": today_tokens,
        "projected_eod_cost": round(proj_cost, 4),
        "projected_eod_tokens": int(proj_tokens),
        "burn_rate_per_min": round(burn_pm, 1),
        "hours_remaining": round(hours_left, 1),
    }

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
        "system_info": {
            "db_size_bytes":      db_size_bytes,
            "total_files_tracked": total_files_tracked,
            "scan_interval_secs":  SCAN_INTERVAL_SECS,
        },
        "session_turns": session_turns_map,
        "branches": branches,
        "anomalies": anomalies,
        "forecast": forecast,
        "active_user": ACTIVE_USER,
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

  /* ── Light theme ─────────────────────────────────────────────────── */
  .light-theme {
    --bg:     #F5F5F5;
    --card:   #FFFFFF;
    --border: #E0E0E0;
    --text:   #1A1A1A;
    --muted:  #6B7280;
    --accent: #D4603A;
    --blue:   #3B7DD8;
    --green:  #2E9E5A;
  }
  .light-theme .model-tag { background: rgba(59,125,216,0.10); color: var(--blue); }
  .light-theme tr:hover td { background: rgba(0,0,0,0.02); }
  .light-theme .chart-card, .light-theme .stat-card, .light-theme .table-card { box-shadow: 0 1px 3px rgba(0,0,0,0.06); }

  /* ── Theme toggle ──────────────────────────────────────────────── */
  .theme-toggle { background: transparent; border: 1px solid var(--border); color: var(--muted); width: 34px; height: 34px; border-radius: 8px; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: border-color 0.2s, color 0.2s; font-size: 16px; flex-shrink: 0; }
  .theme-toggle:hover { border-color: var(--accent); color: var(--text); }

  /* ── Connection pulse ──────────────────────────────────────────── */
  .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; flex-shrink: 0; }
  .status-dot.live { background: var(--green); animation: pulse 2s infinite; }
  .status-dot.error { background: #E07A5F; }
  .status-dot.loading { background: var(--muted); animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
  .header-right { display: flex; align-items: center; gap: 12px; }
  .connection-info { display: flex; align-items: center; font-size: 12px; color: var(--muted); }

  /* ── Skeleton loading ──────────────────────────────────────────── */
  .skeleton { background: var(--border); border-radius: 4px; animation: shimmer 1.5s infinite; }
  @keyframes shimmer { 0% { opacity: 0.6; } 50% { opacity: 0.3; } 100% { opacity: 0.6; } }
  .skeleton-card { height: 90px; }
  .skeleton-chart { height: 240px; }
  .skeleton-row { height: 20px; margin-bottom: 8px; }
  #loading-overlay { position: fixed; inset: 0; background: var(--bg); z-index: 1000; display: flex; flex-direction: column; align-items: center; justify-content: center; transition: opacity 0.4s; }
  #loading-overlay.hidden { opacity: 0; pointer-events: none; }
  .loading-spinner { width: 40px; height: 40px; border: 3px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; margin-bottom: 16px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .loading-text { color: var(--muted); font-size: 14px; }

  /* ── Search input ──────────────────────────────────────────────── */
  .table-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; flex-wrap: wrap; gap: 8px; }
  .search-input { background: var(--bg); border: 1px solid var(--border); color: var(--text); font-size: 12px; border-radius: 6px; padding: 6px 12px; width: 240px; transition: border-color 0.2s; }
  .search-input:focus { border-color: var(--accent); outline: none; }
  .search-input::placeholder { color: var(--muted); }

  /* ── Sort indicators ───────────────────────────────────────────── */
  th.sortable { cursor: pointer; user-select: none; white-space: nowrap; transition: color 0.15s; }
  th.sortable:hover { color: var(--accent); }
  th.sortable::after { content: ' \2195'; opacity: 0.3; font-size: 10px; }
  th.sortable.sort-asc::after { content: ' \2191'; opacity: 1; color: var(--accent); }
  th.sortable.sort-desc::after { content: ' \2193'; opacity: 1; color: var(--accent); }

  /* ── Export buttons ────────────────────────────────────────────── */
  .export-group { display: flex; gap: 6px; }
  .export-btn { padding: 4px 10px; border-radius: 4px; border: 1px solid var(--border); background: transparent; color: var(--muted); font-size: 11px; cursor: pointer; transition: all 0.15s; display: flex; align-items: center; gap: 4px; }
  .export-btn:hover { border-color: var(--accent); color: var(--text); background: rgba(224,122,95,0.08); }
  .export-btn svg { width: 12px; height: 12px; }

  /* ── Custom date range ─────────────────────────────────────────── */
  .date-range-inputs { display: flex; align-items: center; gap: 6px; }
  .date-input { background: var(--bg); border: 1px solid var(--border); color: var(--text); font-size: 11px; border-radius: 4px; padding: 3px 6px; cursor: pointer; }
  .date-input:focus { border-color: var(--accent); outline: none; }
  .date-range-inputs .filter-label { margin: 0; }

  /* ── Expandable session rows ───────────────────────────────────── */
  .expandable { cursor: pointer; }
  .expandable td:first-child::before { content: '\25B6'; font-size: 9px; margin-right: 6px; color: var(--muted); transition: transform 0.2s; display: inline-block; }
  .expandable.expanded td:first-child::before { transform: rotate(90deg); color: var(--accent); }
  .turn-detail-row td { padding: 6px 12px 6px 36px; font-size: 12px; background: rgba(224,122,95,0.03); border-bottom: 1px solid var(--border); }
  .turn-detail-row:last-child td { border-bottom: 1px solid var(--border); }
  .turn-tool-tag { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; background: rgba(123,168,212,0.12); color: var(--blue); font-family: monospace; }

  /* ── System health panel ───────────────────────────────────────── */
  .system-panel { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px 20px; margin-bottom: 24px; }
  .system-panel-toggle { background: transparent; border: none; color: var(--muted); font-size: 12px; cursor: pointer; display: flex; align-items: center; gap: 6px; padding: 0; transition: color 0.15s; }
  .system-panel-toggle:hover { color: var(--text); }
  .system-panel-toggle::before { content: '\25B6'; font-size: 9px; transition: transform 0.2s; display: inline-block; }
  .system-panel-toggle.open::before { transform: rotate(90deg); }
  .system-details { display: none; margin-top: 12px; }
  .system-details.visible { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
  .sys-item { font-size: 12px; }
  .sys-item .sys-label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; }
  .sys-item .sys-value { font-family: monospace; margin-top: 2px; }

  /* ── Keyboard shortcut hint ────────────────────────────────────── */
  .kbd { display: inline-block; padding: 1px 5px; border: 1px solid var(--border); border-radius: 3px; font-size: 10px; font-family: monospace; color: var(--muted); background: var(--bg); }
  .shortcut-modal { position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 2000; display: flex; align-items: center; justify-content: center; opacity: 0; pointer-events: none; transition: opacity 0.2s; }
  .shortcut-modal.visible { opacity: 1; pointer-events: auto; }
  .shortcut-content { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 24px 32px; min-width: 360px; max-width: 480px; }
  .shortcut-content h3 { font-size: 14px; color: var(--accent); margin-bottom: 16px; font-weight: 600; }
  .shortcut-list { list-style: none; }
  .shortcut-list li { display: flex; justify-content: space-between; padding: 6px 0; font-size: 13px; color: var(--text); border-bottom: 1px solid var(--border); }
  .shortcut-list li:last-child { border-bottom: none; }

  @media (max-width: 768px) { .charts-grid { grid-template-columns: 1fr; } .chart-card.wide { grid-column: 1; } .search-input { width: 100%; } .table-header { flex-direction: column; align-items: flex-start; } .date-range-inputs { flex-wrap: wrap; } }
</style>
</head>
<body>
<div id="loading-overlay">
  <div class="loading-spinner"></div>
  <div class="loading-text">Loading dashboard data&hellip;</div>
</div>

<div id="shortcut-modal" class="shortcut-modal" onclick="toggleShortcuts()">
  <div class="shortcut-content" onclick="event.stopPropagation()">
    <h3>Keyboard Shortcuts</h3>
    <ul class="shortcut-list">
      <li><span>Toggle theme</span><span class="kbd">T</span></li>
      <li><span>Refresh data</span><span class="kbd">R</span></li>
      <li><span>Range: 7 days</span><span class="kbd">1</span></li>
      <li><span>Range: 30 days</span><span class="kbd">2</span></li>
      <li><span>Range: 90 days</span><span class="kbd">3</span></li>
      <li><span>Range: All time</span><span class="kbd">4</span></li>
      <li><span>Focus search</span><span class="kbd">/</span></li>
      <li><span>Export CSV</span><span class="kbd">E</span></li>
      <li><span>Toggle system panel</span><span class="kbd">I</span></li>
      <li><span>Show shortcuts</span><span class="kbd">?</span></li>
    </ul>
  </div>
</div>

<header>
  <h1>Claude Code Usage Dashboard</h1>
  <div class="header-right">
    <div class="connection-info"><span class="status-dot loading" id="status-dot"></span><span id="meta">Connecting&hellip;</span></div>
    <button class="theme-toggle" id="theme-toggle" onclick="toggleTheme()" title="Toggle theme (T)">&#9790;</button>
  </div>
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
    <button class="range-btn" data-range="custom" onclick="showCustomDatePicker()">Custom</button>
  </div>
  <div class="filter-sep"></div>
  <div class="date-range-inputs" id="custom-range" style="display:none">
    <span class="filter-label">From</span>
    <input type="date" class="date-input" id="date-from" onchange="onCustomDateChange()">
    <span class="filter-label">To</span>
    <input type="date" class="date-input" id="date-to" onchange="onCustomDateChange()">
    <button class="filter-btn" onclick="clearCustomRange()" title="Clear custom range">&times;</button>
  </div>
  <div class="filter-sep"></div>
  <div class="filter-label">Project</div>
  <select id="project-filter" onchange="onProjectChange(this.value)">
    <option value="all">All Projects</option>
  </select>
  <div style="margin-left:auto; display:flex; align-items:center; gap:6px;">
    <span class="kbd" style="cursor:pointer" onclick="toggleShortcuts()" title="Keyboard shortcuts">?</span>
  </div>
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
    <div class="table-header">
      <div class="section-title" style="margin-bottom:0">Recent Sessions</div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <input type="text" class="search-input" id="session-search" placeholder="Search sessions by project, branch, model&hellip;" oninput="onSessionSearch()">
        <div class="export-group">
          <button class="export-btn" onclick="exportCSV()" title="Export CSV (E)">
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 12v2h12v-2"/><path d="M8 2v8m0 0l3-3m-3 3L5 7"/></svg>
            CSV
          </button>
          <button class="export-btn" onclick="exportJSON()" title="Export JSON">
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 12v2h12v-2"/><path d="M8 2v8m0 0l3-3m-3 3L5 7"/></svg>
            JSON
          </button>
        </div>
      </div>
    </div>
    <table>
      <thead><tr>
        <th class="sortable" data-sort="session_id" onclick="sortTable('session_id')">Session</th>
        <th class="sortable" data-sort="project" onclick="sortTable('project')">Project</th>
        <th class="sortable" data-sort="branch" onclick="sortTable('branch')">Branch</th>
        <th class="sortable" data-sort="last" onclick="sortTable('last')">Last Active</th>
        <th class="sortable" data-sort="duration_min" onclick="sortTable('duration_min')">Duration</th>
        <th class="sortable" data-sort="model" onclick="sortTable('model')">Model</th>
        <th class="sortable" data-sort="turns" onclick="sortTable('turns')">Turns</th>
        <th class="sortable" data-sort="input" onclick="sortTable('input')">Input</th>
        <th class="sortable" data-sort="output" onclick="sortTable('output')">Output</th>
        <th class="sortable" data-sort="cost" onclick="sortTable('cost')">Est. Cost</th>
      </tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
  </div>
  <!-- Forecast + Anomalies + Branches -->
  <div class="charts-grid">
    <div class="chart-card">
      <h2>Cost Forecast</h2>
      <div id="forecast-panel" style="font-size:13px">
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px">
          <div><span class="muted">Today's cost</span><div id="fc-today-cost" style="font-size:20px;font-weight:700;color:var(--green)">—</div></div>
          <div><span class="muted">Projected EOD</span><div id="fc-proj-cost" style="font-size:20px;font-weight:700;color:var(--accent)">—</div></div>
          <div><span class="muted">Burn rate</span><div id="fc-burn" class="num">—</div></div>
          <div><span class="muted">Hours left</span><div id="fc-hours" class="num">—</div></div>
          <div><span class="muted">Today's tokens</span><div id="fc-tokens" class="num">—</div></div>
          <div><span class="muted">Projected tokens</span><div id="fc-proj-tokens" class="num">—</div></div>
        </div>
        <div id="fc-limit-bar" style="margin-top:16px;display:none">
          <div class="muted" style="font-size:11px;margin-bottom:4px">Daily Limit Progress</div>
          <div style="background:var(--border);border-radius:4px;height:8px;overflow:hidden">
            <div id="fc-limit-fill" style="height:100%;border-radius:4px;transition:width 0.5s"></div>
          </div>
          <div id="fc-limit-text" class="muted" style="font-size:11px;margin-top:4px"></div>
        </div>
      </div>
    </div>
    <div class="chart-card">
      <h2>Anomaly Alerts</h2>
      <div id="anomaly-panel" style="max-height:260px;overflow-y:auto;font-size:13px">
        <div class="muted">No anomalies detected</div>
      </div>
    </div>
    <div class="chart-card wide">
      <h2>Git Branch Usage</h2>
      <div class="chart-wrap"><canvas id="chart-branches"></canvas></div>
    </div>
  </div>

  <div class="table-card">
    <div class="table-header">
      <div class="section-title" style="margin-bottom:0">Cost by Model</div>
      <div class="export-group">
        <button class="export-btn" onclick="exportModelCSV()" title="Export model costs CSV">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 12v2h12v-2"/><path d="M8 2v8m0 0l3-3m-3 3L5 7"/></svg>
          CSV
        </button>
      </div>
    </div>
    <table>
      <thead><tr>
        <th>Model</th><th>Turns</th><th>Input</th><th>Output</th>
        <th>Cache Read</th><th>Cache Creation</th><th>Est. Cost</th>
      </tr></thead>
      <tbody id="model-cost-body"></tbody>
    </table>
  </div>
  <div class="system-panel" id="system-panel">
    <button class="system-panel-toggle" id="sys-toggle" onclick="toggleSystemPanel()">System Health &amp; Info</button>
    <div class="system-details" id="system-details">
      <div class="sys-item"><div class="sys-label">Database Size</div><div class="sys-value" id="sys-db-size">—</div></div>
      <div class="sys-item"><div class="sys-label">Files Tracked</div><div class="sys-value" id="sys-files">—</div></div>
      <div class="sys-item"><div class="sys-label">Scan Interval</div><div class="sys-value" id="sys-interval">—</div></div>
      <div class="sys-item"><div class="sys-label">Total Models</div><div class="sys-value" id="sys-models">—</div></div>
      <div class="sys-item"><div class="sys-label">Total Sessions</div><div class="sys-value" id="sys-sessions">—</div></div>
      <div class="sys-item"><div class="sys-label">Last Updated</div><div class="sys-value" id="sys-updated">—</div></div>
    </div>
  </div>
</div>

<footer>
  <div class="footer-content">
    <p>Cost estimates based on Anthropic API pricing (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>). Pricing is loaded from <code>config.py</code> — edit it there when rates change. Only models with an explicit entry in the pricing table are included in cost calculations. Actual costs for Max/Pro subscribers differ from API pricing.</p>
    <p>
      GitHub: <a href="https://github.com/DEADSERPENT/claude-usage" target="_blank">https://github.com/DEADSERPENT/claude-usage</a>
      &nbsp;&middot;&nbsp;
      Created by: <a href="https://www.productcompass.pm" target="_blank">DEADSERPENT</a>
      &nbsp;&middot;&nbsp;
      License: MIT
      &nbsp;&middot;&nbsp;
      Press <span class="kbd">?</span> for keyboard shortcuts
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
let rawData          = null;
let selectedModels   = new Set();
let selectedRange    = '30d';
let selectedProject  = 'all';
let chartMode        = 'tokens';   // 'tokens' | 'cost'
let charts           = {};
let sessionSortKey   = 'last';
let sessionSortDir   = 'desc';    // 'asc' | 'desc'
let sessionSearchQ   = '';
let customDateFrom   = null;
let customDateTo     = null;
let connectionState  = 'loading'; // 'live' | 'error' | 'loading'
let failCount        = 0;
let _lastFilteredSessions = [];   // cached for export/sort

// ── Theme ──────────────────────────────────────────────────────────────────
function initTheme() {
  const stored = localStorage.getItem('cu-theme');
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  const theme = stored || (prefersDark ? 'dark' : 'dark');
  applyTheme(theme);
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', e => {
    if (!localStorage.getItem('cu-theme')) applyTheme(e.matches ? 'dark' : 'light');
  });
}
function applyTheme(theme) {
  document.body.classList.toggle('light-theme', theme === 'light');
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.innerHTML = theme === 'light' ? '&#9728;' : '&#9790;';
  if (charts.daily)   updateChartTheme(charts.daily);
  if (charts.model)   updateChartTheme(charts.model);
  if (charts.project) updateChartTheme(charts.project);
  if (charts.tools)   updateChartTheme(charts.tools);
  if (charts.hourly)  updateChartTheme(charts.hourly);
}
function toggleTheme() {
  const isLight = document.body.classList.contains('light-theme');
  const next = isLight ? 'dark' : 'light';
  localStorage.setItem('cu-theme', next);
  applyTheme(next);
}
function getThemeColors() {
  const isLight = document.body.classList.contains('light-theme');
  return {
    gridColor: isLight ? '#E0E0E0' : '#3A3733',
    tickColor: isLight ? '#6B7280' : '#8C8580',
    legendColor: isLight ? '#6B7280' : '#8C8580',
  };
}
function updateChartTheme(chart) {
  if (!chart) return;
  const c = getThemeColors();
  try {
    if (chart.options.scales && chart.options.scales.x) {
      chart.options.scales.x.ticks.color = c.tickColor;
      chart.options.scales.x.grid.color = c.gridColor;
    }
    if (chart.options.scales && chart.options.scales.y) {
      chart.options.scales.y.ticks.color = c.tickColor;
      chart.options.scales.y.grid.color = c.gridColor;
    }
    if (chart.options.plugins && chart.options.plugins.legend && chart.options.plugins.legend.labels) {
      chart.options.plugins.legend.labels.color = c.legendColor;
    }
    chart.update('none');
  } catch(e) {}
}
initTheme();

// ── Connection status ──────────────────────────────────────────────────────
function setConnectionState(state) {
  connectionState = state;
  const dot = document.getElementById('status-dot');
  if (!dot) return;
  dot.className = 'status-dot ' + state;
}

// ── Keyboard shortcuts ─────────────────────────────────────────────────────
function toggleShortcuts() {
  const m = document.getElementById('shortcut-modal');
  m.classList.toggle('visible');
}
document.addEventListener('keydown', function(e) {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;
  const key = e.key;
  if (key === '?' || (e.shiftKey && key === '/')) { toggleShortcuts(); return; }
  if (key === 'Escape') { document.getElementById('shortcut-modal').classList.remove('visible'); return; }
  if (key === 't' || key === 'T') { toggleTheme(); return; }
  if (key === 'r' || key === 'R') { loadData(); return; }
  if (key === '1') { setRange('7d'); return; }
  if (key === '2') { setRange('30d'); return; }
  if (key === '3') { setRange('90d'); return; }
  if (key === '4') { setRange('all'); return; }
  if (key === '/' ) { e.preventDefault(); document.getElementById('session-search').focus(); return; }
  if (key === 'e' || key === 'E') { exportCSV(); return; }
  if (key === 'i' || key === 'I') { toggleSystemPanel(); return; }
});

// ── System panel ───────────────────────────────────────────────────────────
function toggleSystemPanel() {
  const btn = document.getElementById('sys-toggle');
  const details = document.getElementById('system-details');
  btn.classList.toggle('open');
  details.classList.toggle('visible');
}
function renderSystemPanel(data) {
  if (!data.system_info) return;
  const si = data.system_info;
  const sizeKB = (si.db_size_bytes / 1024).toFixed(1);
  const sizeMB = (si.db_size_bytes / (1024*1024)).toFixed(2);
  document.getElementById('sys-db-size').textContent = si.db_size_bytes > 1048576 ? sizeMB + ' MB' : sizeKB + ' KB';
  document.getElementById('sys-files').textContent = si.total_files_tracked.toLocaleString();
  document.getElementById('sys-interval').textContent = si.scan_interval_secs + 's';
  document.getElementById('sys-models').textContent = data.all_models.length;
  document.getElementById('sys-sessions').textContent = data.sessions_all.length.toLocaleString();
  document.getElementById('sys-updated').textContent = data.generated_at;
}

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
const RANGE_LABELS = { '7d': 'Last 7 Days', '30d': 'Last 30 Days', '90d': 'Last 90 Days', 'all': 'All Time', 'custom': 'Custom Range' };
const RANGE_TICKS  = { '7d': 7, '30d': 15, '90d': 13, 'all': 12, 'custom': 15 };

function getRangeCutoff(range) {
  if (range === 'custom' && customDateFrom) return customDateFrom;
  if (range === 'all') return null;
  const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}
function getRangeEnd() {
  if (selectedRange === 'custom' && customDateTo) return customDateTo;
  return null;
}

// ── Custom date range ──────────────────────────────────────────────────────
function onCustomDateChange() {
  const from = document.getElementById('date-from').value;
  const to = document.getElementById('date-to').value;
  if (from) {
    customDateFrom = from;
    customDateTo = to || null;
    selectedRange = 'custom';
    document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
    updateURL();
    applyFilter();
  }
}
function clearCustomRange() {
  customDateFrom = null;
  customDateTo = null;
  document.getElementById('date-from').value = '';
  document.getElementById('date-to').value = '';
  document.getElementById('custom-range').style.display = 'none';
  setRange('30d');
}

function readURLRange() {
  const p = new URLSearchParams(window.location.search).get('range');
  return ['7d', '30d', '90d', 'all'].includes(p) ? p : '30d';
}

function setRange(range) {
  selectedRange = range;
  customDateFrom = null;
  customDateTo = null;
  document.querySelectorAll('.range-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.range === range)
  );
  document.getElementById('custom-range').style.display = 'none';
  updateURL();
  applyFilter();
}
function showCustomDatePicker() {
  document.getElementById('custom-range').style.display = 'flex';
  document.getElementById('date-from').focus();
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

// ── Session search ─────────────────────────────────────────────────────────
function onSessionSearch() {
  sessionSearchQ = (document.getElementById('session-search').value || '').toLowerCase().trim();
  applyFilter();
}

// ── Table sorting ──────────────────────────────────────────────────────────
function sortTable(key) {
  if (sessionSortKey === key) {
    sessionSortDir = sessionSortDir === 'asc' ? 'desc' : 'asc';
  } else {
    sessionSortKey = key;
    sessionSortDir = (key === 'last' || key === 'cost' || key === 'turns' || key === 'input' || key === 'output' || key === 'duration_min') ? 'desc' : 'asc';
  }
  document.querySelectorAll('th.sortable').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.sort === sessionSortKey) th.classList.add('sort-' + sessionSortDir);
  });
  applyFilter();
}

function sortSessions(sessions) {
  const dir = sessionSortDir === 'asc' ? 1 : -1;
  const key = sessionSortKey;
  return [...sessions].sort((a, b) => {
    let va, vb;
    if (key === 'cost') {
      va = calcCost(a.model, a.input, a.output, a.cache_read, a.cache_creation);
      vb = calcCost(b.model, b.input, b.output, b.cache_read, b.cache_creation);
    } else {
      va = a[key]; vb = b[key];
    }
    if (typeof va === 'string') return dir * va.localeCompare(vb || '');
    return dir * ((va || 0) - (vb || 0));
  });
}

// ── Export functions ────────────────────────────────────────────────────────
function downloadFile(filename, content, mime) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

function exportCSV() {
  if (!_lastFilteredSessions.length) return;
  const headers = ['Session','Project','Branch','Last Active','Duration (min)','Model','Turns','Input Tokens','Output Tokens','Cache Read','Cache Creation','Est. Cost'];
  const rows = _lastFilteredSessions.map(s => [
    s.session_id, s.project, s.branch, s.last, s.duration_min, s.model,
    s.turns, s.input, s.output, s.cache_read, s.cache_creation,
    calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation).toFixed(6)
  ]);
  const csv = [headers.join(','), ...rows.map(r => r.map(v => '"' + String(v).replace(/"/g, '""') + '"').join(','))].join('\n');
  downloadFile('claude_usage_sessions.csv', csv, 'text/csv');
}

function exportJSON() {
  if (!_lastFilteredSessions.length) return;
  const data = _lastFilteredSessions.map(s => ({
    ...s, est_cost: calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation)
  }));
  downloadFile('claude_usage_sessions.json', JSON.stringify(data, null, 2), 'application/json');
}

function exportModelCSV() {
  const tbody = document.getElementById('model-cost-body');
  if (!tbody || !tbody.innerHTML) return;
  if (!rawData) return;
  const lines = ['Model,Turns,Input,Output,Cache Read,Cache Creation,Est. Cost'];
  const cutoff = getRangeCutoff(selectedRange);
  const filtered = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && (!cutoff || r.day >= cutoff)
  );
  const modelMap = {};
  for (const r of filtered) {
    if (!modelMap[r.model]) modelMap[r.model] = { model: r.model, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0 };
    const m = modelMap[r.model];
    m.input += r.input; m.output += r.output; m.cache_read += r.cache_read; m.cache_creation += r.cache_creation; m.turns += r.turns;
  }
  for (const m of Object.values(modelMap)) {
    const cost = calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation);
    lines.push([m.model, m.turns, m.input, m.output, m.cache_read, m.cache_creation, cost.toFixed(6)].join(','));
  }
  downloadFile('claude_usage_by_model.csv', lines.join('\n'), 'text/csv');
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
  const rangeEnd = getRangeEnd();

  // ── Daily token aggregation (model + date range, no project filter) ──────
  const filteredDaily = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && (!cutoff || r.day >= cutoff) && (!rangeEnd || r.day <= rangeEnd)
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

  // ── Sessions filtered by model + date + project + search ─────────────────
  const filteredSessions = rawData.sessions_all.filter(s => {
    if (!selectedModels.has(s.model)) return false;
    if (cutoff && s.last_date < cutoff) return false;
    if (rangeEnd && s.last_date > rangeEnd) return false;
    if (selectedProject !== 'all' && s.project !== selectedProject) return false;
    if (sessionSearchQ) {
      const hay = (s.session_id + ' ' + s.project + ' ' + s.branch + ' ' + s.model).toLowerCase();
      if (!hay.includes(sessionSearchQ)) return false;
    }
    return true;
  });

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
    selectedModels.has(r.model) && (!cutoff || r.day >= cutoff) && (!rangeEnd || r.day <= rangeEnd)
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

  // ── Cumulative cost data ─────────────────────────────────────────────────
  let cumCost = 0;
  const cumulativeCost = dailyCost.map(d => { cumCost += d.cost; return { day: d.day, cumCost }; });

  const sortedSessions = sortSessions(filteredSessions);
  _lastFilteredSessions = sortedSessions;

  renderStats(totals);
  renderDailyChart(daily, dailyCost, cumulativeCost);
  renderModelChart(byModel);
  renderProjectChart(byProject);
  renderToolsChart(byTool);
  renderHourlyChart(byHour);
  renderSessionsTable(sortedSessions.slice(0, rawData.ui_limits.sessions_table));
  renderModelCostTable(byModelForCost);
  renderSystemPanel(rawData);
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

function renderDailyChart(daily, dailyCost, cumulativeCost) {
  const ctx = document.getElementById('chart-daily').getContext('2d');
  const tc = getThemeColors();

  if (chartMode === 'cost') {
    if (charts.daily && charts.dailyMode === 'cost') {
      charts.daily.data.labels = dailyCost.map(d => d.day);
      charts.daily.data.datasets[0].data = dailyCost.map(d => d.cost);
      charts.daily.data.datasets[1].data = cumulativeCost.map(d => d.cumCost);
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
        datasets: [
          { label: 'Daily Cost ($)', data: dailyCost.map(d => d.cost), backgroundColor: 'rgba(224,122,95,0.75)', order: 2 },
          { label: 'Cumulative ($)', data: cumulativeCost.map(d => d.cumCost), type: 'line', borderColor: 'rgba(109,191,138,0.9)', backgroundColor: 'rgba(109,191,138,0.1)', fill: false, borderWidth: 2, pointRadius: 0, tension: 0.3, yAxisID: 'y1', order: 1 },
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { color: tc.legendColor, boxWidth: 12 } } },
        scales: {
          x: { ticks: { color: tc.tickColor, maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: tc.gridColor } },
          y: { position: 'left', ticks: { color: tc.tickColor, callback: v => '$' + v.toFixed(3) }, grid: { color: tc.gridColor } },
          y1: { position: 'right', ticks: { color: 'rgba(109,191,138,0.8)', callback: v => '$' + v.toFixed(2) }, grid: { drawOnChartArea: false } },
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
      plugins: { legend: { labels: { color: tc.legendColor, boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: tc.tickColor, maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: tc.gridColor } },
        y: { ticks: { color: tc.tickColor, callback: v => fmt(v) }, grid: { color: tc.gridColor } },
      }
    }
  });
}

function renderModelChart(byModel) {
  const tc = getThemeColors();
  const borderCol = document.body.classList.contains('light-theme') ? '#FFFFFF' : '#262320';
  if (!byModel.length) {
    if (charts.model) { charts.model.destroy(); charts.model = null; }
    return;
  }
  if (charts.model) {
    charts.model.data.labels = byModel.map(m => m.model);
    charts.model.data.datasets[0].data = byModel.map(m => m.input + m.output);
    charts.model.data.datasets[0].borderColor = borderCol;
    charts.model.options.plugins.legend.labels.color = tc.legendColor;
    charts.model.update('none');
    return;
  }
  const ctx = document.getElementById('chart-model').getContext('2d');
  charts.model = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: byModel.map(m => m.model),
      datasets: [{ data: byModel.map(m => m.input + m.output), backgroundColor: MODEL_COLORS, borderWidth: 2, borderColor: borderCol }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { color: tc.legendColor, boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${fmt(ctx.raw)} tokens` } }
      }
    }
  });
}

function renderProjectChart(byProject) {
  const tc = getThemeColors();
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
      plugins: { legend: { labels: { color: tc.legendColor, boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: tc.tickColor, callback: v => fmt(v) }, grid: { color: tc.gridColor } },
        y: { ticks: { color: tc.tickColor, font: { size: 11 } }, grid: { color: tc.gridColor } },
      }
    }
  });
}

function renderToolsChart(byTool) {
  const tc = getThemeColors();
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
        x: { ticks: { color: tc.tickColor, callback: v => fmt(v) }, grid: { color: tc.gridColor } },
        y: { ticks: { color: tc.tickColor, font: { size: 11 } }, grid: { color: tc.gridColor } },
      }
    }
  });
}

function renderHourlyChart(byHour) {
  const tc = getThemeColors();
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
      plugins: { legend: { labels: { color: tc.legendColor, boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: tc.tickColor, maxTicksLimit: 24, font: { size: 10 } }, grid: { color: tc.gridColor } },
        y: { ticks: { color: tc.tickColor, callback: v => fmt(v) }, grid: { color: tc.gridColor } },
      }
    }
  });
}

function renderSessionsTable(sessions) {
  const turnsMap = rawData.session_turns || {};
  document.getElementById('sessions-body').innerHTML = sessions.map((s, idx) => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    const costCell = isBillable(s.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const branchCell = s.branch
      ? `<td class="muted" style="font-size:11px;font-family:monospace">${escapeHTML(s.branch)}</td>`
      : `<td class="muted">\u2014</td>`;
    const hasTurns = Object.keys(turnsMap).some(k => k.startsWith(s.session_id.replace(/\u2026/g, '')));
    const fullSid = Object.keys(turnsMap).find(k => k.startsWith(s.session_id.replace(/\u2026/g, '')));
    const turnRows = fullSid && turnsMap[fullSid] ? turnsMap[fullSid] : [];
    const detailHtml = turnRows.length > 0 ? turnRows.slice(0, 15).map(t =>
      `<tr class="turn-detail-row" data-parent="${idx}" style="display:none">
        <td colspan="3">${t.tool ? `<span class="turn-tool-tag">${escapeHTML(t.tool)}</span>` : '<span class="muted">\u2014</span>'}</td>
        <td class="muted" style="font-size:11px">${escapeHTML(t.ts)}</td>
        <td colspan="2"><span class="model-tag">${escapeHTML(t.model)}</span></td>
        <td class="num" style="font-size:11px">${fmt(t.input)}</td>
        <td class="num" style="font-size:11px">${fmt(t.output)}</td>
        <td class="num" style="font-size:11px">${fmt(t.cache_read)}</td>
        <td></td>
      </tr>`
    ).join('') + (turnRows.length > 15 ? `<tr class="turn-detail-row" data-parent="${idx}" style="display:none"><td colspan="10" class="muted" style="font-size:11px;text-align:center">+ ${turnRows.length - 15} more turns</td></tr>` : '') : '';
    return `<tr class="expandable" data-idx="${idx}" onclick="toggleSessionExpand(this, ${idx})">
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
    </tr>${detailHtml}`;
  }).join('');
}

function toggleSessionExpand(row, idx) {
  row.classList.toggle('expanded');
  const isExpanded = row.classList.contains('expanded');
  document.querySelectorAll(`.turn-detail-row[data-parent="${idx}"]`).forEach(tr => {
    tr.style.display = isExpanded ? '' : 'none';
  });
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
  setConnectionState('loading');
  try {
    const resp = await fetch('/api/data');
    const d = await resp.json();
    if (d.error) {
      setConnectionState('error');
      document.getElementById('meta').textContent = d.error;
      return;
    }

    failCount = 0;
    setConnectionState('live');
    const isFirstLoad = rawData === null;
    rawData = d;

    if (!_refreshTimer) {
      _refreshTimer = setInterval(loadData, d.refresh_ms);
    }

    const refreshSecs = Math.round(d.refresh_ms / 1000);
    document.getElementById('meta').textContent =
      'Updated: ' + d.generated_at + ' \u00b7 Auto-refresh ' + refreshSecs + 's';

    // Remove loading overlay on first successful load
    if (isFirstLoad) {
      const overlay = document.getElementById('loading-overlay');
      if (overlay) { overlay.classList.add('hidden'); setTimeout(() => overlay.remove(), 500); }

      selectedRange = readURLRange();
      document.querySelectorAll('.range-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.range === selectedRange)
      );
      buildFilterUI(d.all_models);
      buildProjectFilter(d.sessions_all);
    }

    applyFilter();
  } catch(e) {
    failCount++;
    setConnectionState('error');
    document.getElementById('meta').textContent =
      'Connection lost \u00b7 Retry ' + failCount + (failCount > 3 ? ' \u00b7 Is the server running?' : '');
    console.error(e);
  }
}

// ── Forecast panel ─────────────────────────────────────────────────────────
function renderForecast(fc) {
  if (!fc) return;
  document.getElementById('fc-today-cost').textContent = '$' + fc.today_cost.toFixed(4);
  document.getElementById('fc-proj-cost').textContent = '$' + fc.projected_eod_cost.toFixed(4);
  document.getElementById('fc-burn').textContent = fmt(Math.round(fc.burn_rate_per_min)) + '/min';
  document.getElementById('fc-hours').textContent = fc.hours_remaining.toFixed(1) + 'h';
  document.getElementById('fc-tokens').textContent = fmt(fc.today_tokens);
  document.getElementById('fc-proj-tokens').textContent = fmt(fc.projected_eod_tokens);

  if (rawData && rawData.daily_limit_usd > 0) {
    const bar = document.getElementById('fc-limit-bar');
    const fill = document.getElementById('fc-limit-fill');
    const text = document.getElementById('fc-limit-text');
    bar.style.display = 'block';
    const pct = Math.min(100, (fc.today_cost / rawData.daily_limit_usd) * 100);
    fill.style.width = pct + '%';
    fill.style.background = pct > 80 ? 'var(--accent)' : 'var(--green)';
    text.textContent = pct.toFixed(1) + '% of $' + rawData.daily_limit_usd.toFixed(2) + ' limit used';
  }
}

// ── Anomaly panel ──────────────────────────────────────────────────────────
function renderAnomalies(anomalies) {
  const el = document.getElementById('anomaly-panel');
  if (!anomalies || !anomalies.length) {
    el.innerHTML = '<div class="muted" style="padding:20px 0;text-align:center">No anomalies detected</div>';
    return;
  }
  const sevColors = { critical: 'var(--accent)', warning: '#E0B86D', info: 'var(--blue)' };
  el.innerHTML = anomalies.slice(0, 10).map(a => `
    <div style="padding:8px 0;border-bottom:1px solid var(--border)">
      <div style="display:flex;align-items:center;gap:6px">
        <span style="width:8px;height:8px;border-radius:50%;background:${sevColors[a.severity] || 'var(--muted)'};flex-shrink:0"></span>
        <span style="font-weight:600;font-size:11px;text-transform:uppercase;color:${sevColors[a.severity] || 'var(--muted)'}">${escapeHTML(a.severity)}</span>
        <span class="muted" style="font-size:11px;margin-left:auto">${escapeHTML((a.detected_at||'').slice(0,16))}</span>
      </div>
      <div style="margin-top:4px;font-size:12px">${escapeHTML(a.message)}</div>
      <div class="muted" style="font-size:11px;margin-top:2px">
        ${escapeHTML(a.metric)} · value: ${a.value} · baseline: ${a.baseline} · ${a.factor}x
      </div>
    </div>
  `).join('');
}

// ── Branch chart ───────────────────────────────────────────────────────────
function renderBranchChart(branches) {
  const tc = getThemeColors();
  if (!branches || !branches.length) {
    if (charts.branches) { charts.branches.destroy(); charts.branches = null; }
    return;
  }
  const top = branches.slice(0, 10);
  const labels = top.map(b => b.branch.length > 22 ? '\u2026' + b.branch.slice(-20) : b.branch);
  if (charts.branches) {
    charts.branches.data.labels = labels;
    charts.branches.data.datasets[0].data = top.map(b => b.input);
    charts.branches.data.datasets[1].data = top.map(b => b.output);
    charts.branches.update('none');
    return;
  }
  const ctx = document.getElementById('chart-branches').getContext('2d');
  charts.branches = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Input', data: top.map(b => b.input), backgroundColor: TOKEN_COLORS.input },
        { label: 'Output', data: top.map(b => b.output), backgroundColor: TOKEN_COLORS.output },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: tc.legendColor, boxWidth: 12 } },
        tooltip: { callbacks: { afterLabel: (ctx) => {
          const b = top[ctx.dataIndex];
          return b ? 'Cost: $' + b.cost.toFixed(4) + ' · ' + b.sessions + ' sessions' : '';
        }}}
      },
      scales: {
        x: { ticks: { color: tc.tickColor, callback: v => fmt(v) }, grid: { color: tc.gridColor } },
        y: { ticks: { color: tc.tickColor, font: { size: 11 } }, grid: { color: tc.gridColor } },
      }
    }
  });
}

// Patch applyFilter to render new sections
const _origApplyFilter = applyFilter;
applyFilter = function() {
  _origApplyFilter();
  if (rawData) {
    renderForecast(rawData.forecast);
    renderAnomalies(rawData.anomalies);
    renderBranchChart(rawData.branches);
  }
};

loadData();
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))

        elif path == "/api/data":
            data = get_dashboard_data()
            self._send_json(data)

        elif path == "/api/anomalies":
            try:
                from anomaly import get_recent_anomalies
                anomalies = get_recent_anomalies(DB_PATH)
                self._send_json({"anomalies": anomalies})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/optimize":
            try:
                from optimizer import analyze
                result = analyze(DB_PATH)
                self._send_json(result)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/branches":
            try:
                data = get_dashboard_data()
                self._send_json({"branches": data.get("branches", [])})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/forecast":
            try:
                data = get_dashboard_data()
                self._send_json(data.get("forecast", {}))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

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
