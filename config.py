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
