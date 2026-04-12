"""
config.py - Centralized configuration for claude-usage.

Override defaults with environment variables:
  CLAUDE_DIR           - base directory (default: ~/.claude)
  CLAUDE_USAGE_DB      - path to SQLite database
  CLAUDE_PROJECTS_DIR  - path to Claude Code projects directory
"""

import os
from pathlib import Path

_default_claude_dir = Path.home() / ".claude"

CLAUDE_DIR   = Path(os.environ.get("CLAUDE_DIR",          str(_default_claude_dir)))
DB_PATH      = Path(os.environ.get("CLAUDE_USAGE_DB",     str(CLAUDE_DIR / "usage.db")))
PROJECTS_DIR = Path(os.environ.get("CLAUDE_PROJECTS_DIR", str(CLAUDE_DIR / "projects")))

# How often (seconds) the background scanner re-reads JSONL files while the
# dashboard is running.  Also drives the JS auto-refresh interval.
SCAN_INTERVAL_SECS = int(os.environ.get("CLAUDE_USAGE_SCAN_INTERVAL", "30"))

# Port the dashboard HTTP server listens on.
DASHBOARD_PORT = int(os.environ.get("CLAUDE_USAGE_PORT", "8080"))

# Path to the threshold-hooks JSON config (create to enable shell notifications).
HOOKS_PATH = Path(os.environ.get("CLAUDE_USAGE_HOOKS", str(CLAUDE_DIR / "usage_hooks.json")))

# Optional daily spending cap in USD used by `cu forecast` and the dashboard.
# 0 = disabled.  Override: export CLAUDE_USAGE_DAILY_LIMIT_USD=10.00
DAILY_LIMIT_USD = float(os.environ.get("CLAUDE_USAGE_DAILY_LIMIT_USD", "0"))

# ── Multi-User ────────────────────────────────────────────────────────────────
ACTIVE_USER = os.environ.get("CLAUDE_USAGE_USER", "default")
USERS_FILE  = Path(os.environ.get("CLAUDE_USAGE_USERS", str(CLAUDE_DIR / "usage_users.json")))

# ── Plugin System ─────────────────────────────────────────────────────────────
PLUGINS_DIR = Path(os.environ.get("CLAUDE_USAGE_PLUGINS", str(CLAUDE_DIR / "usage_plugins")))

# ── Archival ──────────────────────────────────────────────────────────────────
ARCHIVE_DIR = Path(os.environ.get("CLAUDE_USAGE_ARCHIVE", str(CLAUDE_DIR / "usage_archives")))

# ── Daemon ────────────────────────────────────────────────────────────────────
DAEMON_PID_FILE = Path(os.environ.get("CLAUDE_USAGE_PID", str(CLAUDE_DIR / "usage_daemon.pid")))
DAEMON_LOG_FILE = Path(os.environ.get("CLAUDE_USAGE_DAEMON_LOG", str(CLAUDE_DIR / "usage_daemon.log")))

# ── API Server ────────────────────────────────────────────────────────────────
API_PORT = int(os.environ.get("CLAUDE_USAGE_API_PORT", "8081"))

# ── Anomaly Detection ─────────────────────────────────────────────────────────
ANOMALY_WINDOW_DAYS  = int(os.environ.get("CLAUDE_USAGE_ANOMALY_WINDOW", "7"))
ANOMALY_SPIKE_FACTOR = float(os.environ.get("CLAUDE_USAGE_ANOMALY_FACTOR", "3.0"))

# ── RBAC ──────────────────────────────────────────────────────────────────────
RBAC_ENABLED = os.environ.get("CLAUDE_USAGE_RBAC", "0") == "1"

# Pricing per million tokens — single source of truth for both CLI and dashboard.
# Edit these values when Anthropic updates rates; no other file needs changing.
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-6":   {"input": 6.15,  "output": 30.75, "cache_write": 7.69, "cache_read": 0.61},
    "claude-opus-4-5":   {"input": 6.15,  "output": 30.75, "cache_write": 7.69, "cache_read": 0.61},
    "claude-sonnet-4-6": {"input": 3.69,  "output": 18.45, "cache_write": 4.61, "cache_read": 0.37},
    "claude-sonnet-4-5": {"input": 3.69,  "output": 18.45, "cache_write": 4.61, "cache_read": 0.37},
    "claude-haiku-4-5":  {"input": 1.23,  "output":  6.15, "cache_write": 1.54, "cache_read": 0.12},
    "claude-haiku-4-6":  {"input": 1.23,  "output":  6.15, "cache_write": 1.54, "cache_read": 0.12},
    "default":           {"input": 3.69,  "output": 18.45, "cache_write": 4.61, "cache_read": 0.37},
}


def get_pricing_for_model(model: str | None) -> dict[str, float]:
    """Return the best pricing row for a model name."""
    model = model or ""
    p = PRICING.get(model)
    if p is None:
        for key in PRICING:
            if key != "default" and model.startswith(key):
                p = PRICING[key]
                break
    if p is None:
        ml = model.lower()
        if "opus" in ml:
            p = PRICING.get("claude-opus-4-6")
        elif "sonnet" in ml:
            p = PRICING.get("claude-sonnet-4-6")
        elif "haiku" in ml:
            p = PRICING.get("claude-haiku-4-5")
    if p is None:
        p = PRICING.get("default", {})
    return p


def calc_cost(model: str | None, inp: int, out: int, cache_read: int, cache_creation: int) -> float:
    """Estimate USD cost from token counters using configured pricing."""
    p = get_pricing_for_model(model)
    return (
        inp * p.get("input", 0) / 1_000_000 +
        out * p.get("output", 0) / 1_000_000 +
        cache_read * p.get("cache_read", 0) / 1_000_000 +
        cache_creation * p.get("cache_write", 0) / 1_000_000
    )


# ── Migrations / sync / layout / plugins state ────────────────────────────────
MIGRATIONS_DIR = Path(
    os.environ.get("CLAUDE_USAGE_MIGRATIONS_DIR", str(CLAUDE_DIR / "usage_migrations"))
)
SYNC_DIR = Path(os.environ.get("CLAUDE_USAGE_SYNC_DIR", str(CLAUDE_DIR / "usage_sync")))
LAYOUT_CONFIG = Path(
    os.environ.get("CLAUDE_USAGE_LAYOUT", str(CLAUDE_DIR / "usage_layout.json"))
)
PLUGINS_CONFIG = Path(
    os.environ.get("CLAUDE_USAGE_PLUGINS_CONFIG", str(CLAUDE_DIR / "usage_plugins.json"))
)

# ── Webhooks / circuit breaker / retention / auth / search / SSE / tags ──────
WEBHOOKS_ENABLED = os.environ.get("CLAUDE_USAGE_WEBHOOKS", "0") == "1"
CIRCUIT_BREAKER_ENABLED = os.environ.get("CLAUDE_USAGE_CIRCUIT_BREAKER", "0") == "1"
CIRCUIT_BREAKER_ACTION = os.environ.get("CLAUDE_USAGE_CIRCUIT_BREAKER_ACTION", "warn")
RETENTION_DAYS = int(os.environ.get("CLAUDE_USAGE_RETENTION_DAYS", "90"))
AUTH_SECRET_FILE = Path(
    os.environ.get("CLAUDE_USAGE_AUTH_SECRET", str(CLAUDE_DIR / "usage_auth_secret"))
)
FTS_ENABLED = os.environ.get("CLAUDE_USAGE_FTS", "1") == "1"
SSE_ENABLED = os.environ.get("CLAUDE_USAGE_SSE", "1") == "1"
TAGS_FILE = Path(os.environ.get("CLAUDE_USAGE_TAGS", str(CLAUDE_DIR / "usage_tags.json")))


def calc_cost_with_pricing(
    pricing_override: dict, model: str, inp: int, out: int, cache_read: int, cache_creation: int
) -> float:
    """Calculate cost with custom pricing (for what-if simulator)."""
    p = pricing_override.get(model)
    if p is None:
        for key in pricing_override:
            if key != "default" and model and model.startswith(key):
                p = pricing_override[key]
                break
    if p is None:
        ml = (model or "").lower()
        if "opus" in ml:
            p = pricing_override.get("claude-opus-4-6", pricing_override.get("default", {}))
        elif "sonnet" in ml:
            p = pricing_override.get("claude-sonnet-4-6", pricing_override.get("default", {}))
        elif "haiku" in ml:
            p = pricing_override.get("claude-haiku-4-5", pricing_override.get("default", {}))
    if p is None:
        p = pricing_override.get("default", {})
    return (
        inp * p.get("input", 0) / 1_000_000
        + out * p.get("output", 0) / 1_000_000
        + cache_read * p.get("cache_read", 0) / 1_000_000
        + cache_creation * p.get("cache_write", 0) / 1_000_000
    )
