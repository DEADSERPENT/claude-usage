"""
mcp_server.py - Model Context Protocol (MCP) server for claude-usage.

Exposes claude-usage data as MCP tools so Claude Code can query its own
usage, cost, and session data directly during a conversation.

Usage:
    python mcp_server.py

Register in Claude Code's config (~/.config/claude/claude_desktop_config.json):
    {
      "mcpServers": {
        "claude-usage": {
          "command": "python",
          "args": ["/path/to/claude-usage/mcp_server.py"]
        }
      }
    }

Or using the compiled binary:
    {
      "mcpServers": {
        "claude-usage": {
          "command": "/path/to/claude-usage-linux"
        }
      }
    }
    Then add  `mcp`  as a command alias in cli.py (already handled).

Tools exposed:
    get_usage_today       - Today's token and cost breakdown by model
    get_usage_stats       - All-time statistics
    get_cost_forecast     - Burn rate + projected end-of-day cost
    get_recent_sessions   - Last N sessions with cost
    get_top_projects      - Most expensive projects
    get_circuit_breaker   - Circuit breaker / budget alert status
    get_anomalies         - Recent anomaly detections
    run_scan              - Trigger a fresh scan of JSONL logs
"""

import sys
import json
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path

# ── Minimal bootstrap so this file is runnable standalone ────────────────────
try:
    from config import DB_PATH, calc_cost, DAILY_LIMIT_USD
except ImportError:
    _HERE = Path(__file__).parent
    sys.path.insert(0, str(_HERE))
    from config import DB_PATH, calc_cost, DAILY_LIMIT_USD


# ─────────────────────────────────────────────────────────────────────────────
#  JSON-RPC / MCP wire protocol helpers
# ─────────────────────────────────────────────────────────────────────────────

def _send(obj: dict):
    """Write a JSON-RPC message to stdout followed by newline."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _ok(req_id, result):
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _err(req_id, code: int, msg: str):
    _send({"jsonrpc": "2.0", "id": req_id,
           "error": {"code": code, "message": msg}})


def _tool_result(text: str) -> dict:
    """Wrap a plain-text answer in the MCP tool-result schema."""
    return {"content": [{"type": "text", "text": text}]}


# ─────────────────────────────────────────────────────────────────────────────
#  Database helpers
# ─────────────────────────────────────────────────────────────────────────────

def _conn():
    if not DB_PATH.exists():
        return None
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _fmt(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


# ─────────────────────────────────────────────────────────────────────────────
#  Tool implementations
# ─────────────────────────────────────────────────────────────────────────────

def tool_get_usage_today(_args: dict) -> str:
    conn = _conn()
    if not conn:
        return "No database found. Run: claude-usage scan"

    today = date.today().isoformat()
    rows = conn.execute("""
        SELECT COALESCE(model,'unknown') as model,
               SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
               COUNT(*) as turns, COUNT(DISTINCT session_id) as sessions
        FROM turns WHERE substr(timestamp,1,10)=?
        GROUP BY model ORDER BY inp+out DESC
    """, (today,)).fetchall()
    conn.close()

    if not rows:
        return f"No usage recorded today ({today})."

    lines = [f"Claude Usage — Today ({today})", ""]
    total_cost = 0.0
    total_inp = total_out = 0
    for r in rows:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0,
                         r["cr"] or 0, r["cc"] or 0)
        total_cost += cost
        total_inp += r["inp"] or 0
        total_out += r["out"] or 0
        lines.append(
            f"  {r['model']:<34} turns={r['turns']:<4}  "
            f"in={_fmt(r['inp'] or 0):<8}  out={_fmt(r['out'] or 0):<8}  "
            f"cost=${cost:.4f}"
        )
    lines += [
        "",
        f"  TOTAL  in={_fmt(total_inp)}  out={_fmt(total_out)}  cost=${total_cost:.4f}",
    ]
    if DAILY_LIMIT_USD > 0:
        pct = min(100, total_cost / DAILY_LIMIT_USD * 100)
        lines.append(f"  Daily limit: ${DAILY_LIMIT_USD:.2f} — used {pct:.1f}%")
    return "\n".join(lines)


def tool_get_usage_stats(_args: dict) -> str:
    conn = _conn()
    if not conn:
        return "No database found. Run: claude-usage scan"

    row = conn.execute("""
        SELECT SUM(total_input_tokens) as inp, SUM(total_output_tokens) as out,
               SUM(total_cache_read) as cr, SUM(total_cache_creation) as cc,
               SUM(turn_count) as turns, COUNT(*) as sessions,
               MIN(first_timestamp) as first, MAX(last_timestamp) as last
        FROM sessions
    """).fetchone()

    by_model = conn.execute("""
        SELECT COALESCE(model,'unknown') as model,
               SUM(total_input_tokens) as inp, SUM(total_output_tokens) as out,
               SUM(total_cache_read) as cr, SUM(total_cache_creation) as cc,
               COUNT(*) as sessions
        FROM sessions GROUP BY model ORDER BY inp+out DESC
    """).fetchall()
    conn.close()

    total_cost = sum(
        calc_cost(r["model"], r["inp"] or 0, r["out"] or 0,
                  r["cr"] or 0, r["cc"] or 0)
        for r in by_model
    )

    lines = [
        "Claude Usage — All-Time Statistics",
        f"  Period:         {(row['first'] or '')[:10]}  →  {(row['last'] or '')[:10]}",
        f"  Sessions:       {row['sessions'] or 0:,}",
        f"  Turns:          {_fmt(row['turns'] or 0)}",
        f"  Input tokens:   {_fmt(row['inp'] or 0)}",
        f"  Output tokens:  {_fmt(row['out'] or 0)}",
        f"  Cache read:     {_fmt(row['cr'] or 0)}",
        f"  Cache creation: {_fmt(row['cc'] or 0)}",
        f"  Est. total cost: ${total_cost:.4f}",
        "",
        "  By model:",
    ]
    for r in by_model:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0,
                         r["cr"] or 0, r["cc"] or 0)
        lines.append(f"    {r['model']:<34} sessions={r['sessions']:<4}  cost=${cost:.4f}")
    return "\n".join(lines)


def tool_get_cost_forecast(_args: dict) -> str:
    conn = _conn()
    if not conn:
        return "No database found. Run: claude-usage scan"

    today = date.today().isoformat()
    now = datetime.now()

    today_row = conn.execute("""
        SELECT SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc
        FROM turns WHERE substr(timestamp,1,10)=?
    """, (today,)).fetchone()

    cut2h = (datetime.utcnow() - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%S')
    burn_row = conn.execute(
        "SELECT SUM(input_tokens+output_tokens) as t FROM turns WHERE timestamp>=?",
        (cut2h,)
    ).fetchone()
    conn.close()

    inp = today_row["inp"] or 0
    out = today_row["out"] or 0
    today_cost = calc_cost("default", inp, out,
                           today_row["cr"] or 0, today_row["cc"] or 0)
    today_tokens = inp + out
    burn_pm = (burn_row["t"] or 0) / 120
    cpt = today_cost / max(today_tokens, 1)
    hours_left = max(0, 24 - now.hour - now.minute / 60)
    proj_cost = today_cost + burn_pm * 60 * hours_left * cpt
    proj_tokens = today_tokens + burn_pm * 60 * hours_left

    lines = [
        f"Claude Usage — Cost Forecast  [{now.strftime('%H:%M')}]",
        f"  Today's cost:       ${today_cost:.4f}",
        f"  Today's tokens:     {_fmt(today_tokens)}",
        f"  Burn rate (2h avg): {_fmt(int(burn_pm))}/min",
        f"  Hours remaining:    {hours_left:.1f}h",
        f"  Projected EOD cost: ${proj_cost:.4f}",
        f"  Projected EOD tok:  {_fmt(int(proj_tokens))}",
    ]
    if DAILY_LIMIT_USD > 0:
        remaining = max(0, DAILY_LIMIT_USD - today_cost)
        pct = min(100, today_cost / DAILY_LIMIT_USD * 100)
        lines += [
            f"  Daily limit:        ${DAILY_LIMIT_USD:.2f}",
            f"  Remaining:          ${remaining:.4f}  ({pct:.1f}% used)",
        ]
    return "\n".join(lines)


def tool_get_recent_sessions(args: dict) -> str:
    n = min(int(args.get("limit", 10)), 50)
    conn = _conn()
    if not conn:
        return "No database found. Run: claude-usage scan"

    rows = conn.execute("""
        SELECT session_id, project_name, last_timestamp, model,
               total_input_tokens, total_output_tokens,
               total_cache_read, total_cache_creation, turn_count
        FROM sessions ORDER BY last_timestamp DESC LIMIT ?
    """, (n,)).fetchall()
    conn.close()

    lines = [f"Claude Usage — Last {n} Sessions", ""]
    for r in rows:
        cost = calc_cost(r["model"] or "default",
                         r["total_input_tokens"] or 0,
                         r["total_output_tokens"] or 0,
                         r["total_cache_read"] or 0,
                         r["total_cache_creation"] or 0)
        ts = (r["last_timestamp"] or "")[:16].replace("T", " ")
        lines.append(
            f"  {r['session_id'][:8]}  {(r['project_name'] or 'unknown'):<30}  "
            f"{ts}  turns={r['turn_count']:<4}  cost=${cost:.4f}"
        )
    return "\n".join(lines)


def tool_get_top_projects(args: dict) -> str:
    n = min(int(args.get("limit", 10)), 30)
    conn = _conn()
    if not conn:
        return "No database found. Run: claude-usage scan"

    rows = conn.execute("""
        SELECT project_name,
               SUM(total_input_tokens) as inp, SUM(total_output_tokens) as out,
               SUM(total_cache_read) as cr, SUM(total_cache_creation) as cc,
               COUNT(*) as sessions, SUM(turn_count) as turns
        FROM sessions GROUP BY project_name
        ORDER BY inp+out DESC LIMIT ?
    """, (n,)).fetchall()
    conn.close()

    lines = [f"Claude Usage — Top {n} Projects by Tokens", ""]
    for i, r in enumerate(rows, 1):
        cost = calc_cost("default", r["inp"] or 0, r["out"] or 0,
                         r["cr"] or 0, r["cc"] or 0)
        lines.append(
            f"  {i:>2}. {(r['project_name'] or 'unknown'):<40}  "
            f"sessions={r['sessions']:<4}  turns={_fmt(r['turns'] or 0):<8}  "
            f"tokens={_fmt((r['inp'] or 0)+(r['out'] or 0))}  cost=${cost:.4f}"
        )
    return "\n".join(lines)


def tool_get_circuit_breaker(_args: dict) -> str:
    try:
        from circuit_breaker import get_status
        s = get_status(DB_PATH)
        lines = ["Claude Usage — Circuit Breaker Status", ""]
        cb = s.get("breaker", {})
        lines.append(f"  Tripped:   {'YES ⚠️' if cb.get('tripped') else 'No'}")
        lines.append(f"  Message:   {cb.get('message', 'OK')}")
        alerts = s.get("budget_alerts", [])
        if alerts:
            lines.append("")
            lines.append("  Budget Alerts:")
            for a in alerts:
                lines.append(f"    [{a.get('severity','?').upper()}] {a.get('message','')}")
        else:
            lines.append("  Budget Alerts: none")
        return "\n".join(lines)
    except Exception as e:
        return f"Could not read circuit breaker status: {e}"


def tool_get_anomalies(args: dict) -> str:
    days = int(args.get("days", 7))
    try:
        from anomaly import get_recent_anomalies
        items = get_recent_anomalies(DB_PATH, days=days)
        if not items:
            return f"No anomalies detected in the last {days} days."
        lines = [f"Claude Usage — Anomalies (last {days} days)", ""]
        for a in items[:20]:
            ack = " [ack]" if a.get("acknowledged") else ""
            lines.append(
                f"  [{a.get('severity','?').upper()}]{ack}  "
                f"{(a.get('detected_at') or '')[:16]}  "
                f"{a.get('message','')}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Could not read anomalies: {e}"


def tool_run_scan(_args: dict) -> str:
    try:
        from scanner import scan
        result = scan(verbose=False)
        return (
            f"Scan complete — "
            f"{result.get('new', 0)} new files, "
            f"{result.get('updated', 0)} updated, "
            f"{result.get('turns', 0)} turns added, "
            f"{result.get('sessions', 0)} sessions seen."
        )
    except Exception as e:
        return f"Scan failed: {e}"


# ─────────────────────────────────────────────────────────────────────────────
#  Tool registry
# ─────────────────────────────────────────────────────────────────────────────

TOOLS = {
    "get_usage_today": {
        "fn": tool_get_usage_today,
        "description": "Return today's Claude Code token usage and cost breakdown by model.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    "get_usage_stats": {
        "fn": tool_get_usage_stats,
        "description": "Return all-time Claude Code usage statistics (tokens, cost, sessions).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    "get_cost_forecast": {
        "fn": tool_get_cost_forecast,
        "description": "Return today's spend, current burn rate, and projected end-of-day cost.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    "get_recent_sessions": {
        "fn": tool_get_recent_sessions,
        "description": "List recent Claude Code sessions with their cost. Optional limit (default 10, max 50).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of sessions to return (default 10)"}
            },
            "required": [],
        },
    },
    "get_top_projects": {
        "fn": tool_get_top_projects,
        "description": "List top projects by token usage and cost.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Number of projects to return (default 10)"}
            },
            "required": [],
        },
    },
    "get_circuit_breaker": {
        "fn": tool_get_circuit_breaker,
        "description": "Check the circuit breaker and budget alert status.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    "get_anomalies": {
        "fn": tool_get_anomalies,
        "description": "List recently detected usage anomalies (spikes, unusual patterns).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Look-back window in days (default 7)"}
            },
            "required": [],
        },
    },
    "run_scan": {
        "fn": tool_run_scan,
        "description": "Trigger a fresh scan of Claude Code JSONL log files to update the database.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
}


# ─────────────────────────────────────────────────────────────────────────────
#  MCP request dispatcher
# ─────────────────────────────────────────────────────────────────────────────

def _handle(req: dict):
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {})

    # ── MCP lifecycle ──────────────────────────────────────────────────────────
    if method == "initialize":
        _ok(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "claude-usage", "version": "1.0.1"},
        })

    elif method == "notifications/initialized":
        pass  # No response needed for notifications

    elif method == "tools/list":
        tool_list = [
            {
                "name": name,
                "description": meta["description"],
                "inputSchema": meta["inputSchema"],
            }
            for name, meta in TOOLS.items()
        ]
        _ok(req_id, {"tools": tool_list})

    elif method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {})
        tool = TOOLS.get(name)
        if not tool:
            _err(req_id, -32601, f"Unknown tool: {name}")
            return
        try:
            text = tool["fn"](args)
            _ok(req_id, _tool_result(text))
        except Exception as e:
            _err(req_id, -32000, str(e))

    elif method == "ping":
        _ok(req_id, {})

    else:
        if req_id is not None:
            _err(req_id, -32601, f"Method not found: {method}")


# ─────────────────────────────────────────────────────────────────────────────
#  Main loop — reads newline-delimited JSON from stdin
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # Redirect stderr to /dev/null so Python tracebacks don't pollute the MCP
    # stdio channel (Claude Code reads stdout only).
    import os
    try:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            _handle(req)
        except Exception:
            pass  # Never crash — just drop the request


if __name__ == "__main__":
    main()
