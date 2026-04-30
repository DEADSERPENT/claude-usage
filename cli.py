"""
cli.py - Command-line interface for the Claude Code usage dashboard.

Commands:
  scan        - Scan JSONL files and update the database
  today       - Print today's usage summary
  stats       - Print all-time usage statistics
  live        - Real-time terminal monitor (Ctrl+C to stop)
  forecast    - Burn-rate analysis and end-of-day projection
  export      - Export data to CSV/JSON/SQLite
  dashboard   - Scan + open browser + start dashboard server
  query       - Run analytics DSL queries
  replay      - Session replay with step-by-step timeline
  branches    - Git branch usage comparison
  optimize    - Cost optimization analysis and suggestions
  anomalies   - View detected usage anomalies
  archive     - Compress and archive old data
  timetravel  - Query historical DB state at a point in time
  api         - Start the REST API server
  daemon      - Background scanner daemon (start/stop/status)
  users       - Multi-user management
  plugins     - Plugin management
"""

import sys
import sqlite3
from pathlib import Path
from datetime import datetime, date, timedelta

from config import (
    DB_PATH,
    DAILY_LIMIT_USD,
    RBAC_ENABLED,
    ACTIVE_USER,
    SCAN_INTERVAL_SECS,
    get_pricing_for_model,
    calc_cost as estimate_cost,
)


def get_pricing(model: str) -> dict:
    return get_pricing_for_model(model)


def calc_cost(model, inp, out, cache_read, cache_creation):
    return estimate_cost(model, inp, out, cache_read, cache_creation)

def fmt(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def fmt_cost(c):
    return f"${c:.4f}"

def hr(char="-", width=60):
    print(char * width)


def _enable_windows_ansi():
    """Best-effort ANSI enablement on modern Windows terminals."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        # If ANSI cannot be enabled, output still works without colors.
        pass

def require_db():
    if not DB_PATH.exists():
        print("Database not found. Run: python cli.py scan")
        sys.exit(1)
    return sqlite3.connect(DB_PATH)


def _get_user_role(conn: sqlite3.Connection, user_id: str) -> str:
    try:
        row = conn.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return "admin" if user_id == "default" else "viewer"


def require_role(required_role: str = "admin"):
    """Enforce local RBAC when enabled via CLAUDE_USAGE_RBAC=1."""
    if not RBAC_ENABLED:
        return
    conn = require_db()
    try:
        role = _get_user_role(conn, ACTIVE_USER)
    finally:
        conn.close()
    if role != required_role:
        print(f"Permission denied: '{ACTIVE_USER}' has role '{role}', requires '{required_role}'.")
        sys.exit(1)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_scan():
    require_role("admin")
    from scanner import scan, PROJECTS_DIR
    print(f"Scanning {PROJECTS_DIR} ...")
    scan()


def cmd_today():
    conn = require_db()
    conn.row_factory = sqlite3.Row
    today = date.today().isoformat()

    rows = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as inp,
            SUM(output_tokens)         as out,
            SUM(cache_read_tokens)     as cr,
            SUM(cache_creation_tokens) as cc,
            COUNT(*)                   as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
        GROUP BY model
        ORDER BY inp + out DESC
    """, (today,)).fetchall()

    sessions = conn.execute("""
        SELECT COUNT(DISTINCT session_id) as cnt
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
    """, (today,)).fetchone()

    print()
    hr()
    print(f"  Today's Usage  ({today})")
    hr()

    if not rows:
        print("  No usage recorded today.")
        print()
        return

    total_inp = total_out = total_cr = total_cc = total_turns = 0
    total_cost = 0.0

    for r in rows:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        total_cost += cost
        total_inp += r["inp"] or 0
        total_out += r["out"] or 0
        total_cr  += r["cr"]  or 0
        total_cc  += r["cc"]  or 0
        total_turns += r["turns"]
        print(f"  {r['model']:<30}  turns={r['turns']:<4}  in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print(f"  {'TOTAL':<30}  turns={total_turns:<4}  in={fmt(total_inp):<8}  out={fmt(total_out):<8}  cost={fmt_cost(total_cost)}")
    print()
    print(f"  Sessions today:   {sessions['cnt']}")
    print(f"  Cache read:       {fmt(total_cr)}")
    print(f"  Cache creation:   {fmt(total_cc)}")
    hr()

    try:
        from insights import generate_pulse, format_pulse_cli
        pulse = generate_pulse(DB_PATH)
        pulse_str = format_pulse_cli(pulse)
        if pulse_str:
            print()
            print(pulse_str)
    except Exception:
        pass

    print()
    conn.close()


def cmd_stats():
    conn = require_db()
    conn.row_factory = sqlite3.Row

    totals = conn.execute("""
        SELECT
            SUM(total_input_tokens)   as inp,
            SUM(total_output_tokens)  as out,
            SUM(total_cache_read)     as cr,
            SUM(total_cache_creation) as cc,
            SUM(turn_count)           as turns,
            COUNT(*)                  as sessions,
            MIN(first_timestamp)      as first,
            MAX(last_timestamp)       as last
        FROM sessions
    """).fetchone()

    by_model = conn.execute("""
        SELECT
            COALESCE(model, 'unknown') as model,
            SUM(total_input_tokens)    as inp,
            SUM(total_output_tokens)   as out,
            SUM(total_cache_read)      as cr,
            SUM(total_cache_creation)  as cc,
            SUM(turn_count)            as turns,
            COUNT(*)                   as sessions
        FROM sessions
        GROUP BY model
        ORDER BY inp + out DESC
    """).fetchall()

    top_projects = conn.execute("""
        SELECT
            project_name,
            SUM(total_input_tokens)  as inp,
            SUM(total_output_tokens) as out,
            SUM(turn_count)          as turns,
            COUNT(*)                 as sessions
        FROM sessions
        GROUP BY project_name
        ORDER BY inp + out DESC
        LIMIT 5
    """).fetchall()

    daily_avg = conn.execute("""
        SELECT
            AVG(daily_inp) as avg_inp,
            AVG(daily_out) as avg_out,
            AVG(daily_cost) as avg_cost
        FROM (
            SELECT
                substr(timestamp, 1, 10) as day,
                SUM(input_tokens) as daily_inp,
                SUM(output_tokens) as daily_out,
                0.0 as daily_cost
            FROM turns
            WHERE timestamp >= datetime('now', '-30 days')
            GROUP BY day
        )
    """).fetchone()

    total_cost = sum(
        calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        for r in by_model
    )

    print()
    hr("=")
    print("  Claude Code Usage - All-Time Statistics")
    hr("=")

    first_date = (totals["first"] or "")[:10]
    last_date = (totals["last"] or "")[:10]
    print(f"  Period:           {first_date} to {last_date}")
    print(f"  Total sessions:   {totals['sessions'] or 0:,}")
    print(f"  Total turns:      {fmt(totals['turns'] or 0)}")
    print()
    print(f"  Input tokens:     {fmt(totals['inp'] or 0):<12}  (raw prompt tokens)")
    print(f"  Output tokens:    {fmt(totals['out'] or 0):<12}  (generated tokens)")
    print(f"  Cache read:       {fmt(totals['cr'] or 0):<12}  (90% cheaper than input)")
    print(f"  Cache creation:   {fmt(totals['cc'] or 0):<12}  (25% premium on input)")
    print()
    print(f"  Est. total cost:  ${total_cost:.4f}")
    hr()

    print("  By Model:")
    for r in by_model:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        print(f"    {r['model']:<30}  sessions={r['sessions']:<4}  turns={fmt(r['turns'] or 0):<6}  "
              f"in={fmt(r['inp'] or 0):<8}  out={fmt(r['out'] or 0):<8}  cost={fmt_cost(cost)}")

    hr()
    print("  Top Projects:")
    for r in top_projects:
        print(f"    {(r['project_name'] or 'unknown'):<40}  sessions={r['sessions']:<3}  "
              f"turns={fmt(r['turns'] or 0):<6}  tokens={fmt((r['inp'] or 0)+(r['out'] or 0))}")

    if daily_avg["avg_inp"]:
        hr()
        print("  Daily Average (last 30 days):")
        print(f"    Input:   {fmt(int(daily_avg['avg_inp'] or 0))}")
        print(f"    Output:  {fmt(int(daily_avg['avg_out'] or 0))}")

    hr("=")
    print()
    conn.close()


def cmd_live():
    """Real-time terminal monitor (updates every 2 s). Press Ctrl+C to stop."""
    import time
    _enable_windows_ansi()

    CLEAR = '\033[2J\033[H'
    BOLD  = '\033[1m'
    DIM   = '\033[2m'
    RST   = '\033[0m'
    W     = 66

    def sep(title=''):
        if title:
            bar = f'-- {title} ' + '-' * max(0, W - len(title) - 4)
            return f'  {bar}'
        return '  ' + '-' * W

    try:
        while True:
            if not DB_PATH.exists():
                print(f"{CLEAR}  Database not found. Run: python cli.py scan")
                time.sleep(3)
                continue

            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            today_str = date.today().isoformat()
            now       = datetime.now()

            today_rows = conn.execute("""
                SELECT COALESCE(model,'unknown') as model,
                       SUM(input_tokens) as inp, SUM(output_tokens) as out,
                       SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
                       COUNT(*) as turns, COUNT(DISTINCT session_id) as sessions
                FROM turns WHERE substr(timestamp,1,10)=?
                GROUP BY model ORDER BY inp+out DESC
            """, (today_str,)).fetchall()

            cut15 = (datetime.utcnow()-timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%S')
            cut1h = (datetime.utcnow()-timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S')
            b15 = conn.execute("SELECT SUM(input_tokens+output_tokens) as t FROM turns WHERE timestamp>=?", (cut15,)).fetchone()
            b1h = conn.execute("SELECT SUM(input_tokens+output_tokens) as t FROM turns WHERE timestamp>=?", (cut1h,)).fetchone()
            last = conn.execute("""
                SELECT t.timestamp, t.model, s.git_branch
                FROM turns t LEFT JOIN sessions s USING (session_id)
                ORDER BY t.timestamp DESC LIMIT 1
            """).fetchone()

            # Anomaly check
            anomaly_count = 0
            try:
                anomaly_count = conn.execute("""
                    SELECT COUNT(*) as cnt FROM anomalies
                    WHERE substr(detected_at, 1, 10) = ? AND acknowledged = 0
                """, (today_str,)).fetchone()["cnt"]
            except Exception:
                pass

            conn.close()

            t_inp = t_out = t_cr = t_cc = t_turns = t_sess = 0
            t_cost = 0.0
            for r in today_rows:
                inp=r['inp'] or 0; out=r['out'] or 0
                cr=r['cr'] or 0;   cc=r['cc'] or 0
                t_inp+=inp; t_out+=out; t_cr+=cr; t_cc+=cc
                t_turns+=r['turns']; t_sess+=r['sessions']
                t_cost+=calc_cost(r['model'], inp, out, cr, cc)

            burn15 = (b15['t'] or 0) / 15
            burn1h = (b1h['t'] or 0) / 60
            total_tok = max(t_inp + t_out, 1)
            cpt = t_cost / total_tok
            cost_hr = burn15 * 60 * cpt

            print(CLEAR, end='')
            print(f"  {BOLD}Claude Code Usage - Live{RST}  "
                  f"{DIM}{now.strftime('%Y-%m-%d  %H:%M:%S')}{RST}")
            print(sep())
            print()

            print(f"  Today ({today_str})")
            print(f"  {'MODEL':<32} {'INPUT':>9} {'OUTPUT':>9}  {'COST':>10}")
            print(f"  {'-'*32} {'-'*9} {'-'*9}  {'-'*10}")
            for r in today_rows:
                inp=r['inp'] or 0; out=r['out'] or 0
                cost=calc_cost(r['model'], inp, out, r['cr'] or 0, r['cc'] or 0)
                m = r['model'][:32] if len(r['model']) > 32 else r['model']
                print(f"  {m:<32} {fmt(inp):>9} {fmt(out):>9}  ${cost:>9.4f}")
            print(f"  {'-'*32} {'-'*9} {'-'*9}  {'-'*10}")
            print(f"  {'TOTAL':<32} {fmt(t_inp):>9} {fmt(t_out):>9}  ${t_cost:>9.4f}")
            print(f"  {DIM}{t_sess} sessions  |  {t_turns} turns  |  "
                  f"cache read {fmt(t_cr)}  cache write {fmt(t_cc)}{RST}")
            print()

            print(sep('Burn Rate'))
            print(f"  {'':32} {'15-min':>9} {'1-hour':>9}")
            print(f"  {'tokens/min':<32} {fmt(int(burn15)):>9} {fmt(int(burn1h)):>9}")
            if burn15 > 0:
                print(f"  {'est. cost/hour':<32} {'':>9} ${cost_hr:>8.4f}")
            print()

            if last:
                ts    = last['timestamp'][:19].replace('T', ' ')
                model = (last['model'] or '')[:28]
                branch = f"  [{last['git_branch']}]" if last['git_branch'] else ''
                print(sep('Last Turn'))
                print(f"  {ts}   {model}{branch}")
                print()

            if anomaly_count > 0:
                YELLOW = '\033[33m'
                print(f"  {YELLOW}! {anomaly_count} unacknowledged anomalie(s) detected today{RST}")
                print(f"  {DIM}Run: python cli.py anomalies{RST}")
                print()

            print(sep())
            print(f"  {DIM}Refreshing every 2s  |  Ctrl+C to stop{RST}")

            time.sleep(2)

    except KeyboardInterrupt:
        print("\n\n  Stopped.")


def cmd_forecast():
    """Burn-rate analysis and end-of-day cost projection."""
    if not DB_PATH.exists():
        print("Database not found. Run: python cli.py scan")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    today_str = date.today().isoformat()
    now       = datetime.now()

    hourly = conn.execute("""
        SELECT substr(timestamp,1,13) as hour,
               COALESCE(model,'unknown') as model,
               SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc
        FROM turns
        WHERE timestamp >= datetime('now','-24 hours')
        GROUP BY hour, model ORDER BY hour
    """).fetchall()

    today_rows = conn.execute("""
        SELECT COALESCE(model,'unknown') as model,
               SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc
        FROM turns WHERE substr(timestamp,1,10)=? GROUP BY model
    """, (today_str,)).fetchall()

    dow_rows = conn.execute("""
        SELECT strftime('%w', day) as dow, AVG(daily_tok) as avg_tok
        FROM (
            SELECT substr(timestamp,1,10) as day,
                   SUM(input_tokens+output_tokens) as daily_tok
            FROM turns
            WHERE timestamp >= datetime('now','-28 days')
            GROUP BY day
        ) GROUP BY dow ORDER BY dow
    """).fetchall()

    peak = conn.execute("""
        SELECT substr(timestamp,1,10) as day,
               SUM(input_tokens+output_tokens) as total
        FROM turns GROUP BY day ORDER BY total DESC LIMIT 1
    """).fetchone()

    conn.close()

    from collections import defaultdict
    hour_map: dict = defaultdict(lambda: {"inp":0,"out":0,"cr":0,"cc":0,"model":"unknown"})
    for r in hourly:
        h = hour_map[r["hour"]]
        h["inp"] += r["inp"] or 0; h["out"] += r["out"] or 0
        h["cr"]  += r["cr"]  or 0; h["cc"]  += r["cc"]  or 0
        h["model"] = r["model"]

    hours_sorted = sorted(hour_map.items())

    recent = hours_sorted[-2:] if len(hours_sorted) >= 2 else hours_sorted
    if recent:
        burn_tokens = sum(v["inp"]+v["out"] for _, v in recent)
        burn_per_min = burn_tokens / (len(recent) * 60)
    else:
        burn_per_min = 0.0

    def _avg_per_min(n_hours):
        window = hours_sorted[-n_hours:] if len(hours_sorted) >= n_hours else hours_sorted
        if not window:
            return 0.0
        return sum(v["inp"]+v["out"] for _, v in window) / (len(window) * 60)

    burn6h  = _avg_per_min(6)
    burn24h = _avg_per_min(24)

    t_inp=t_out=t_cr=t_cc = 0; t_cost = 0.0
    for r in today_rows:
        inp=r["inp"] or 0; out=r["out"] or 0
        cr=r["cr"] or 0;   cc=r["cc"] or 0
        t_inp+=inp; t_out+=out; t_cr+=cr; t_cc+=cc
        t_cost+=calc_cost(r["model"], inp, out, cr, cc)

    t_tokens = t_inp + t_out
    cpt      = t_cost / max(t_tokens, 1)

    hours_left     = max(0, 24 - now.hour - now.minute/60)
    proj_add_tok   = burn_per_min * 60 * hours_left
    proj_tok_total = t_tokens + proj_add_tok
    proj_cost      = t_cost   + proj_add_tok * cpt

    print()
    hr('=')
    print(f"  Claude Code Usage - Forecast           [{now.strftime('%Y-%m-%d  %H:%M')}]")
    hr('=')

    print()
    print(f"  Burn Rate")
    hr()
    print(f"  {'Last 2h avg:':<22} {fmt(int(burn_per_min)):>8}/min  "
          f"  est. ${burn_per_min*60*cpt:.4f}/hr")
    print(f"  {'Last 6h avg:':<22} {fmt(int(burn6h)):>8}/min")
    print(f"  {'Last 24h avg:':<22} {fmt(int(burn24h)):>8}/min")

    print()
    print(f"  Today's Projection  (at 2h-avg rate)")
    hr()
    print(f"  {'Spent so far:':<22} {fmt(t_tokens):>12} tokens    ${t_cost:.4f}")
    print(f"  {'Projected EOD:':<22} {fmt(int(proj_tok_total)):>12} tokens    ${proj_cost:.4f}")

    if DAILY_LIMIT_USD > 0:
        remaining = DAILY_LIMIT_USD - t_cost
        if burn_per_min > 0 and cpt > 0:
            mins_left = remaining / (burn_per_min * cpt)
            if mins_left > 0:
                eta = f"~{int(mins_left//60)}h {int(mins_left%60)}m at current rate"
            else:
                eta = "limit already exceeded"
        else:
            eta = "N/A (no recent activity)"
        print(f"  {'Daily limit:':<22} ${DAILY_LIMIT_USD:.2f}  (CLAUDE_USAGE_DAILY_LIMIT_USD)")
        print(f"  {'Remaining:':<22} ${max(remaining, 0):.4f}")
        print(f"  {'ETA to limit:':<22} {eta}")
    else:
        print(f"  Daily limit:           (not set - export CLAUDE_USAGE_DAILY_LIMIT_USD=10.00)")

    if dow_rows:
        print()
        DOW = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat']
        dow_data = {int(r["dow"]): int(r["avg_tok"] or 0) for r in dow_rows}
        print(f"  Weekly Pattern  (last 4 weeks avg)")
        hr()
        print('  ' + '  '.join(f"{DOW[d]:>7}" for d in range(7)))
        print('  ' + '  '.join(f"{fmt(dow_data.get(d,0)):>7}" for d in range(7)))

    if peak:
        print()
        print(f"  All-time Peak Day:  {peak['day']}  -  {fmt(peak['total'])} tokens")

    print()
    hr('=')
    print()


def cmd_export():
    """Export data to CSV, JSON, or SQLite snapshot."""
    import csv
    import json as json_mod

    # Parse arguments
    format_type = "csv"
    range_days = None
    output_path = None

    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--format" and i + 1 < len(args):
            format_type = args[i + 1].lower()
            i += 2
        elif args[i] == "--range" and i + 1 < len(args):
            val = args[i + 1]
            if val.endswith("d"):
                range_days = int(val[:-1])
            else:
                range_days = int(val)
            i += 2
        elif args[i] == "--output" and i + 1 < len(args):
            output_path = args[i + 1]
            i += 2
        else:
            i += 1

    if format_type == "sqlite":
        from archiver import create_snapshot
        result = create_snapshot(DB_PATH, Path(output_path) if output_path else None)
        if result["status"] == "created":
            print(f"SQLite snapshot created: {result['path']}")
            print(f"  Size: {result['size_bytes']:,} bytes (compressed)")
        else:
            print(f"Error: {result.get('message', 'unknown')}")
        return

    conn = require_db()
    conn.row_factory = sqlite3.Row

    query = """
        SELECT session_id, project_name, first_timestamp, last_timestamp,
               git_branch, model, turn_count,
               total_input_tokens, total_output_tokens,
               total_cache_read, total_cache_creation
        FROM sessions
    """
    params = []
    if range_days:
        cutoff = (date.today() - timedelta(days=range_days)).isoformat()
        query += " WHERE last_timestamp >= ?"
        params.append(cutoff)
    query += " ORDER BY last_timestamp DESC"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        print("No data to export.")
        return

    if format_type == "json":
        data = []
        for r in rows:
            row = dict(r)
            row["est_cost_usd"] = round(calc_cost(
                row["model"],
                row["total_input_tokens"] or 0, row["total_output_tokens"] or 0,
                row["total_cache_read"] or 0, row["total_cache_creation"] or 0,
            ), 6)
            data.append(row)

        out_path = output_path or "claude_usage_export.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json_mod.dump(data, f, indent=2, default=str)
        print(f"Exported {len(data)} sessions to {out_path}")

    else:  # csv
        out_path = output_path or "claude_usage_export.csv"
        fieldnames = [
            "session_id", "project_name", "first_timestamp", "last_timestamp",
            "git_branch", "model", "turn_count",
            "total_input_tokens", "total_output_tokens",
            "total_cache_read", "total_cache_creation", "est_cost_usd",
        ]
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                row = dict(r)
                row["est_cost_usd"] = round(calc_cost(
                    row["model"],
                    row["total_input_tokens"] or 0, row["total_output_tokens"] or 0,
                    row["total_cache_read"] or 0, row["total_cache_creation"] or 0,
                ), 6)
                writer.writerow(row)
        print(f"Exported {len(rows)} sessions to {out_path}")


def cmd_dashboard():
    import webbrowser
    import threading
    import time
    from config import DASHBOARD_PORT, SCAN_INTERVAL_SECS

    print("Running initial scan...")
    cmd_scan()

    def _bg_scan():
        from scanner import scan
        while True:
            time.sleep(SCAN_INTERVAL_SECS)
            scan(verbose=False)

    bg = threading.Thread(target=_bg_scan, daemon=True, name="bg-scanner")
    bg.start()
    print(f"Background scanner started (every {SCAN_INTERVAL_SECS}s).")

    print(f"\nStarting dashboard server on port {DASHBOARD_PORT}...")
    from dashboard import serve

    def open_browser():
        time.sleep(1.0)
        webbrowser.open(f"http://localhost:{DASHBOARD_PORT}")

    t = threading.Thread(target=open_browser, daemon=True)
    t.start()
    serve(port=DASHBOARD_PORT)


# ── NEW COMMANDS ──────────────────────────────────────────────────────────────

def cmd_query():
    """Execute analytics DSL queries against sessions."""
    query_str = " ".join(sys.argv[2:])
    if not query_str:
        print("Usage: python cli.py query <expression>")
        print('  Example: python cli.py query "model~sonnet AND tokens > 1M"')
        print('  Example: python cli.py query "project~my-app"')
        print('  Example: python cli.py query "cost > 0.50 AND date >= 2026-04-01"')
        print()
        print("Fields:  model, project, branch, session, date, user,")
        print("         tokens, input, output, cost, turns, cache_read, cache_creation")
        print("Ops:     =, !=, >, <, >=, <=, ~(contains)")
        print("Connect: AND, OR")
        print("Suffixes: K, M, B (e.g. 1M = 1,000,000)")
        return

    from query_engine import execute_query, format_results
    results = execute_query(query_str, DB_PATH)
    print(format_results(results))


def cmd_replay():
    """Replay a session's conversation timeline step by step."""
    if len(sys.argv) < 3:
        print("Usage: python cli.py replay <session_id_prefix>")
        print("  Shows step-by-step timeline of a session's turns.")
        return

    session_prefix = sys.argv[2]
    conn = require_db()
    conn.row_factory = sqlite3.Row

    # Find session by prefix
    session = conn.execute(
        "SELECT * FROM sessions WHERE session_id LIKE ?",
        (f"{session_prefix}%",)
    ).fetchone()

    if not session:
        print(f"No session found matching '{session_prefix}'")
        conn.close()
        return

    sid = session["session_id"]
    turns = conn.execute("""
        SELECT timestamp, model, input_tokens, output_tokens,
               cache_read_tokens, cache_creation_tokens, tool_name, cwd
        FROM turns WHERE session_id = ?
        ORDER BY timestamp ASC
    """, (sid,)).fetchall()
    conn.close()

    BOLD  = '\033[1m'
    DIM   = '\033[2m'
    CYAN  = '\033[36m'
    GREEN = '\033[32m'
    RST   = '\033[0m'

    print()
    hr('=')
    print(f"  {BOLD}Session Replay{RST}")
    hr('=')
    print(f"  Session:  {sid[:8]}...")
    print(f"  Project:  {session['project_name']}")
    print(f"  Branch:   {session['git_branch'] or '(none)'}")
    print(f"  Model:    {session['model']}")
    print(f"  Turns:    {session['turn_count']}")
    print(f"  Period:   {(session['first_timestamp'] or '')[:19]} -> "
          f"{(session['last_timestamp'] or '')[:19]}")
    hr()
    print()

    running_cost = 0.0
    running_tokens = 0

    for i, t in enumerate(turns, 1):
        ts = (t["timestamp"] or "")[:19].replace("T", " ")
        model = t["model"] or "unknown"
        inp = t["input_tokens"] or 0
        out = t["output_tokens"] or 0
        cr = t["cache_read_tokens"] or 0
        cc = t["cache_creation_tokens"] or 0
        tool = t["tool_name"] or ""
        cost = calc_cost(model, inp, out, cr, cc)
        running_cost += cost
        running_tokens += inp + out

        # Timeline visualization
        bar_len = min(40, max(1, (inp + out) // 5000))
        bar = "#" * bar_len

        print(f"  {CYAN}Turn {i:>3}{RST}  {DIM}{ts}{RST}")
        print(f"    Model: {model}")
        if tool:
            print(f"    Tool:  {GREEN}{tool}{RST}")
        print(f"    Input: {fmt(inp):<10}  Output: {fmt(out):<10}  Cost: {fmt_cost(cost)}")
        print(f"    Cache: read={fmt(cr)}  create={fmt(cc)}")
        print(f"    {DIM}{bar}{RST}")
        print(f"    {DIM}Running total: {fmt(running_tokens)} tokens  ${running_cost:.4f}{RST}")
        print()

    hr('=')
    print(f"  Replay complete: {len(turns)} turns  |  "
          f"{fmt(running_tokens)} tokens  |  ${running_cost:.4f}")
    hr('=')
    print()


def cmd_branches():
    """Compare usage across git branches."""
    conn = require_db()
    conn.row_factory = sqlite3.Row

    days = 30
    if len(sys.argv) > 2:
        try:
            days = int(sys.argv[2].rstrip("d"))
        except ValueError:
            pass

    cutoff = (date.today() - timedelta(days=days)).isoformat()

    rows = conn.execute("""
        SELECT COALESCE(git_branch, '(none)') as branch,
               SUM(total_input_tokens) as inp,
               SUM(total_output_tokens) as out,
               SUM(total_cache_read) as cr,
               SUM(total_cache_creation) as cc,
               SUM(turn_count) as turns,
               COUNT(*) as sessions,
               GROUP_CONCAT(DISTINCT project_name) as projects
        FROM sessions
        WHERE last_timestamp >= ?
        GROUP BY branch
        ORDER BY inp + out DESC
    """, (cutoff,)).fetchall()
    conn.close()

    print()
    hr('=')
    print(f"  Git Branch Usage  (last {days} days)")
    hr('=')

    if not rows:
        print("  No branch data found.")
        print()
        return

    print(f"\n  {'BRANCH':<30} {'TOKENS':>12} {'COST':>10} {'TURNS':>6} {'SESSIONS':>8}")
    print(f"  {'-'*30} {'-'*12} {'-'*10} {'-'*6} {'-'*8}")

    total_cost = 0
    for r in rows:
        tokens = (r["inp"] or 0) + (r["out"] or 0)
        cost = calc_cost("default", r["inp"] or 0, r["out"] or 0,
                         r["cr"] or 0, r["cc"] or 0)
        total_cost += cost
        branch = r["branch"][:28]
        print(f"  {branch:<30} {fmt(tokens):>12} {fmt_cost(cost):>10} "
              f"{r['turns'] or 0:>6} {r['sessions']:>8}")

    hr()
    print(f"  Total estimated cost: {fmt_cost(total_cost)}")

    # Find the most expensive branch
    if rows:
        top = rows[0]
        top_cost = calc_cost("default", top["inp"] or 0, top["out"] or 0,
                             top["cr"] or 0, top["cc"] or 0)
        projs = (top["projects"] or "").split(",")[:3]
        print(f"  Most expensive: {top['branch']} (${top_cost:.4f})")
        if projs:
            print(f"    Projects: {', '.join(projs)}")

    print()
    hr('=')
    print()


def cmd_optimize():
    """Run cost optimization analysis."""
    from optimizer import analyze, format_report

    days = 30
    if len(sys.argv) > 2:
        try:
            days = int(sys.argv[2].rstrip("d"))
        except ValueError:
            pass

    analysis = analyze(DB_PATH, days=days)
    print(format_report(analysis))


def cmd_anomalies():
    """View detected usage anomalies."""
    from anomaly import get_recent_anomalies, detect_anomalies

    # Run a fresh detection
    detect_anomalies(DB_PATH)

    days = 7
    if len(sys.argv) > 2:
        try:
            days = int(sys.argv[2].rstrip("d"))
        except ValueError:
            pass

    anomalies = get_recent_anomalies(DB_PATH, days=days)

    print()
    hr('=')
    print(f"  Anomaly Detection Report  (last {days} days)")
    hr('=')

    if not anomalies:
        print("  No anomalies detected.")
        print()
        return

    severity_colors = {"critical": '\033[31m', "warning": '\033[33m', "info": '\033[36m'}
    RST = '\033[0m'

    for a in anomalies:
        color = severity_colors.get(a["severity"], '')
        ack = " [ACK]" if a.get("acknowledged") else ""
        print(f"\n  {color}[{a['severity'].upper()}]{RST}{ack}  {a['detected_at'][:16]}")
        print(f"    {a['message']}")
        print(f"    Metric: {a['metric']}  Value: {a['value']}  "
              f"Baseline: {a['baseline']}  Factor: {a['factor']}x")

    print()
    hr('=')
    print(f"  {len(anomalies)} anomalie(s) found")
    hr('=')
    print()


def cmd_archive():
    """Manage data archives."""
    require_role("admin")
    from archiver import (get_archivable_months, archive_month,
                          list_archives, restore_archive)

    subcmd = sys.argv[2] if len(sys.argv) > 2 else "status"

    if subcmd == "status":
        archives = list_archives()
        archivable = get_archivable_months(DB_PATH)

        print()
        hr('=')
        print("  Archive Status")
        hr('=')

        if archives:
            print(f"\n  Existing Archives:")
            for a in archives:
                print(f"    {a['month']}  {a['size_human']:>10}  "
                      f"turns={a['turns']}  archived={a['archived_at'][:10] if isinstance(a['archived_at'], str) else '?'}")
        else:
            print("\n  No archives found.")

        if archivable:
            print(f"\n  Archivable months: {', '.join(archivable)}")
            print(f"  Run: python cli.py archive run")
        else:
            print("\n  No months ready for archival (keeping last 2 months live).")

        print()

    elif subcmd == "run":
        archivable = get_archivable_months(DB_PATH)
        if not archivable:
            print("No months to archive.")
            return

        for month in archivable:
            print(f"Archiving {month}...")
            result = archive_month(month, DB_PATH)
            if result["status"] == "archived":
                print(f"  Archived: {result['turns']} turns, "
                      f"{result['sessions']} sessions -> {result['archive_path']}")
            else:
                print(f"  Skipped: {result.get('reason', 'unknown')}")

    elif subcmd == "restore" and len(sys.argv) > 3:
        month = sys.argv[3]
        print(f"Restoring {month}...")
        result = restore_archive(month, DB_PATH)
        if result["status"] == "restored":
            print(f"  Restored: {result['turns_restored']} turns, "
                  f"{result['sessions_restored']} sessions")
        else:
            print(f"  Error: {result.get('message', 'unknown')}")

    elif subcmd == "snapshot":
        from archiver import create_snapshot
        result = create_snapshot(DB_PATH)
        if result["status"] == "created":
            print(f"Snapshot created: {result['path']}")
            print(f"  Size: {result['size_bytes']:,} bytes")
        else:
            print(f"Error: {result.get('message')}")

    else:
        print("Usage: python cli.py archive [status|run|restore <month>|snapshot]")


def cmd_timetravel():
    """Query historical database state at a specific point in time."""
    if len(sys.argv) < 3:
        print("Usage: python cli.py timetravel <datetime>")
        print('  Example: python cli.py timetravel "2026-04-01 10:00"')
        print("  Example: python cli.py timetravel 2026-04-01")
        return

    at = " ".join(sys.argv[2:]).strip('"').strip("'")
    from archiver import time_travel_query

    print(f"\n  Reconstructing state as of: {at}")
    result = time_travel_query(at, DB_PATH)

    print()
    hr('=')
    print(f"  Time Travel  - State at {result['as_of'][:16]}")
    hr('=')

    print(f"\n  Sources queried: {', '.join(result['sources'])}")
    print(f"  Total tokens:    {fmt(result['total_tokens'])}")
    print(f"  Total input:     {fmt(result['total_input'])}")
    print(f"  Total output:    {fmt(result['total_output'])}")
    print(f"  Total turns:     {result['total_turns']:,}")
    print(f"  Total sessions:  {result['total_sessions']:,}")
    print(f"  Est. total cost: ${result['total_cost']:.4f}")

    if result["by_model"]:
        print(f"\n  By Model:")
        for model, data in sorted(result["by_model"].items(),
                                   key=lambda x: x[1]["turns"], reverse=True):
            print(f"    {model:<30}  turns={data['turns']:<6}  "
                  f"tokens={fmt(data['input']+data['output']):<10}  cost=${data['cost']:.4f}")

    if result["by_project"]:
        print(f"\n  Top Projects:")
        for proj, data in sorted(result["by_project"].items(),
                                  key=lambda x: x[1]["tokens"], reverse=True)[:5]:
            print(f"    {proj:<35}  tokens={fmt(data['tokens']):<10}  sessions={data['sessions']}")

    print()
    hr('=')
    print()


def cmd_api():
    """Start the REST API server."""
    require_role("admin")
    from api_server import serve
    from config import API_PORT

    import threading
    import time
    from config import SCAN_INTERVAL_SECS

    # Run an initial scan
    print("Running initial scan...")
    cmd_scan()

    # Background scanner
    def _bg_scan():
        from scanner import scan
        while True:
            time.sleep(SCAN_INTERVAL_SECS)
            scan(verbose=False)

    bg = threading.Thread(target=_bg_scan, daemon=True, name="bg-scanner")
    bg.start()

    serve(port=API_PORT)


def cmd_daemon():
    """Manage the background scanner daemon."""
    from daemon import start, stop, is_running, get_log

    subcmd = sys.argv[2] if len(sys.argv) > 2 else "status"

    if subcmd == "start":
        require_role("admin")
        foreground = "--foreground" in sys.argv or "-f" in sys.argv
        interval = SCAN_INTERVAL_SECS

        for i, arg in enumerate(sys.argv):
            if arg == "--interval" and i + 1 < len(sys.argv):
                interval = int(sys.argv[i + 1])

        result = start(foreground=foreground, interval=interval)
        if not foreground and result.get("running"):
            print(f"Daemon started (PID {result['pid']})")

    elif subcmd == "stop":
        require_role("admin")
        result = stop()
        if result.get("stopped"):
            print(f"Daemon stopped (PID {result['pid']})")
        else:
            print(result.get("message", "Failed to stop daemon"))

    elif subcmd == "status":
        status = is_running()
        if status["running"]:
            print(f"Daemon is running (PID {status['pid']})")
        else:
            print("Daemon is not running")

    elif subcmd == "log":
        lines = int(sys.argv[3]) if len(sys.argv) > 3 else 20
        log_lines = get_log(lines)
        if log_lines:
            for line in log_lines:
                print(f"  {line}")
        else:
            print("No daemon logs found.")

    else:
        print("Usage: python cli.py daemon [start|stop|status|log]")
        print("  start --foreground  Run in foreground")
        print("  start --interval N  Scan every N seconds")


def cmd_users():
    """Multi-user management."""
    from config import ACTIVE_USER, USERS_FILE

    subcmd = sys.argv[2] if len(sys.argv) > 2 else "list"

    conn = require_db()
    conn.row_factory = sqlite3.Row

    if subcmd == "list":
        rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()

        # Per-user stats
        stats = conn.execute("""
            SELECT user_id,
                   SUM(input_tokens + output_tokens) as tokens,
                   COUNT(*) as turns,
                   COUNT(DISTINCT session_id) as sessions
            FROM turns GROUP BY user_id
        """).fetchall()
        stats_map = {r["user_id"]: dict(r) for r in stats}

        print()
        hr('=')
        print(f"  Users  (active: {ACTIVE_USER})")
        hr('=')

        for r in rows:
            s = stats_map.get(r["user_id"], {})
            active = " *" if r["user_id"] == ACTIVE_USER else ""
            print(f"\n  {r['user_id']}{active}")
            print(f"    Display name: {r['display_name']}")
            print(f"    Role:         {r['role']}")
            print(f"    Created:      {r['created_at']}")
            print(f"    Tokens:       {fmt(s.get('tokens', 0) or 0)}")
            print(f"    Turns:        {s.get('turns', 0) or 0}")
            print(f"    Sessions:     {s.get('sessions', 0) or 0}")

        print()
        hr('=')
        print()

    elif subcmd in ("add", "create") and len(sys.argv) > 3:
        user_id = sys.argv[3]
        display_name = sys.argv[4] if len(sys.argv) > 4 else user_id
        role = sys.argv[5] if len(sys.argv) > 5 else "admin"

        try:
            conn.execute(
                "INSERT INTO users (user_id, display_name, role) VALUES (?, ?, ?)",
                (user_id, display_name, role)
            )
            conn.commit()
            print(f"User '{user_id}' created (role: {role})")
            print(f"Switch with: set CLAUDE_USAGE_USER={user_id}")
        except sqlite3.IntegrityError:
            print(f"User '{user_id}' already exists")

    elif subcmd == "switch" and len(sys.argv) > 3:
        user_id = sys.argv[3]
        existing = conn.execute("SELECT * FROM users WHERE user_id = ?",
                                (user_id,)).fetchone()
        if existing:
            print(f"To switch user, set the environment variable:")
            print(f"  set CLAUDE_USAGE_USER={user_id}  (Windows)")
            print(f"  export CLAUDE_USAGE_USER={user_id}  (Linux/Mac)")
        else:
            print(f"User '{user_id}' not found. Create with: python cli.py users add {user_id}")

    elif subcmd == "role" and len(sys.argv) > 4:
        user_id = sys.argv[3]
        new_role = sys.argv[4]
        if new_role not in ("admin", "viewer"):
            print("Valid roles: admin, viewer")
        else:
            conn.execute("UPDATE users SET role = ? WHERE user_id = ?",
                         (new_role, user_id))
            conn.commit()
            print(f"User '{user_id}' role set to '{new_role}'")

    else:
        print("Usage: python cli.py users [list|add <id> [name] [role]|switch <id>|role <id> <role>]")

    conn.close()


def cmd_plugins():
    """Plugin management."""
    from plugins import discover_plugins, load_plugins, list_loaded, create_plugin_scaffold
    from config import PLUGINS_DIR

    subcmd = sys.argv[2] if len(sys.argv) > 2 else "list"

    if subcmd == "list":
        load_plugins(verbose=True)
        loaded = list_loaded()

        print()
        hr('=')
        print(f"  Plugins  (dir: {PLUGINS_DIR})")
        hr('=')

        if not loaded:
            discovered = discover_plugins()
            if discovered:
                print(f"\n  Found {len(discovered)} plugin(s) but none loaded:")
                for p in discovered:
                    print(f"    {p['name']}  ({p['type']})  {p['path']}")
            else:
                print("\n  No plugins found.")
                print(f"  Create one with: python cli.py plugins create my_plugin")
        else:
            for p in loaded:
                print(f"\n  {p['name']} v{p['version']}")
                print(f"    {p['description']}")
                if p['author']:
                    print(f"    Author: {p['author']}")
                print(f"    Hooks:  {', '.join(p['hooks']) or 'none'}")
                print(f"    Path:   {p['path']}")

        print()
        hr('=')
        print()

    elif subcmd == "create" and len(sys.argv) > 3:
        name = sys.argv[3]
        try:
            path = create_plugin_scaffold(name)
            print(f"Plugin scaffold created: {path}")
            print(f"Edit {path / '__init__.py'} to customize.")
        except FileExistsError as e:
            print(str(e))

    else:
        print("Usage: python cli.py plugins [list|create <name>]")


def cmd_graph():
    """Dependency graph of projects/branches/models based on session usage.

    Usage:
      python cli.py graph [days] [--format mermaid|tree|json]
    """
    conn = require_db()
    conn.row_factory = sqlite3.Row
    days = 30
    output_format = "mermaid"
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--format" and i + 1 < len(args):
            output_format = args[i + 1].lower()
            i += 2
            continue
        if a.startswith("--format="):
            output_format = a.split("=", 1)[1].lower()
            i += 1
            continue
        try:
            days = int(a.rstrip("d"))
        except ValueError:
            pass
        i += 1
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute("""
        SELECT COALESCE(project_name, 'unknown') as project,
               COALESCE(git_branch, '(none)') as branch,
               COALESCE(model, 'unknown') as model,
               SUM(turn_count) as turns,
               SUM(total_input_tokens + total_output_tokens) as tokens
        FROM sessions
        WHERE last_timestamp >= ?
        GROUP BY project, branch, model
        ORDER BY tokens DESC
        LIMIT 150
    """, (cutoff,)).fetchall()
    conn.close()

    if not rows:
        print(f"No graph data for last {days} days.")
        return

    rows = [r for r in rows if (r["tokens"] or 0) > 0 or (r["turns"] or 0) > 0]
    if not rows:
        print(f"No non-empty graph data for last {days} days.")
        return

    if output_format == "json":
        import json as _json
        payload = [{
            "project": r["project"],
            "branch": r["branch"],
            "model": r["model"],
            "turns": int(r["turns"] or 0),
            "tokens": int(r["tokens"] or 0),
        } for r in rows]
        print(_json.dumps(payload, indent=2))
        return

    print()
    hr("=")
    print(f"  Dependency Graph (projects -> branches -> models) last {days} days")
    hr("=")

    if output_format == "tree":
        proj_map = {}
        for r in rows:
            p = r["project"]
            b = r["branch"]
            m = r["model"]
            proj_map.setdefault(p, {}).setdefault(b, []).append(r)
        for project, branch_map in proj_map.items():
            print(f"  {project}")
            for branch, leaf_rows in branch_map.items():
                b_tokens = sum(int(x["tokens"] or 0) for x in leaf_rows)
                print(f"    |- {branch}  [{b_tokens:,} tok]")
                for x in leaf_rows:
                    print(f"       |- {x['model']}  ({int(x['turns'] or 0)} turns)")
        print()
        hr("=")
        print()
        return

    if output_format != "mermaid":
        print(f"Unknown format '{output_format}'. Use: mermaid, tree, json")
        return

    print("  Mermaid format (paste into mermaid.live):")
    print()
    print("graph TD")

    def norm(label: str) -> str:
        return "".join(ch if ch.isalnum() else "_" for ch in label)[:42]

    seen_nodes = set()
    for r in rows:
        p_label = r["project"]
        b_label = r["branch"]
        m_label = r["model"]
        p = f"P_{norm(p_label)}"
        b = f"B_{norm(p_label + '_' + b_label)}"
        m = f"M_{norm(m_label)}"
        if p not in seen_nodes:
            print(f'    {p}["{p_label}"]')
            seen_nodes.add(p)
        if b not in seen_nodes:
            print(f'    {b}["{b_label}"]')
            seen_nodes.add(b)
        if m not in seen_nodes:
            print(f'    {m}["{m_label}"]')
            seen_nodes.add(m)
        print(f"    {p} -->|{int(r['tokens'] or 0):,} tok| {b}")
        print(f"    {b} -->|{int(r['turns'] or 0)} turns| {m}")

    print()
    hr("=")
    print()


def cmd_sync():
    """Cross-machine data synchronization."""
    from sync import export_sync, import_sync

    subcmd = sys.argv[2] if len(sys.argv) > 2 else "help"

    if subcmd in ("export", "--export"):
        since = None
        output = None
        args = sys.argv[3:]
        i = 0
        while i < len(args):
            if args[i] == "--since" and i + 1 < len(args):
                since = args[i + 1]
                i += 2
            elif args[i] == "--output" and i + 1 < len(args):
                output = args[i + 1]
                i += 2
            else:
                i += 1
        result = export_sync(output_path=Path(output) if output else None, since=since)
        if result["status"] == "exported":
            print(f"Exported sync file: {result['path']}")
            print(f"  Turns: {result['turns']}  Sessions: {result['sessions']}")
            print(f"  Size: {result['size_bytes']:,} bytes")
        else:
            print(f"Error: {result.get('message')}")

    elif subcmd in ("import", "--import"):
        if len(sys.argv) < 4:
            print("Usage: python cli.py sync import <path_to_sync_file>")
            return
        sync_path = Path(sys.argv[3])
        result = import_sync(sync_path)
        if result["status"] == "imported":
            print(f"Import complete from {result['source_host']}")
            print(f"  Turns imported: {result['turns_imported']}")
            print(f"  Turns skipped (duplicates): {result['turns_skipped']}")
            print(f"  Sessions imported: {result['sessions_imported']}")
        else:
            print(f"Error: {result.get('message')}")

    else:
        print("Usage: python cli.py sync [export|import]")
        print("  export [--since DATE] [--output PATH]  Export sync file")
        print("  import <sync_file>                     Import from sync file")


def cmd_tag():
    """Manual tagging and cost allocation."""
    conn = require_db()
    conn.row_factory = sqlite3.Row

    subcmd = sys.argv[2] if len(sys.argv) > 2 else "list"

    if subcmd == "list":
        try:
            rows = conn.execute("""
                SELECT tag_name, COUNT(*) as cnt,
                       SUM(s.total_input_tokens + s.total_output_tokens) as tokens
                FROM tags t JOIN sessions s ON t.session_id = s.session_id
                GROUP BY tag_name ORDER BY cnt DESC
            """).fetchall()
        except Exception:
            rows = []

        print()
        hr('=')
        print("  Tags & Cost Allocation")
        hr('=')
        if not rows:
            print("  No tags found. Use: python cli.py tag add <session_prefix> <tag_name>")
        else:
            for r in rows:
                print(f"  {r['tag_name']:<25}  sessions={r['cnt']:<4}  tokens={fmt(r['tokens'] or 0)}")
        print()

    elif subcmd == "add" and len(sys.argv) > 4:
        session_prefix = sys.argv[3]
        tag_name = sys.argv[4]
        sess = conn.execute("SELECT session_id FROM sessions WHERE session_id LIKE ?",
                          (f"{session_prefix}%",)).fetchone()
        if not sess:
            print(f"No session found matching '{session_prefix}'")
        else:
            try:
                conn.execute("INSERT OR IGNORE INTO tags (session_id, tag_name) VALUES (?, ?)",
                           (sess["session_id"], tag_name))
                conn.commit()
                print(f"Tagged session {sess['session_id'][:8]}... as '{tag_name}'")
            except Exception as e:
                print(f"Error: {e}")

    elif subcmd == "remove" and len(sys.argv) > 4:
        session_prefix = sys.argv[3]
        tag_name = sys.argv[4]
        conn.execute("DELETE FROM tags WHERE session_id LIKE ? AND tag_name = ?",
                    (f"{session_prefix}%", tag_name))
        conn.commit()
        print(f"Removed tag '{tag_name}' from matching sessions")

    elif subcmd == "sessions" and len(sys.argv) > 3:
        tag_name = sys.argv[3]
        rows = conn.execute("""
            SELECT s.session_id, s.project_name, s.model, s.turn_count,
                   s.total_input_tokens, s.total_output_tokens
            FROM tags t JOIN sessions s ON t.session_id = s.session_id
            WHERE t.tag_name = ?
            ORDER BY s.last_timestamp DESC
        """, (tag_name,)).fetchall()

        print(f"\n  Sessions tagged '{tag_name}': {len(rows)}")
        hr()
        total_cost = 0
        for r in rows:
            cost = calc_cost(r["model"], r["total_input_tokens"] or 0,
                           r["total_output_tokens"] or 0, 0, 0)
            total_cost += cost
            print(f"  {r['session_id'][:8]}  {(r['project_name'] or 'unknown')[:25]:<25}  "
                  f"{(r['model'] or 'unknown')[:20]:<20}  ${cost:.4f}")
        hr()
        print(f"  Total cost: ${total_cost:.4f}")
        print()

    else:
        print("Usage: python cli.py tag [list|add|remove|sessions]")
        print("  list                            List all tags")
        print("  add <session_prefix> <tag>      Tag a session")
        print("  remove <session_prefix> <tag>   Remove tag from session")
        print("  sessions <tag>                  List sessions with tag")

    conn.close()


def cmd_simulate():
    """What-if pricing simulator."""
    import json as json_mod
    from config import PRICING, calc_cost_with_pricing

    if len(sys.argv) < 3:
        print("Usage: python cli.py simulate <pricing_json_file> [--days N]")
        print('  Example: python cli.py simulate haiku_pricing.json --days 30')
        print("\n  JSON format: same as PRICING in config.py")
        print("  Current pricing:")
        for model, p in PRICING.items():
            if model != "default":
                print(f"    {model}: input=${p['input']}/M  output=${p['output']}/M")
        return

    pricing_path = Path(sys.argv[2])
    days = 30
    for i, arg in enumerate(sys.argv):
        if arg == "--days" and i + 1 < len(sys.argv):
            days = int(sys.argv[i + 1])

    if not pricing_path.exists():
        print(f"File not found: {pricing_path}")
        return

    custom_pricing = json_mod.loads(pricing_path.read_text(encoding="utf-8"))

    conn = require_db()
    conn.row_factory = sqlite3.Row
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') as model,
               SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc
        FROM turns WHERE substr(timestamp, 1, 10) >= ?
        GROUP BY model ORDER BY inp + out DESC
    """, (cutoff,)).fetchall()
    conn.close()

    print()
    hr('=')
    print(f"  What-If Pricing Simulator  (last {days} days)")
    hr('=')

    total_actual = total_sim = 0
    print(f"\n  {'MODEL':<30} {'ACTUAL':>10} {'SIMULATED':>10} {'DELTA':>10}")
    print(f"  {'-'*30} {'-'*10} {'-'*10} {'-'*10}")

    for r in rows:
        actual = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
        simulated = calc_cost_with_pricing(custom_pricing, r["model"],
                                           r["inp"] or 0, r["out"] or 0,
                                           r["cr"] or 0, r["cc"] or 0)
        delta = simulated - actual
        total_actual += actual
        total_sim += simulated
        print(f"  {r['model'][:28]:<30} ${actual:>9.4f} ${simulated:>9.4f} ${delta:>+9.4f}")

    hr()
    delta_total = total_sim - total_actual
    print(f"  {'TOTAL':<30} ${total_actual:>9.4f} ${total_sim:>9.4f} ${delta_total:>+9.4f}")
    pct = (delta_total / max(total_actual, 0.0001)) * 100
    print(f"  Change: {pct:+.1f}% ({'savings' if delta_total < 0 else 'increase'})")
    hr('=')
    print()


def cmd_rollup():
    """Manual data retention rollup."""
    require_role("admin")
    from scanner import rollup_old_data
    from config import RETENTION_DAYS

    days = RETENTION_DAYS
    if len(sys.argv) > 2:
        try:
            days = int(sys.argv[2].rstrip("d"))
        except ValueError:
            pass

    print(f"Rolling up data older than {days} days...")
    result = rollup_old_data(DB_PATH, days)
    print(f"  Rolled up: {result['rolled_up_days']} day-groups")
    print(f"  Deleted turns: {result['deleted_turns']}")
    print(f"  Cutoff date: {result['cutoff']}")


def cmd_invoice():
    """Generate standalone HTML invoice/report."""
    from invoice import generate_invoice

    args = sys.argv[2:]
    project = None
    date_from = None
    date_to = None
    client = None
    output = None
    tag = None

    i = 0
    while i < len(args):
        if args[i] == "--project" and i + 1 < len(args):
            project = args[i + 1]; i += 2
        elif args[i] == "--from" and i + 1 < len(args):
            date_from = args[i + 1]; i += 2
        elif args[i] == "--to" and i + 1 < len(args):
            date_to = args[i + 1]; i += 2
        elif args[i] == "--client" and i + 1 < len(args):
            client = args[i + 1]; i += 2
        elif args[i] == "--output" and i + 1 < len(args):
            output = args[i + 1]; i += 2
        elif args[i] == "--tag" and i + 1 < len(args):
            tag = args[i + 1]; i += 2
        else:
            i += 1

    result = generate_invoice(DB_PATH, project=project, date_from=date_from,
                             date_to=date_to, client_name=client,
                             output_path=Path(output) if output else None,
                             tag=tag)

    if result["status"] == "created":
        print(f"Invoice generated: {result['path']}")
        print(f"  Sessions: {result['sessions']}  Total cost: ${result['total_cost']:.4f}")
    else:
        print(f"Error: {result.get('message')}")


def cmd_tui():
    """Interactive terminal user interface."""
    from tui import run_tui
    run_tui(DB_PATH)


def cmd_circuit_breaker():
    """Circuit breaker quota enforcer."""
    from circuit_breaker import check_circuit_breaker, get_status, unblock_claude_binary
    from config import CIRCUIT_BREAKER_ACTION

    subcmd = sys.argv[2] if len(sys.argv) > 2 else "status"

    if subcmd == "status":
        status = get_status(DB_PATH)
        print()
        hr()
        print("  Circuit Breaker Status")
        hr()
        print(f"  Enabled:    {'Yes' if status['enabled'] else 'No (set CLAUDE_USAGE_DAILY_LIMIT_USD)'}")
        print(f"  Today cost: ${status['today_cost']:.4f}")
        print(f"  Limit:      ${status['limit']:.2f}")
        print(f"  Used:       {status['pct_used']:.1f}%")
        print(f"  Tripped:    {'YES' if status['tripped'] else 'No'}")
        hr()
        print()

    elif subcmd == "check":
        action = CIRCUIT_BREAKER_ACTION
        if len(sys.argv) > 3:
            action = sys.argv[3]
        result = check_circuit_breaker(DB_PATH, action)
        if result["tripped"]:
            print(f"  TRIPPED: {result.get('message', '')}")
        else:
            print(f"  OK: ${result.get('today_cost', 0):.4f} / ${result.get('limit', 0):.2f}")

    elif subcmd == "unblock":
        if unblock_claude_binary():
            print("  Claude binary unblocked.")
        else:
            print("  No blocked binary found.")

    else:
        print("Usage: python cli.py breaker [status|check [action]|unblock]")


def cmd_redact_export():
    """Export with redacted/anonymized project names and paths."""
    import csv
    import json as json_mod
    import hashlib

    format_type = "csv"
    range_days = None
    output_path = None

    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--format" and i + 1 < len(args):
            format_type = args[i + 1].lower(); i += 2
        elif args[i] == "--range" and i + 1 < len(args):
            val = args[i + 1]
            range_days = int(val.rstrip("d")); i += 2
        elif args[i] == "--output" and i + 1 < len(args):
            output_path = args[i + 1]; i += 2
        else:
            i += 1

    conn = require_db()
    conn.row_factory = sqlite3.Row

    query = """SELECT session_id, project_name, first_timestamp, last_timestamp,
               git_branch, model, turn_count, total_input_tokens, total_output_tokens,
               total_cache_read, total_cache_creation FROM sessions"""
    params = []
    if range_days:
        cutoff = (date.today() - timedelta(days=range_days)).isoformat()
        query += " WHERE last_timestamp >= ?"
        params.append(cutoff)
    query += " ORDER BY last_timestamp DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        print("No data to export.")
        return

    def _redact(val):
        if not val:
            return val
        h = hashlib.sha256(val.encode()).hexdigest()[:4].upper()
        return f"REDACTED_{h}"

    data = []
    for r in rows:
        row = dict(r)
        row["project_name"] = _redact(row["project_name"])
        row["git_branch"] = _redact(row["git_branch"]) if row["git_branch"] else ""
        row["est_cost_usd"] = round(calc_cost(
            row["model"], row["total_input_tokens"] or 0, row["total_output_tokens"] or 0,
            row["total_cache_read"] or 0, row["total_cache_creation"] or 0), 6)
        data.append(row)

    if format_type == "json":
        out_path = output_path or "claude_usage_redacted.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json_mod.dump(data, f, indent=2, default=str)
    else:
        out_path = output_path or "claude_usage_redacted.csv"
        fieldnames = list(data[0].keys())
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in data:
                writer.writerow(row)

    print(f"Exported {len(data)} redacted sessions to {out_path}")


def cmd_search():
    """Full-text search across sessions."""
    query = " ".join(sys.argv[2:])
    if not query:
        print("Usage: python cli.py search <query>")
        print("  Searches project names, branches, models, and session IDs")
        return

    from scanner import search_sessions_fts
    results = search_sessions_fts(query, DB_PATH)

    if not results:
        # Fallback to LIKE
        conn = require_db()
        conn.row_factory = sqlite3.Row
        results = [dict(r) for r in conn.execute("""
            SELECT session_id, project_name, git_branch, model, turn_count,
                   total_input_tokens, total_output_tokens, last_timestamp
            FROM sessions
            WHERE project_name LIKE ? OR git_branch LIKE ? OR model LIKE ?
            ORDER BY last_timestamp DESC LIMIT 30
        """, (f"%{query}%", f"%{query}%", f"%{query}%")).fetchall()]
        conn.close()

    print(f"\n  Search results for '{query}': {len(results)} found\n")
    print(f"  {'SESSION':<10} {'PROJECT':<30} {'MODEL':<22} {'TURNS':>6} {'LAST':>12}")
    hr()
    for r in results:
        sid = (r.get("session_id", "") or "")[:8]
        proj = (r.get("project_name", "") or "unknown")[:28]
        model = (r.get("model", "") or "unknown")[:20]
        turns = r.get("turn_count", 0) or 0
        last = (r.get("last_timestamp", "") or "")[:10]
        print(f"  {sid:<10} {proj:<30} {model:<22} {turns:>6} {last:>12}")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

USAGE = """
Claude Code Usage - Local AI Observability Platform

Usage:
  python cli.py scan        Scan JSONL files and update database
  python cli.py today       Show today's usage summary
  python cli.py stats       Show all-time statistics
  python cli.py live        Real-time terminal monitor (Ctrl+C to stop)
  python cli.py watch       Alias for live
  python cli.py forecast    Burn-rate analysis and end-of-day projection
  python cli.py export      Export sessions (--format csv|json|sqlite --range 7d)
  python cli.py dashboard   Scan + start dashboard (port: CLAUDE_USAGE_PORT)

  python cli.py query       Run analytics DSL queries (e.g. "model~sonnet AND tokens>1M")
  python cli.py replay      Session replay with step-by-step timeline
  python cli.py branches    Git branch usage comparison
  python cli.py optimize    Cost optimization analysis and suggestions
  python cli.py anomalies   View detected usage anomalies

  python cli.py archive     Data archival (archive status|run|restore|snapshot)
  python cli.py timetravel  Query past DB state (e.g. "2026-04-01 10:00")

  python cli.py api         Start the REST API server (port: CLAUDE_USAGE_API_PORT)
  python cli.py daemon      Background scanner (daemon start|stop|status|log)

  python cli.py users       Multi-user management (users list|add|switch|role)
  python cli.py plugins     Plugin management (plugins list|create)
  python cli.py graph       Dependency graph (projects -> branches -> models)
                            optional: --format mermaid|tree|json

  python cli.py sync        Cross-machine sync (sync export|import)
  python cli.py tag         Manual tagging (tag list|add|remove|sessions)
  python cli.py simulate    What-if pricing simulator
  python cli.py rollup      Data retention rollup
  python cli.py invoice     Generate HTML invoice/report
  python cli.py tui         Interactive terminal UI (htop-style)
  python cli.py breaker     Circuit breaker (breaker status|check|unblock)
  python cli.py redact      Export with redacted project names
  python cli.py search      Full-text search across sessions

Environment variables:
  CLAUDE_USAGE_DB              Path to SQLite database
  CLAUDE_USAGE_PORT            Dashboard port  (default 8080)
  CLAUDE_USAGE_API_PORT        API server port (default 8081)
  CLAUDE_USAGE_SCAN_INTERVAL   Background scan interval in seconds (default 30)
  CLAUDE_USAGE_DAILY_LIMIT_USD Daily spend cap for forecast/hooks  (default: unset)
  CLAUDE_USAGE_HOOKS           Path to hooks JSON config file
  CLAUDE_USAGE_USER            Active user ID (default: default)
  CLAUDE_USAGE_PLUGINS         Path to plugins directory
  CLAUDE_USAGE_ARCHIVE         Path to archive directory
  CLAUDE_USAGE_ANOMALY_FACTOR  Spike detection sensitivity (default: 3.0)
  CLAUDE_USAGE_RBAC            Enable role-based access control (0|1)
"""

COMMANDS = {
    "scan":       cmd_scan,
    "today":      cmd_today,
    "stats":      cmd_stats,
    "live":       cmd_live,
    "watch":      cmd_live,          # alias
    "forecast":   cmd_forecast,
    "export":     cmd_export,
    "dashboard":  cmd_dashboard,
    "query":      cmd_query,
    "replay":     cmd_replay,
    "branches":   cmd_branches,
    "optimize":   cmd_optimize,
    "anomalies":  cmd_anomalies,
    "archive":    cmd_archive,
    "timetravel": cmd_timetravel,
    "api":        cmd_api,
    "daemon":     cmd_daemon,
    "users":      cmd_users,
    "plugins":    cmd_plugins,
    "graph":      cmd_graph,
    "sync":       cmd_sync,
    "tag":        cmd_tag,
    "simulate":   cmd_simulate,
    "rollup":     cmd_rollup,
    "invoice":    cmd_invoice,
    "tui":        cmd_tui,
    "htop":       cmd_tui,          # alias
    "breaker":    cmd_circuit_breaker,
    "redact":     cmd_redact_export,
    "search":     cmd_search,
}

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(USAGE)
        sys.exit(0)
    COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
