"""
insights.py - Proactive cost awareness engine for claude-usage.

Surfaces spending patterns early so developers naturally adjust behavior
before guardrails are needed.  Runs after every scan and produces a brief
"cost pulse" — a few lines of context that answer:

  - How much have I spent today, and is that unusual?
  - Am I using caching well?
  - Which session is costing the most, and why?
  - What's the one thing I should know right now?

The pulse is designed to be glanceable (5 lines max in CLI), not alarming.
It shifts behavior through awareness, not enforcement.
"""

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from config import DB_PATH, DAILY_LIMIT_USD, calc_cost


def _query_today_by_model(conn, today_str: str) -> list:
    return conn.execute("""
        SELECT COALESCE(model, 'unknown') as model,
               SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
               COUNT(*) as turns, COUNT(DISTINCT session_id) as sessions
        FROM turns WHERE substr(timestamp, 1, 10) = ?
        GROUP BY model
    """, (today_str,)).fetchall()


def _query_baseline(conn, window_start: str, today_str: str) -> list:
    return conn.execute("""
        SELECT substr(timestamp, 1, 10) as day,
               COALESCE(model, 'unknown') as model,
               SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
               SUM(input_tokens + output_tokens) as total_tokens,
               COUNT(*) as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) >= ? AND substr(timestamp, 1, 10) < ?
        GROUP BY day, model ORDER BY day
    """, (window_start, today_str)).fetchall()


def _query_top_sessions(conn, today_str: str, limit: int = 5) -> list:
    return conn.execute("""
        SELECT session_id,
               COALESCE(
                   (SELECT project_name FROM sessions WHERE session_id = t.session_id),
                   'unknown'
               ) as project,
               COALESCE(model, 'unknown') as model,
               SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
               COUNT(*) as turns
        FROM turns t
        WHERE substr(timestamp, 1, 10) = ?
        GROUP BY session_id
        ORDER BY inp + out DESC
        LIMIT ?
    """, (today_str, limit)).fetchall()


def _query_hour_of_day(conn, today_str: str) -> dict:
    """Get current hour's spend and the hour with peak spend today."""
    rows = conn.execute("""
        SELECT CAST(substr(timestamp, 12, 2) AS INTEGER) as hour,
               COALESCE(model, 'unknown') as model,
               SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
        GROUP BY hour, model ORDER BY hour
    """, (today_str,)).fetchall()

    hourly_costs = {}
    for r in rows:
        h = r["hour"]
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0,
                         r["cr"] or 0, r["cc"] or 0)
        hourly_costs[h] = hourly_costs.get(h, 0) + cost

    return hourly_costs


def compute_cache_efficiency(inp: int, cache_read: int, cache_creation: int) -> float | None:
    """Cache hit ratio: what fraction of cacheable tokens were served from cache.

    Returns 0.0–1.0, or None if caching wasn't used at all.
    """
    cacheable = cache_read + cache_creation
    if cacheable == 0:
        return None
    return cache_read / cacheable


def compute_efficiency_score(today_cost: float, avg_cost: float,
                             cache_efficiency: float | None,
                             model_mix: dict) -> int:
    """Composite 0–100 score reflecting how cost-efficiently you're working.

    Components (weighted):
      40% — Cache efficiency (higher = better)
      30% — Model mix (more haiku/sonnet vs opus = better)
      30% — Spend trajectory (at or below baseline = better)
    """
    # Cache component: 0–40
    if cache_efficiency is not None:
        cache_score = cache_efficiency * 40
    else:
        cache_score = 20  # neutral if no caching data

    # Model mix: fraction of turns on cheaper models → 0–30
    total_turns = sum(model_mix.values()) or 1
    cheap_turns = model_mix.get("sonnet", 0) + model_mix.get("haiku", 0)
    model_score = (cheap_turns / total_turns) * 30

    # Trajectory: ratio of today's cost to baseline avg → 0–30
    if avg_cost > 0 and today_cost > 0:
        ratio = today_cost / avg_cost
        if ratio <= 1.0:
            traj_score = 30
        elif ratio <= 2.0:
            traj_score = 30 * (2.0 - ratio)
        else:
            traj_score = 0
    else:
        traj_score = 30  # no baseline yet = neutral

    return max(0, min(100, int(cache_score + model_score + traj_score)))


def _pick_nudge(today_cost: float, avg_cost: float,
                cache_efficiency: float | None,
                model_mix: dict, top_session: dict | None) -> str | None:
    """Pick the single most impactful behavioral nudge."""
    nudges = []

    # Spend trajectory nudge
    if avg_cost > 0 and today_cost > avg_cost * 1.5:
        ratio = today_cost / avg_cost
        nudges.append((
            ratio * 10,
            f"Today's spend is {ratio:.1f}x your 7-day average — "
            f"review your most expensive session below"
        ))

    # Cache efficiency nudge
    if cache_efficiency is not None and cache_efficiency < 0.3:
        nudges.append((
            (1 - cache_efficiency) * 8,
            f"Cache hit rate is {cache_efficiency:.0%} — "
            f"shorter, focused sessions improve cache reuse"
        ))

    # Model tier nudge
    total_turns = sum(model_mix.values()) or 1
    opus_pct = model_mix.get("opus", 0) / total_turns
    if opus_pct > 0.5 and total_turns > 5:
        sonnet_cost = 3.69  # per MTok input
        opus_cost = 6.15
        savings_pct = int((1 - sonnet_cost / opus_cost) * 100)
        nudges.append((
            opus_pct * 7,
            f"{opus_pct:.0%} of turns used Opus — "
            f"Sonnet handles most tasks at {savings_pct}% lower cost"
        ))

    # Top session dominance nudge
    if top_session and today_cost > 0:
        sess_cost = top_session.get("cost", 0)
        if sess_cost > today_cost * 0.6:
            project = top_session.get("project", "unknown")
            nudges.append((
                5,
                f"One session ({project}) accounts for "
                f"{sess_cost / today_cost:.0%} of today's cost"
            ))

    if not nudges:
        return None

    nudges.sort(key=lambda x: x[0], reverse=True)
    return nudges[0][1]


def generate_pulse(db_path: Path = DB_PATH, window_days: int = 7) -> dict:
    """Generate the cost pulse — a brief snapshot of spending awareness.

    Returns a dict suitable for CLI display, API response, or dashboard card.
    """
    if not db_path.exists():
        return {"available": False}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    today_str = date.today().isoformat()
    window_start = (date.today() - timedelta(days=window_days)).isoformat()

    # Today's totals by model
    today_rows = _query_today_by_model(conn, today_str)
    if not today_rows:
        conn.close()
        return {"available": True, "today_cost": 0, "message": "No usage today yet."}

    today_cost = 0.0
    today_inp = today_out = today_cr = today_cc = today_turns = 0
    model_mix = {}

    for r in today_rows:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0,
                         r["cr"] or 0, r["cc"] or 0)
        today_cost += cost
        today_inp += r["inp"] or 0
        today_out += r["out"] or 0
        today_cr += r["cr"] or 0
        today_cc += r["cc"] or 0
        today_turns += r["turns"]

        m = (r["model"] or "").lower()
        tier = ("opus" if "opus" in m else
                "sonnet" if "sonnet" in m else
                "haiku" if "haiku" in m else "other")
        model_mix[tier] = model_mix.get(tier, 0) + r["turns"]

    # Baseline: daily cost average over window
    baseline_rows = _query_baseline(conn, window_start, today_str)
    from collections import defaultdict
    daily_costs = defaultdict(float)
    for r in baseline_rows:
        daily_costs[r["day"]] += calc_cost(
            r["model"], r["inp"] or 0, r["out"] or 0,
            r["cr"] or 0, r["cc"] or 0)

    baseline_days = list(daily_costs.values())
    avg_cost = sum(baseline_days) / len(baseline_days) if baseline_days else 0

    # Cache efficiency
    cache_eff = compute_cache_efficiency(today_inp, today_cr, today_cc)

    # Top session
    top_sessions = _query_top_sessions(conn, today_str, limit=3)
    top_session = None
    top_sessions_out = []
    for s in top_sessions:
        cost = calc_cost(s["model"], s["inp"] or 0, s["out"] or 0,
                         s["cr"] or 0, s["cc"] or 0)
        entry = {
            "session_id": s["session_id"],
            "project": s["project"],
            "model": s["model"],
            "turns": s["turns"],
            "cost": round(cost, 4),
        }
        top_sessions_out.append(entry)
        if top_session is None:
            top_session = entry

    # Hourly pattern
    hourly = _query_hour_of_day(conn, today_str)
    current_hour = datetime.now().hour
    pace_cost = None
    if hourly and current_hour > 0:
        spent_so_far = sum(hourly.values())
        hours_elapsed = current_hour + datetime.now().minute / 60
        pace_cost = round((spent_so_far / hours_elapsed) * 24, 4) if hours_elapsed > 0 else None

    conn.close()

    # Efficiency score
    score = compute_efficiency_score(today_cost, avg_cost, cache_eff, model_mix)

    # Trend direction
    if avg_cost > 0:
        trend_ratio = today_cost / avg_cost
        if trend_ratio <= 0.8:
            trend = "below"
        elif trend_ratio <= 1.2:
            trend = "normal"
        elif trend_ratio <= 2.0:
            trend = "elevated"
        else:
            trend = "high"
    else:
        trend = "new"

    # Behavioral nudge
    nudge = _pick_nudge(today_cost, avg_cost, cache_eff, model_mix, top_session)

    return {
        "available": True,
        "today_cost": round(today_cost, 4),
        "avg_daily_cost": round(avg_cost, 4),
        "trend": trend,
        "trend_ratio": round(today_cost / avg_cost, 2) if avg_cost > 0 else None,
        "cache_efficiency": round(cache_eff, 3) if cache_eff is not None else None,
        "efficiency_score": score,
        "turns_today": today_turns,
        "model_mix": model_mix,
        "top_sessions": top_sessions_out,
        "pace_eod_cost": pace_cost,
        "daily_limit": DAILY_LIMIT_USD if DAILY_LIMIT_USD > 0 else None,
        "nudge": nudge,
    }


# ── CLI formatting ────────────────────────────────────────────────────────────

_TREND_SYMBOLS = {
    "below":    "v",
    "normal":   "-",
    "elevated": "^",
    "high":     "^^",
    "new":      "~",
}


def format_pulse_cli(pulse: dict) -> str:
    """Format the pulse as a compact CLI block (3–5 lines)."""
    if not pulse.get("available"):
        return ""

    if pulse.get("today_cost", 0) == 0:
        return ""

    lines = []

    # Line 1: spend + trend + efficiency score
    cost = pulse["today_cost"]
    avg = pulse.get("avg_daily_cost", 0)
    trend = pulse.get("trend", "new")
    sym = _TREND_SYMBOLS.get(trend, "")
    score = pulse.get("efficiency_score", 0)

    score_bar = _score_bar(score)

    if avg > 0:
        ratio = pulse.get("trend_ratio", 0)
        lines.append(
            f"  {sym} ${cost:.4f} today  "
            f"({ratio:.1f}x avg ${avg:.4f})  "
            f"efficiency {score_bar} {score}/100"
        )
    else:
        lines.append(
            f"  {sym} ${cost:.4f} today  "
            f"efficiency {score_bar} {score}/100"
        )

    # Line 2: cache efficiency + model mix
    parts = []
    ce = pulse.get("cache_efficiency")
    if ce is not None:
        parts.append(f"cache hit {ce:.0%}")
    mix = pulse.get("model_mix", {})
    if mix:
        total = sum(mix.values()) or 1
        mix_parts = []
        for tier in ("opus", "sonnet", "haiku", "other"):
            pct = mix.get(tier, 0) / total
            if pct > 0:
                mix_parts.append(f"{tier} {pct:.0%}")
        if mix_parts:
            parts.append("  ".join(mix_parts))
    if parts:
        lines.append("  " + "  |  ".join(parts))

    # Line 3: top session (only if it's noteworthy)
    top = pulse.get("top_sessions", [])
    if top and cost > 0:
        t = top[0]
        pct = t["cost"] / cost * 100 if cost > 0 else 0
        project = t["project"] or "unknown"
        if len(project) > 25:
            project = "..." + project[-22:]
        lines.append(
            f"  top session: {project}  "
            f"${t['cost']:.4f} ({pct:.0f}%)  "
            f"{t['turns']} turns  {t['model']}"
        )

    # Line 4: nudge (the behavioral insight)
    nudge = pulse.get("nudge")
    if nudge:
        lines.append(f"  >> {nudge}")

    return "\n".join(lines)


def _score_bar(score: int, width: int = 10) -> str:
    """Render a small visual bar for the efficiency score (ASCII-safe)."""
    filled = round(score / 100 * width)
    return "#" * filled + "-" * (width - filled)
