"""
hooks.py - Threshold-based shell notification hooks for claude-usage.

Create ~/.claude/usage_hooks.json to configure.  Example:

    {
      "daily_cost_usd": {
        "warn":        1.00,
        "critical":    5.00,
        "on_warn":     "notify-send 'Claude Usage' 'Daily cost passed $1'",
        "on_critical": "notify-send 'Claude Usage' 'Daily cost passed $5'",
        "webhook_url": "http://localhost:5000/hook"
      },
      "daily_tokens": {
        "warn":    500000,
        "on_warn": "echo 'Claude: 500K tokens used today'",
        "on_warn_webhook": "http://localhost:5000/tokens"
      }
    }

Supported metrics (evaluated once per scanner run):
  daily_cost_usd   Estimated API cost today (USD)
  daily_tokens     Total input + output tokens today
  daily_turns      Number of assistant turns today

Each metric block may define:
  warn / critical      Numeric threshold value
  on_warn / on_critical  Shell command executed when threshold is crossed
  webhook_url          Optional URL: POST JSON for any level without on_*_webhook
  on_warn_webhook / on_critical_webhook  URL for HTTP POST (JSON) when that level fires

Shell commands and webhooks fire once per threshold crossing per day.  A threshold resets
(allowing them to fire again) once usage drops back below 90 % of it.
"""

import json
import shlex
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from config import calc_cost


def _today_stats(db_path: Path) -> dict:
    """Return {daily_cost_usd, daily_tokens, daily_turns} for today."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    today = date.today().isoformat()
    rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') as model,
               SUM(input_tokens)          as inp,
               SUM(output_tokens)         as out,
               SUM(cache_read_tokens)     as cr,
               SUM(cache_creation_tokens) as cc,
               COUNT(*)                   as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
        GROUP BY model
    """, (today,)).fetchall()
    conn.close()

    total_tokens = total_turns = 0
    total_cost = 0.0
    for r in rows:
        inp = r["inp"] or 0;  out = r["out"] or 0
        cr  = r["cr"]  or 0;  cc  = r["cc"]  or 0
        total_tokens += inp + out
        total_turns  += r["turns"]
        total_cost   += calc_cost(r["model"], inp, out, cr, cc)

    return {
        "daily_cost_usd": round(total_cost, 6),
        "daily_tokens":   total_tokens,
        "daily_turns":    total_turns,
    }


def _fire(command: str) -> None:
    """Execute a notification command silently in the background.

    On Windows, uses cmd /c since shlex.split follows POSIX rules.
    On Unix, splits the command into an argv list to avoid shell=True.
    """
    try:
        if sys.platform == "win32":
            args = ["cmd", "/c", command]
        else:
            args = shlex.split(command)
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _fire_webhook(url: str, payload: dict) -> None:
    """Send an HTTP POST webhook with JSON payload."""
    import urllib.request

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json", "User-Agent": "claude-usage-hooks/1.0"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def check_and_fire(db_path: Path, hooks_path: Path) -> None:
    """Load hooks config, evaluate every threshold, fire commands if crossed."""
    if not hooks_path.exists() or not db_path.exists():
        return

    try:
        hooks: dict = json.loads(hooks_path.read_text(encoding="utf-8"))
    except Exception:
        return  # malformed JSON — skip silently

    state_path = hooks_path.parent / "hook_state.json"
    try:
        state: dict = (
            json.loads(state_path.read_text(encoding="utf-8"))
            if state_path.exists() else {}
        )
    except Exception:
        state = {}

    today = date.today().isoformat()
    today_state: dict = state.get(today, {})

    try:
        stats = _today_stats(db_path)
    except Exception:
        return

    changed = False
    for metric, cfg in hooks.items():
        if not isinstance(cfg, dict):
            continue
        value = stats.get(metric)
        if value is None:
            continue

        for level in ("critical", "warn"):
            threshold = cfg.get(level)
            command   = cfg.get(f"on_{level}")
            webhook   = (
                cfg.get(f"on_{level}_webhook")
                or cfg.get(f"on_{level}_url")
                or cfg.get("webhook_url")
            )
            if threshold is None or (not command and not webhook):
                continue

            key           = f"{metric}:{level}"
            already_fired = today_state.get(key, False)

            if value >= threshold and not already_fired:
                if command:
                    _fire(command)
                if webhook:
                    ts = datetime.now(timezone.utc).isoformat()
                    _fire_webhook(
                        webhook,
                        {
                            "metric": metric,
                            "value": value,
                            "threshold": threshold,
                            "timestamp": ts,
                            "severity": level,
                        },
                    )
                today_state[key] = True
                changed = True
            elif value < threshold * 0.9 and already_fired:
                # Reset so it can fire again if usage climbs back up
                today_state[key] = False
                changed = True

    if changed or today_state:
        state[today] = today_state
        # Keep only last 7 days to prevent unbounded growth
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        state = {k: v for k, v in state.items() if k >= cutoff}
        try:
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception:
            pass
