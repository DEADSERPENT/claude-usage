"""
anomaly.py - Anomaly detection engine for claude-usage.

Detects abnormal usage spikes using statistical analysis (no AI/ML required).
Uses a rolling window moving average + standard deviation approach to identify
when metrics deviate significantly from the baseline.

Detected anomalies are stored in the `anomalies` table and can be queried
via CLI or API.
"""

import sqlite3
import math
from datetime import datetime, date, timedelta
from pathlib import Path

from config import DB_PATH, PRICING, ANOMALY_WINDOW_DAYS, ANOMALY_SPIKE_FACTOR


def _calc_cost(model: str, inp: int, out: int, cr: int, cc: int) -> float:
    p = PRICING.get(model)
    if p is None:
        for key in PRICING:
            if key != "default" and model.startswith(key):
                p = PRICING[key]
                break
    if p is None:
        p = PRICING.get("default", {})
    return (
        inp * p.get("input", 0) / 1_000_000 +
        out * p.get("output", 0) / 1_000_000 +
        cr  * p.get("cache_read", 0) / 1_000_000 +
        cc  * p.get("cache_write", 0) / 1_000_000
    )


def _mean_stddev(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, 0.0
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)
    return mean, math.sqrt(variance)


def detect_anomalies(db_path: Path = DB_PATH, window_days: int = None,
                     spike_factor: float = None) -> list[dict]:
    """
    Analyze recent usage against historical baseline and detect anomalies.

    Returns list of detected anomaly dicts.
    """
    if window_days is None:
        window_days = ANOMALY_WINDOW_DAYS
    if spike_factor is None:
        spike_factor = ANOMALY_SPIKE_FACTOR

    if not db_path.exists():
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    today_str = date.today().isoformat()
    window_start = (date.today() - timedelta(days=window_days)).isoformat()

    # Get daily aggregates for the baseline window (excluding today)
    daily_rows = conn.execute("""
        SELECT substr(timestamp, 1, 10) as day,
               SUM(input_tokens + output_tokens) as total_tokens,
               SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
               COUNT(*) as turns,
               COUNT(DISTINCT session_id) as sessions
        FROM turns
        WHERE substr(timestamp, 1, 10) >= ? AND substr(timestamp, 1, 10) < ?
        GROUP BY day ORDER BY day
    """, (window_start, today_str)).fetchall()

    # Get today's aggregates
    today_row = conn.execute("""
        SELECT SUM(input_tokens + output_tokens) as total_tokens,
               SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
               COUNT(*) as turns,
               COUNT(DISTINCT session_id) as sessions
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
    """, (today_str,)).fetchone()

    # Get today's cost by model
    today_cost_rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') as model,
               SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
        GROUP BY model
    """, (today_str,)).fetchall()

    # Get hourly data for today to detect intra-day spikes
    hourly_rows = conn.execute("""
        SELECT substr(timestamp, 1, 13) as hour,
               SUM(input_tokens + output_tokens) as total_tokens,
               COUNT(*) as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
        GROUP BY hour ORDER BY hour
    """, (today_str,)).fetchall()

    # Also check per-session anomalies
    session_rows = conn.execute("""
        SELECT session_id,
               SUM(input_tokens + output_tokens) as total_tokens,
               COUNT(*) as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) = ?
        GROUP BY session_id
        ORDER BY total_tokens DESC
        LIMIT 10
    """, (today_str,)).fetchall()

    anomalies = []

    # ── Daily token anomaly ───────────────────────────────────────────────────
    if daily_rows and today_row and today_row["total_tokens"]:
        baseline_tokens = [r["total_tokens"] or 0 for r in daily_rows]
        mean_tok, std_tok = _mean_stddev(baseline_tokens)
        today_tokens = today_row["total_tokens"] or 0

        if mean_tok > 0 and today_tokens > mean_tok + spike_factor * max(std_tok, mean_tok * 0.2):
            factor = today_tokens / mean_tok
            anomalies.append({
                "metric": "daily_tokens",
                "value": today_tokens,
                "baseline": mean_tok,
                "factor": round(factor, 2),
                "severity": "critical" if factor > spike_factor * 2 else "warning",
                "message": f"Token usage spike: {today_tokens:,} tokens today "
                           f"({factor:.1f}x the {window_days}-day avg of {int(mean_tok):,})",
            })

    # ── Daily cost anomaly ────────────────────────────────────────────────────
    if daily_rows and today_cost_rows:
        baseline_costs = []
        for r in daily_rows:
            # Rough cost estimate using default pricing for baseline
            cost = _calc_cost("default", r["inp"] or 0, r["out"] or 0,
                              r["cr"] or 0, r["cc"] or 0)
            baseline_costs.append(cost)

        today_cost = sum(
            _calc_cost(r["model"], r["inp"] or 0, r["out"] or 0,
                       r["cr"] or 0, r["cc"] or 0)
            for r in today_cost_rows
        )
        mean_cost, std_cost = _mean_stddev(baseline_costs)

        if mean_cost > 0 and today_cost > mean_cost + spike_factor * max(std_cost, mean_cost * 0.2):
            factor = today_cost / mean_cost
            anomalies.append({
                "metric": "daily_cost",
                "value": round(today_cost, 4),
                "baseline": round(mean_cost, 4),
                "factor": round(factor, 2),
                "severity": "critical" if factor > spike_factor * 2 else "warning",
                "message": f"Cost spike: ${today_cost:.4f} today "
                           f"({factor:.1f}x the {window_days}-day avg of ${mean_cost:.4f})",
            })

    # ── Daily turns anomaly ───────────────────────────────────────────────────
    if daily_rows and today_row and today_row["turns"]:
        baseline_turns = [r["turns"] or 0 for r in daily_rows]
        mean_turns, std_turns = _mean_stddev(baseline_turns)
        today_turns = today_row["turns"] or 0

        if mean_turns > 0 and today_turns > mean_turns + spike_factor * max(std_turns, mean_turns * 0.2):
            factor = today_turns / mean_turns
            anomalies.append({
                "metric": "daily_turns",
                "value": today_turns,
                "baseline": mean_turns,
                "factor": round(factor, 2),
                "severity": "warning",
                "message": f"Turn count spike: {today_turns} turns today "
                           f"({factor:.1f}x the {window_days}-day avg of {int(mean_turns)})",
            })

    # ── Hourly spike detection ────────────────────────────────────────────────
    if len(hourly_rows) >= 3:
        hourly_tokens = [r["total_tokens"] or 0 for r in hourly_rows]
        mean_hourly, std_hourly = _mean_stddev(hourly_tokens[:-1])  # exclude last (current) hour
        last_hour = hourly_tokens[-1]
        last_hour_label = hourly_rows[-1]["hour"]

        if mean_hourly > 0 and last_hour > mean_hourly + spike_factor * max(std_hourly, mean_hourly * 0.3):
            factor = last_hour / mean_hourly
            anomalies.append({
                "metric": "hourly_tokens",
                "value": last_hour,
                "baseline": mean_hourly,
                "factor": round(factor, 2),
                "severity": "warning",
                "message": f"Hourly spike at {last_hour_label}: "
                           f"{last_hour:,} tokens ({factor:.1f}x hourly avg)",
            })

    # ── Single-session token hog ──────────────────────────────────────────────
    if session_rows and today_row and today_row["total_tokens"]:
        today_total = today_row["total_tokens"] or 1
        for sr in session_rows[:3]:
            sess_tokens = sr["total_tokens"] or 0
            ratio = sess_tokens / today_total
            if ratio > 0.6 and sess_tokens > 100_000:
                anomalies.append({
                    "metric": "session_dominance",
                    "value": sess_tokens,
                    "baseline": today_total,
                    "factor": round(ratio, 2),
                    "severity": "info",
                    "message": f"Session {sr['session_id'][:8]}... accounts for "
                               f"{ratio:.0%} of today's tokens ({sess_tokens:,})",
                    "session_id": sr["session_id"],
                })

    # Store detected anomalies in DB
    for a in anomalies:
        conn.execute("""
            INSERT INTO anomalies (metric, value, baseline, factor, severity, message, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            a["metric"], a["value"], a["baseline"], a["factor"],
            a["severity"], a["message"], a.get("session_id"),
        ))
    conn.commit()
    conn.close()

    return anomalies


def get_recent_anomalies(db_path: Path = DB_PATH, days: int = 7, limit: int = 50) -> list[dict]:
    """Retrieve recent anomalies from the database."""
    if not db_path.exists():
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    rows = conn.execute("""
        SELECT id, detected_at, metric, value, baseline, factor,
               severity, message, session_id, acknowledged
        FROM anomalies
        WHERE detected_at >= ?
        ORDER BY detected_at DESC
        LIMIT ?
    """, (cutoff, limit)).fetchall()
    conn.close()

    return [dict(r) for r in rows]


def acknowledge_anomaly(db_path: Path, anomaly_id: int) -> bool:
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE anomalies SET acknowledged = 1 WHERE id = ?", (anomaly_id,))
    conn.commit()
    changed = conn.total_changes > 0
    conn.close()
    return changed
