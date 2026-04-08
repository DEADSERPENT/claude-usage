"""
cli.py - Command-line interface for the Claude Code usage dashboard.

Commands:
  scan      - Scan JSONL files and update the database
  today     - Print today's usage summary
  stats     - Print all-time usage statistics
  dashboard - Scan + open browser + start dashboard server
"""

import sys
import sqlite3
from pathlib import Path
from datetime import datetime, date, timedelta

from config import DB_PATH, PRICING, DAILY_LIMIT_USD


def get_pricing(model: str) -> dict:
    if not model:
        return PRICING["default"]
    if model in PRICING:
        return PRICING[model]
    for key in PRICING:
        if key != "default" and model.startswith(key):
            return PRICING[key]
    m = model.lower()
    if "opus"   in m: return PRICING.get("claude-opus-4-6",   PRICING["default"])
    if "sonnet" in m: return PRICING.get("claude-sonnet-4-6", PRICING["default"])
    if "haiku"  in m: return PRICING.get("claude-haiku-4-5",  PRICING["default"])
    return PRICING["default"]


def calc_cost(model, inp, out, cache_read, cache_creation):
    p = get_pricing(model)
    return (
        inp            * p["input"]       / 1_000_000 +
        out            * p["output"]      / 1_000_000 +
        cache_read     * p["cache_read"]  / 1_000_000 +
        cache_creation * p["cache_write"] / 1_000_000
    )

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

def require_db():
    if not DB_PATH.exists():
        print("Database not found. Run: python cli.py scan")
        sys.exit(1)
    return sqlite3.connect(DB_PATH)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_scan():
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
    print()
    conn.close()


def cmd_stats():
    conn = require_db()
    conn.row_factory = sqlite3.Row

    # All-time totals
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

    # By model
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

    # Top 5 projects
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

    # Daily average (last 30 days)
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

    # Build total cost across all models
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

    CLEAR = '\033[2J\033[H'
    BOLD  = '\033[1m'
    DIM   = '\033[2m'
    RST   = '\033[0m'
    W     = 66

    def sep(title=''):
        if title:
            bar = f'── {title} ' + '─' * max(0, W - len(title) - 4)
            return f'  {bar}'
        return '  ' + '─' * W

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
            cpt = t_cost / total_tok           # cost per token
            cost_hr = burn15 * 60 * cpt        # est $/hour at current rate

            print(CLEAR, end='')
            print(f"  {BOLD}Claude Code Usage  —  Live{RST}  "
                  f"{DIM}{now.strftime('%Y-%m-%d  %H:%M:%S')}{RST}")
            print(sep())
            print()

            # Today table
            print(f"  Today ({today_str})")
            print(f"  {'MODEL':<32} {'INPUT':>9} {'OUTPUT':>9}  {'COST':>10}")
            print(f"  {'─'*32} {'─'*9} {'─'*9}  {'─'*10}")
            for r in today_rows:
                inp=r['inp'] or 0; out=r['out'] or 0
                cost=calc_cost(r['model'], inp, out, r['cr'] or 0, r['cc'] or 0)
                m = r['model'][:32] if len(r['model']) > 32 else r['model']
                print(f"  {m:<32} {fmt(inp):>9} {fmt(out):>9}  ${cost:>9.4f}")
            print(f"  {'─'*32} {'─'*9} {'─'*9}  {'─'*10}")
            print(f"  {'TOTAL':<32} {fmt(t_inp):>9} {fmt(t_out):>9}  ${t_cost:>9.4f}")
            print(f"  {DIM}{t_sess} sessions  ·  {t_turns} turns  ·  "
                  f"cache read {fmt(t_cr)}  cache write {fmt(t_cc)}{RST}")
            print()

            # Burn rate
            print(sep('Burn Rate'))
            print(f"  {'':32} {'15-min':>9} {'1-hour':>9}")
            print(f"  {'tokens/min':<32} {fmt(int(burn15)):>9} {fmt(int(burn1h)):>9}")
            if burn15 > 0:
                print(f"  {'est. cost/hour':<32} {'':>9} ${cost_hr:>8.4f}")
            print()

            # Last turn
            if last:
                ts    = last['timestamp'][:19].replace('T', ' ')
                model = (last['model'] or '')[:28]
                branch = f"  [{last['git_branch']}]" if last['git_branch'] else ''
                print(sep('Last Turn'))
                print(f"  {ts}   {model}{branch}")
                print()

            print(sep())
            print(f"  {DIM}Refreshing every 2s  ·  Ctrl+C to stop{RST}")

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

    # Hourly totals — last 24h
    hourly = conn.execute("""
        SELECT substr(timestamp,1,13) as hour,
               COALESCE(model,'unknown') as model,
               SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc
        FROM turns
        WHERE timestamp >= datetime('now','-24 hours')
        GROUP BY hour, model ORDER BY hour
    """).fetchall()

    # Today totals by model
    today_rows = conn.execute("""
        SELECT COALESCE(model,'unknown') as model,
               SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc
        FROM turns WHERE substr(timestamp,1,10)=? GROUP BY model
    """, (today_str,)).fetchall()

    # Day-of-week averages (last 4 weeks)
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

    # All-time peak day
    peak = conn.execute("""
        SELECT substr(timestamp,1,10) as day,
               SUM(input_tokens+output_tokens) as total
        FROM turns GROUP BY day ORDER BY total DESC LIMIT 1
    """).fetchone()

    conn.close()

    # Aggregate hourly map
    from collections import defaultdict
    hour_map: dict = defaultdict(lambda: {"inp":0,"out":0,"cr":0,"cc":0,"model":"unknown"})
    for r in hourly:
        h = hour_map[r["hour"]]
        h["inp"] += r["inp"] or 0; h["out"] += r["out"] or 0
        h["cr"]  += r["cr"]  or 0; h["cc"]  += r["cc"]  or 0
        h["model"] = r["model"]  # last model seen per hour — good enough for cost hint

    hours_sorted = sorted(hour_map.items())

    # Burn rate from last 2h of hourly data
    recent = hours_sorted[-2:] if len(hours_sorted) >= 2 else hours_sorted
    if recent:
        burn_tokens = sum(v["inp"]+v["out"] for _, v in recent)
        burn_per_min = burn_tokens / (len(recent) * 60)
    else:
        burn_per_min = 0.0

    # 6h and 24h averages
    def _avg_per_min(n_hours):
        window = hours_sorted[-n_hours:] if len(hours_sorted) >= n_hours else hours_sorted
        if not window:
            return 0.0
        return sum(v["inp"]+v["out"] for _, v in window) / (len(window) * 60)

    burn6h  = _avg_per_min(6)
    burn24h = _avg_per_min(24)

    # Today totals
    t_inp=t_out=t_cr=t_cc = 0; t_cost = 0.0
    for r in today_rows:
        inp=r["inp"] or 0; out=r["out"] or 0
        cr=r["cr"] or 0;   cc=r["cc"] or 0
        t_inp+=inp; t_out+=out; t_cr+=cr; t_cc+=cc
        t_cost+=calc_cost(r["model"], inp, out, cr, cc)

    t_tokens = t_inp + t_out
    cpt      = t_cost / max(t_tokens, 1)   # cost per token (today's mix)

    # Project rest of day at current 2h burn rate
    hours_left     = max(0, 24 - now.hour - now.minute/60)
    proj_add_tok   = burn_per_min * 60 * hours_left
    proj_tok_total = t_tokens + proj_add_tok
    proj_cost      = t_cost   + proj_add_tok * cpt

    print()
    hr('=')
    print(f"  Claude Code Usage — Forecast           [{now.strftime('%Y-%m-%d  %H:%M')}]")
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
                eta = "⚠ limit already exceeded"
        else:
            eta = "N/A (no recent activity)"
        print(f"  {'Daily limit:':<22} ${DAILY_LIMIT_USD:.2f}  (CLAUDE_USAGE_DAILY_LIMIT_USD)")
        print(f"  {'Remaining:':<22} ${max(remaining, 0):.4f}")
        print(f"  {'ETA to limit:':<22} {eta}")
    else:
        print(f"  Daily limit:           (not set — export CLAUDE_USAGE_DAILY_LIMIT_USD=10.00)")

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
        print(f"  All-time Peak Day:  {peak['day']}  —  {fmt(peak['total'])} tokens")

    print()
    hr('=')
    print()


def cmd_export():
    import csv
    conn = require_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp,
            git_branch, model, turn_count,
            total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation
        FROM sessions
        ORDER BY last_timestamp DESC
    """).fetchall()
    conn.close()

    if not rows:
        print("No data to export.")
        return

    output_path = Path("claude_usage_export.csv")
    fieldnames = [
        "session_id", "project_name", "first_timestamp", "last_timestamp",
        "git_branch", "model", "turn_count",
        "total_input_tokens", "total_output_tokens",
        "total_cache_read", "total_cache_creation", "est_cost_usd",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            row = dict(r)
            row["est_cost_usd"] = round(calc_cost(
                row["model"],
                row["total_input_tokens"]   or 0,
                row["total_output_tokens"]  or 0,
                row["total_cache_read"]     or 0,
                row["total_cache_creation"] or 0,
            ), 6)
            writer.writerow(row)

    print(f"Exported {len(rows)} sessions to {output_path}")


def cmd_dashboard():
    import webbrowser
    import threading
    import time
    from config import DASHBOARD_PORT, SCAN_INTERVAL_SECS

    print("Running initial scan...")
    cmd_scan()

    # Background thread: keeps re-scanning JSONL files while the server runs
    # so the dashboard always reflects the latest Claude Code activity.
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


# ── Entry point ───────────────────────────────────────────────────────────────

USAGE = """
Claude Code Usage

Usage:
  python cli.py scan       Scan JSONL files and update database
  python cli.py today      Show today's usage summary
  python cli.py stats      Show all-time statistics
  python cli.py live       Real-time terminal monitor (Ctrl+C to stop)
  python cli.py watch      Alias for live
  python cli.py forecast   Burn-rate analysis and end-of-day projection
  python cli.py export     Export sessions to claude_usage_export.csv
  python cli.py dashboard  Scan + start dashboard (port: CLAUDE_USAGE_PORT, default 8080)

Environment variables:
  CLAUDE_USAGE_DB              Path to SQLite database
  CLAUDE_USAGE_PORT            Dashboard port  (default 8080)
  CLAUDE_USAGE_SCAN_INTERVAL   Background scan interval in seconds (default 30)
  CLAUDE_USAGE_DAILY_LIMIT_USD Daily spend cap for forecast/hooks  (default: unset)
  CLAUDE_USAGE_HOOKS           Path to hooks JSON config file
"""

COMMANDS = {
    "scan":      cmd_scan,
    "today":     cmd_today,
    "stats":     cmd_stats,
    "live":      cmd_live,
    "watch":     cmd_live,       # alias
    "forecast":  cmd_forecast,
    "export":    cmd_export,
    "dashboard": cmd_dashboard,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(USAGE)
        sys.exit(0)
    COMMANDS[sys.argv[1]]()
