"""
circuit_breaker.py - Automatic local quota enforcement for claude-usage.

When enabled (CLAUDE_USAGE_CIRCUIT_BREAKER=1), runs automatically after every
scan to check daily spending against CLAUDE_USAGE_DAILY_LIMIT_USD.

Budget warning thresholds fire notifications at 50%, 80%, and 100% of the
daily limit. When the limit is exceeded, the configured action is taken:
- warn: Print a warning (default)
- kill: Kill Claude Code processes to stop spending
- block: Rename Claude binary to prevent launching

Environment variables:
  CLAUDE_USAGE_CIRCUIT_BREAKER=1          Enable automatic checking
  CLAUDE_USAGE_DAILY_LIMIT_USD=10.00      Daily spending cap in USD
  CLAUDE_USAGE_CIRCUIT_BREAKER_ACTION=warn Action when tripped (warn/kill/block)
"""

import json
import os
import sys
import signal
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from config import (
    DB_PATH, DAILY_LIMIT_USD, CIRCUIT_BREAKER_ENABLED,
    CIRCUIT_BREAKER_ACTION, CLAUDE_DIR, calc_cost,
)

BUDGET_THRESHOLDS = [
    (0.50, "info",     "50% of daily budget used"),
    (0.80, "warning",  "80% of daily budget used"),
    (1.00, "critical", "Daily budget limit reached"),
]

_STATE_FILE = CLAUDE_DIR / "circuit_breaker_state.json"


def _load_state() -> dict:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def _get_today_cost(db_path: Path = DB_PATH) -> float:
    if not db_path.exists():
        return 0.0
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    today_str = date.today().isoformat()
    rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') as model,
               SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
        GROUP BY model
    """, (today_str,)).fetchall()
    conn.close()
    total = 0.0
    for r in rows:
        total += calc_cost(r["model"], r["inp"] or 0, r["out"] or 0,
                          r["cr"] or 0, r["cc"] or 0)
    return total


def _find_claude_processes() -> list[int]:
    """Find running Claude Code process PIDs."""
    pids = []
    if sys.platform == "win32":
        try:
            import subprocess
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq claude.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True
            )
            for line in result.stdout.strip().split("\n"):
                if line.strip() and "claude" in line.lower():
                    parts = line.strip('"').split('","')
                    if len(parts) >= 2:
                        try:
                            pids.append(int(parts[1].strip('"')))
                        except ValueError:
                            pass
        except Exception:
            pass
    else:
        try:
            import subprocess
            result = subprocess.run(["pgrep", "-f", "claude"], capture_output=True, text=True)
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    try:
                        pids.append(int(line.strip()))
                    except ValueError:
                        pass
        except Exception:
            pass
    return pids


def _kill_processes(pids: list[int]) -> int:
    """Kill a list of processes. Returns number killed."""
    killed = 0
    for pid in pids:
        try:
            if sys.platform == "win32":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(0x0001, False, pid)
                if handle:
                    kernel32.TerminateProcess(handle, 1)
                    kernel32.CloseHandle(handle)
                    killed += 1
            else:
                os.kill(pid, signal.SIGTERM)
                killed += 1
        except (OSError, PermissionError):
            pass
    return killed


def _fire_budget_notification(pct: float, severity: str, message: str,
                              today_cost: float, limit: float) -> None:
    """Send budget threshold notifications via hooks and plugins."""
    alert = {
        "type": "budget_warning",
        "metric": "daily_cost_pct",
        "severity": severity,
        "pct_used": round(pct * 100, 1),
        "today_cost": round(today_cost, 4),
        "limit": limit,
        "message": f"{message}: ${today_cost:.4f} / ${limit:.2f} ({pct*100:.0f}%)",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        from hooks import _fire, _fire_webhook
        from config import HOOKS_PATH
        if HOOKS_PATH.exists():
            hooks_cfg = json.loads(HOOKS_PATH.read_text(encoding="utf-8"))
            budget_cfg = hooks_cfg.get("daily_cost_usd", {})
            cmd = budget_cfg.get(f"on_{severity}") or budget_cfg.get("on_warn")
            webhook = (budget_cfg.get(f"on_{severity}_webhook")
                       or budget_cfg.get("webhook_url"))
            if cmd:
                _fire(cmd)
            if webhook:
                _fire_webhook(webhook, alert)
    except Exception:
        pass

    try:
        from plugins import run_hook
        run_hook("on_alert", alert)
    except Exception:
        pass


def check_budget_thresholds(db_path: Path = DB_PATH) -> list[dict]:
    """
    Check spending against budget thresholds and fire notifications
    for newly crossed thresholds. Returns list of alerts fired.
    """
    if DAILY_LIMIT_USD <= 0:
        return []

    today_cost = _get_today_cost(db_path)
    pct_used = today_cost / DAILY_LIMIT_USD

    state = _load_state()
    today_str = date.today().isoformat()
    today_state = state.get(today_str, {})

    alerts_fired = []

    for threshold_pct, severity, message in BUDGET_THRESHOLDS:
        key = f"threshold_{int(threshold_pct * 100)}"
        already_fired = today_state.get(key, False)

        if pct_used >= threshold_pct and not already_fired:
            _fire_budget_notification(pct_used, severity, message,
                                      today_cost, DAILY_LIMIT_USD)
            today_state[key] = True
            alerts_fired.append({
                "threshold": threshold_pct,
                "severity": severity,
                "message": message,
                "pct_used": round(pct_used * 100, 1),
                "today_cost": round(today_cost, 4),
            })

    if alerts_fired:
        state[today_str] = today_state
        cutoff = date.today().isoformat()[:8]  # keep recent days only
        state = {k: v for k, v in state.items()
                 if k >= (date.today().replace(day=1)).isoformat()}
        _save_state(state)

    return alerts_fired


def check_circuit_breaker(db_path: Path = DB_PATH, action: str = None) -> dict:
    """
    Check if daily limit is exceeded and take action.

    When called without an explicit action, uses the CIRCUIT_BREAKER_ACTION
    config (defaults to "warn").
    """
    if action is None:
        action = CIRCUIT_BREAKER_ACTION

    if DAILY_LIMIT_USD <= 0:
        return {"tripped": False, "reason": "no daily limit configured"}

    today_cost = _get_today_cost(db_path)

    if today_cost < DAILY_LIMIT_USD:
        pct = (today_cost / DAILY_LIMIT_USD) * 100
        return {
            "tripped": False,
            "today_cost": round(today_cost, 4),
            "limit": DAILY_LIMIT_USD,
            "pct_used": round(pct, 1),
        }

    result = {
        "tripped": True,
        "today_cost": round(today_cost, 4),
        "limit": DAILY_LIMIT_USD,
        "overage": round(today_cost - DAILY_LIMIT_USD, 4),
        "action_taken": action,
    }

    if action == "warn":
        result["message"] = (
            f"CIRCUIT BREAKER: Daily limit ${DAILY_LIMIT_USD:.2f} exceeded! "
            f"Current cost: ${today_cost:.4f}"
        )

    elif action == "kill":
        pids = _find_claude_processes()
        if pids:
            killed = _kill_processes(pids)
            result["message"] = f"Killed {killed} Claude process(es) to stop spending"
            result["killed_pids"] = pids[:killed]
        else:
            result["message"] = "Limit exceeded but no Claude processes found to kill"

    elif action == "block":
        blocked = _block_claude_binary()
        result["message"] = (
            "Claude binary blocked to prevent further usage"
            if blocked else "Could not block Claude binary"
        )
        result["blocked"] = blocked

    return result


def auto_check(db_path: Path = DB_PATH) -> dict | None:
    """
    Automatic post-scan check. Only runs when CIRCUIT_BREAKER_ENABLED is True.
    Returns the breaker result if it ran, None if disabled.
    """
    if not CIRCUIT_BREAKER_ENABLED:
        return None

    budget_alerts = check_budget_thresholds(db_path)
    breaker_result = check_circuit_breaker(db_path)

    if breaker_result.get("tripped"):
        try:
            from plugins import run_hook
            run_hook("on_alert", {
                "type": "circuit_breaker_tripped",
                "severity": "critical",
                **breaker_result,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass

    return {
        "budget_alerts": budget_alerts,
        "breaker": breaker_result,
    }


def _block_claude_binary() -> bool:
    """Attempt to rename Claude binary to prevent execution."""
    possible_paths = []
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            possible_paths.append(Path(local_app) / "Programs" / "claude" / "claude.exe")
        possible_paths.append(Path.home() / ".claude" / "local" / "claude.exe")
    else:
        possible_paths.extend([
            Path.home() / ".local" / "bin" / "claude",
            Path("/usr/local/bin/claude"),
        ])

    for path in possible_paths:
        if path.exists():
            blocked_path = path.with_suffix(path.suffix + ".blocked")
            try:
                path.rename(blocked_path)
                return True
            except (OSError, PermissionError):
                pass
    return False


def unblock_claude_binary() -> bool:
    """Restore a previously blocked Claude binary."""
    possible_paths = []
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            possible_paths.append(Path(local_app) / "Programs" / "claude" / "claude.exe.blocked")
        possible_paths.append(Path.home() / ".claude" / "local" / "claude.exe.blocked")
    else:
        possible_paths.extend([
            Path.home() / ".local" / "bin" / "claude.blocked",
            Path("/usr/local/bin/claude.blocked"),
        ])

    for path in possible_paths:
        if path.exists():
            orig_path = path.with_name(path.name.replace(".blocked", ""))
            try:
                path.rename(orig_path)
                return True
            except (OSError, PermissionError):
                pass
    return False


def get_status(db_path: Path = DB_PATH) -> dict:
    """Get current circuit breaker status without taking action."""
    today_cost = _get_today_cost(db_path)
    return {
        "enabled": DAILY_LIMIT_USD > 0,
        "today_cost": round(today_cost, 4),
        "limit": DAILY_LIMIT_USD,
        "pct_used": round((today_cost / max(DAILY_LIMIT_USD, 0.01)) * 100, 1),
        "tripped": today_cost >= DAILY_LIMIT_USD if DAILY_LIMIT_USD > 0 else False,
    }
