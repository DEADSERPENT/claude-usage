"""
optimizer.py - Cost optimization engine for claude-usage.

Analyzes usage patterns and suggests cost-saving strategies:
- Model downgrade opportunities (Opus -> Sonnet -> Haiku)
- Cache efficiency analysis
- Session consolidation suggestions
- Peak-hour avoidance recommendations
"""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from config import DB_PATH, PRICING, calc_cost


def _model_tier(model: str) -> str:
    m = (model or "").lower()
    if "opus" in m:   return "opus"
    if "sonnet" in m: return "sonnet"
    if "haiku" in m:  return "haiku"
    return "unknown"


def _tier_pricing(tier: str) -> dict:
    """Get representative pricing for a model tier."""
    tier_map = {
        "opus":   "claude-opus-4-6",
        "sonnet": "claude-sonnet-4-6",
        "haiku":  "claude-haiku-4-6",
    }
    key = tier_map.get(tier, "default")
    return PRICING.get(key, PRICING["default"])


def analyze_cache_thrashing(db_path: Path = DB_PATH, days: int = 30) -> list[dict]:
    """Detect sessions with cache thrashing patterns."""
    if not db_path.exists():
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    sessions = conn.execute("""
        SELECT session_id,
               SUM(cache_creation_tokens) as total_cc,
               SUM(cache_read_tokens) as total_cr,
               SUM(input_tokens) as total_inp,
               COUNT(*) as turn_count
        FROM turns
        WHERE substr(timestamp, 1, 10) >= ?
        GROUP BY session_id
        HAVING total_cc > 0
        ORDER BY total_cc DESC
    """, (cutoff,)).fetchall()

    thrashing = []
    for s in sessions:
        cc = s["total_cc"] or 0
        cr = s["total_cr"] or 0
        inp = s["total_inp"] or 0
        turns = s["turn_count"] or 0

        if turns < 3 or cc < 10000:
            continue

        # Cache thrashing: high creation but low read ratio
        read_ratio = cr / max(cc, 1)
        creation_per_turn = cc / max(turns, 1)

        if read_ratio < 0.5 and creation_per_turn > 5000:
            # Get session details
            sess_row = conn.execute(
                "SELECT project_name, model, git_branch FROM sessions WHERE session_id = ?",
                (s["session_id"],)
            ).fetchone()

            wasted_cost = calc_cost(
                (sess_row["model"] if sess_row else "default"),
                0, 0, 0, cc
            ) - calc_cost(
                (sess_row["model"] if sess_row else "default"),
                cc, 0, 0, 0
            )

            thrashing.append({
                "session_id": s["session_id"][:8],
                "full_session_id": s["session_id"],
                "project": (sess_row["project_name"] if sess_row else "unknown"),
                "model": (sess_row["model"] if sess_row else "unknown"),
                "turns": turns,
                "cache_creation": cc,
                "cache_read": cr,
                "read_ratio": round(read_ratio, 3),
                "creation_per_turn": int(creation_per_turn),
                "wasted_cost": round(abs(wasted_cost), 6),
                "severity": "high" if read_ratio < 0.2 else "medium",
            })

    conn.close()
    thrashing.sort(key=lambda x: x["wasted_cost"], reverse=True)
    return thrashing[:20]


def analyze(db_path: Path = DB_PATH, days: int = 30) -> dict:
    """
    Run full optimization analysis. Returns dict with:
    - suggestions: list of actionable recommendations
    - model_breakdown: usage per model tier
    - potential_savings: estimated savings in USD
    - cache_efficiency: cache hit/miss ratio analysis
    - cache_thrash_sessions: sessions with heavy cache writes vs reads
    - cache_thrashing: profiler output for sessions with creation-heavy, read-light cache use
    """
    if not db_path.exists():
        return {"suggestions": [], "model_breakdown": {}, "potential_savings": 0,
                "cache_efficiency": {}, "cache_thrash_sessions": [], "cache_thrashing": []}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    # Model usage breakdown
    model_rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') as model,
               SUM(input_tokens) as inp, SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
               COUNT(*) as turns,
               COUNT(DISTINCT session_id) as sessions
        FROM turns
        WHERE substr(timestamp, 1, 10) >= ?
        GROUP BY model
        ORDER BY inp + out DESC
    """, (cutoff,)).fetchall()

    # Hourly distribution for peak-hour analysis
    hourly_rows = conn.execute("""
        SELECT CAST(substr(timestamp, 12, 2) AS INTEGER) as hour,
               SUM(input_tokens + output_tokens) as tokens,
               COUNT(*) as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) >= ?
        GROUP BY hour ORDER BY hour
    """, (cutoff,)).fetchall()

    # Session efficiency (short sessions = wasted cache warm-up)
    session_rows = conn.execute("""
        SELECT session_id, turn_count,
               total_input_tokens as inp, total_output_tokens as out,
               total_cache_read as cr, total_cache_creation as cc,
               model
        FROM sessions
        WHERE last_timestamp >= ?
        ORDER BY last_timestamp DESC
    """, (cutoff,)).fetchall()

    # Cache ratio per model
    cache_rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') as model,
               SUM(cache_read_tokens) as cr,
               SUM(cache_creation_tokens) as cc,
               SUM(input_tokens) as inp
        FROM turns
        WHERE substr(timestamp, 1, 10) >= ?
        GROUP BY model
    """, (cutoff,)).fetchall()

    # Tool usage pattern (some tools use lots of tokens)
    tool_rows = conn.execute("""
        SELECT tool_name,
               SUM(input_tokens + output_tokens) as tokens,
               COUNT(*) as count,
               AVG(input_tokens + output_tokens) as avg_tokens
        FROM turns
        WHERE tool_name IS NOT NULL AND substr(timestamp, 1, 10) >= ?
        GROUP BY tool_name
        ORDER BY tokens DESC
        LIMIT 10
    """, (cutoff,)).fetchall()

    thrash_query_rows = conn.execute("""
        SELECT session_id,
               SUM(cache_creation_tokens) as total_create,
               SUM(cache_read_tokens) as total_read,
               COUNT(*) as turns
        FROM turns
        WHERE substr(timestamp, 1, 10) >= ?
          AND cache_creation_tokens > 0
        GROUP BY session_id
        HAVING total_create > total_read * 3
           AND total_create > 50000
           AND turns >= 3
        ORDER BY total_create DESC
        LIMIT 10
    """, (cutoff,)).fetchall()

    conn.close()

    suggestions = []
    model_breakdown = {}
    total_cost = 0.0
    total_savings = 0.0

    # ── Model downgrade analysis ──────────────────────────────────────────────
    tier_usage = {"opus": {"tokens": 0, "cost": 0, "turns": 0, "sessions": 0},
                  "sonnet": {"tokens": 0, "cost": 0, "turns": 0, "sessions": 0},
                  "haiku": {"tokens": 0, "cost": 0, "turns": 0, "sessions": 0},
                  "unknown": {"tokens": 0, "cost": 0, "turns": 0, "sessions": 0}}

    for r in model_rows:
        tier = _model_tier(r["model"])
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0,
                         r["cr"] or 0, r["cc"] or 0)
        total_cost += cost
        tokens = (r["inp"] or 0) + (r["out"] or 0)

        model_breakdown[r["model"]] = {
            "tokens": tokens,
            "cost": round(cost, 4),
            "turns": r["turns"],
            "sessions": r["sessions"],
            "tier": tier,
        }

        if tier in tier_usage:
            tier_usage[tier]["tokens"] += tokens
            tier_usage[tier]["cost"] += cost
            tier_usage[tier]["turns"] += r["turns"]
            tier_usage[tier]["sessions"] += r["sessions"]

    # Opus -> Sonnet savings
    if tier_usage["opus"]["cost"] > 0:
        opus_cost = tier_usage["opus"]["cost"]
        opus_inp = sum(r["inp"] or 0 for r in model_rows if _model_tier(r["model"]) == "opus")
        opus_out = sum(r["out"] or 0 for r in model_rows if _model_tier(r["model"]) == "opus")
        opus_cr = sum(r["cr"] or 0 for r in model_rows if _model_tier(r["model"]) == "opus")
        opus_cc = sum(r["cc"] or 0 for r in model_rows if _model_tier(r["model"]) == "opus")

        sonnet_p = _tier_pricing("sonnet")
        sonnet_cost = (
            opus_inp * sonnet_p["input"] / 1e6 +
            opus_out * sonnet_p["output"] / 1e6 +
            opus_cr  * sonnet_p["cache_read"] / 1e6 +
            opus_cc  * sonnet_p["cache_write"] / 1e6
        )
        savings = opus_cost - sonnet_cost
        pct = (savings / opus_cost * 100) if opus_cost else 0

        total_opus_pct = (tier_usage["opus"]["turns"] /
                          max(sum(t["turns"] for t in tier_usage.values()), 1) * 100)

        if total_opus_pct > 30:
            suggestions.append({
                "type": "model_downgrade",
                "priority": "high",
                "title": "Consider Sonnet for routine tasks",
                "detail": (f"Opus accounts for {total_opus_pct:.0f}% of your turns. "
                           f"Switching to Sonnet would save ~${savings:.4f} ({pct:.0f}%) "
                           f"over the last {days} days."),
                "savings": round(savings, 4),
            })
            total_savings += savings

    # Sonnet -> Haiku for simple tasks
    if tier_usage["sonnet"]["cost"] > 0:
        sonnet_cost = tier_usage["sonnet"]["cost"]
        sonnet_inp = sum(r["inp"] or 0 for r in model_rows if _model_tier(r["model"]) == "sonnet")
        sonnet_out = sum(r["out"] or 0 for r in model_rows if _model_tier(r["model"]) == "sonnet")
        sonnet_cr = sum(r["cr"] or 0 for r in model_rows if _model_tier(r["model"]) == "sonnet")
        sonnet_cc = sum(r["cc"] or 0 for r in model_rows if _model_tier(r["model"]) == "sonnet")

        haiku_p = _tier_pricing("haiku")
        haiku_cost = (
            sonnet_inp * haiku_p["input"] / 1e6 +
            sonnet_out * haiku_p["output"] / 1e6 +
            sonnet_cr  * haiku_p["cache_read"] / 1e6 +
            sonnet_cc  * haiku_p["cache_write"] / 1e6
        )
        partial_savings = (sonnet_cost - haiku_cost) * 0.3  # assume 30% could use Haiku

        if partial_savings > 0.01:
            suggestions.append({
                "type": "model_downgrade",
                "priority": "medium",
                "title": "Use Haiku for simple/short tasks",
                "detail": (f"~30% of Sonnet tasks could potentially use Haiku, "
                           f"saving ~${partial_savings:.4f} over {days} days."),
                "savings": round(partial_savings, 4),
            })
            total_savings += partial_savings

    # ── Cache efficiency analysis ─────────────────────────────────────────────
    cache_efficiency = {}
    for r in cache_rows:
        cr = r["cr"] or 0
        cc = r["cc"] or 0
        inp = r["inp"] or 0
        total_input = inp + cr + cc
        hit_ratio = cr / max(total_input, 1)
        cache_efficiency[r["model"]] = {
            "cache_read": cr,
            "cache_creation": cc,
            "raw_input": inp,
            "hit_ratio": round(hit_ratio, 4),
        }
        if hit_ratio < 0.3 and total_input > 100_000:
            savings_if_cached = calc_cost(r["model"], inp * 0.5, 0, 0, 0) * 0.9
            suggestions.append({
                "type": "cache_optimization",
                "priority": "medium",
                "title": f"Low cache hit ratio for {r['model']}",
                "detail": (f"Cache hit ratio is {hit_ratio:.0%} for {r['model']}. "
                           f"Longer sessions and context reuse could improve this. "
                           f"Potential savings: ~${savings_if_cached:.4f}"),
                "savings": round(savings_if_cached, 4),
            })
            total_savings += savings_if_cached

    # ── Cache thrashing detection ──────────────────────────────────────────────
    cache_thrash_sessions = []
    total_thrash_waste = 0.0
    for tr in thrash_query_rows:
        excess_creation = (tr["total_create"] or 0) - (tr["total_read"] or 0)
        waste_cost = excess_creation * _tier_pricing("sonnet").get("cache_write", 0) / 1e6
        total_thrash_waste += waste_cost
        cache_thrash_sessions.append({
            "session_id": tr["session_id"][:8],
            "cache_creation": tr["total_create"] or 0,
            "cache_read": tr["total_read"] or 0,
            "turns": tr["turns"],
            "waste_cost": round(waste_cost, 4),
        })

    if cache_thrash_sessions:
        suggestions.append({
            "type": "cache_thrashing",
            "priority": "high" if total_thrash_waste > 0.10 else "medium",
            "title": f"Cache thrashing detected in {len(cache_thrash_sessions)} session(s)",
            "detail": (f"Sessions repeatedly create cache without reading it, "
                       f"wasting ~${total_thrash_waste:.4f}. Consider consolidating "
                       f"prompts to reuse cached context."),
            "savings": round(total_thrash_waste * 0.7, 4),
            "thrash_sessions": cache_thrash_sessions,
        })
        total_savings += total_thrash_waste * 0.7

    # ── Short session analysis ────────────────────────────────────────────────
    short_sessions = [s for s in session_rows if (s["turn_count"] or 0) <= 2]
    if len(short_sessions) > len(session_rows) * 0.3 and len(short_sessions) > 5:
        short_cost = sum(
            calc_cost(s["model"], s["inp"] or 0, s["out"] or 0, s["cr"] or 0, s["cc"] or 0)
            for s in short_sessions
        )
        suggestions.append({
            "type": "session_consolidation",
            "priority": "low",
            "title": "Many short sessions detected",
            "detail": (f"{len(short_sessions)} of {len(session_rows)} sessions have "
                       f"<=2 turns (${short_cost:.4f} cost). Consolidating tasks into "
                       f"longer sessions improves cache efficiency."),
            "savings": round(short_cost * 0.1, 4),  # ~10% savings from better caching
        })

    # ── Peak hour analysis ────────────────────────────────────────────────────
    if hourly_rows:
        hourly_data = {r["hour"]: {"tokens": r["tokens"] or 0, "turns": r["turns"] or 0}
                       for r in hourly_rows}
        peak_hour = max(hourly_data, key=lambda h: hourly_data[h]["tokens"])
        peak_tokens = hourly_data[peak_hour]["tokens"]
        avg_tokens = sum(d["tokens"] for d in hourly_data.values()) / max(len(hourly_data), 1)

        if peak_tokens > avg_tokens * 3 and peak_tokens > 500_000:
            suggestions.append({
                "type": "usage_pattern",
                "priority": "info",
                "title": f"Peak usage at {peak_hour}:00",
                "detail": (f"Hour {peak_hour}:00 has {peak_tokens:,} tokens "
                           f"({peak_tokens/avg_tokens:.1f}x average). "
                           f"Spreading work more evenly can help manage costs."),
                "savings": 0,
            })

    # ── Heavy tool analysis ───────────────────────────────────────────────────
    if tool_rows:
        top_tool = tool_rows[0]
        if (top_tool["avg_tokens"] or 0) > 50_000:
            suggestions.append({
                "type": "tool_efficiency",
                "priority": "info",
                "title": f"Tool '{top_tool['tool_name']}' is token-heavy",
                "detail": (f"'{top_tool['tool_name']}' averages {int(top_tool['avg_tokens']):,} "
                           f"tokens/use ({top_tool['count']} uses, {top_tool['tokens']:,} total). "
                           f"Consider if all uses are necessary."),
                "savings": 0,
            })

    # Sort by priority
    priority_order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    suggestions.sort(key=lambda s: priority_order.get(s["priority"], 9))

    # Cache thrashing analysis
    cache_thrashing = analyze_cache_thrashing(db_path, days)
    if cache_thrashing:
        total_wasted = sum(t["wasted_cost"] for t in cache_thrashing)
        if total_wasted > 0.01:
            suggestions.append({
                "type": "cache_thrashing",
                "priority": "high",
                "title": "Cache thrashing detected",
                "detail": (f"{len(cache_thrashing)} session(s) are frequently recreating cache "
                           f"without reading it. Estimated waste: ${total_wasted:.4f}"),
                "savings": round(total_wasted, 4),
            })
            total_savings += total_wasted

    return {
        "suggestions": suggestions,
        "model_breakdown": model_breakdown,
        "tier_usage": {k: {**v, "cost": round(v["cost"], 4)} for k, v in tier_usage.items()},
        "potential_savings": round(total_savings, 4),
        "total_cost": round(total_cost, 4),
        "cache_efficiency": cache_efficiency,
        "cache_thrash_sessions": cache_thrash_sessions,
        "cache_thrashing": cache_thrashing,
        "analysis_period_days": days,
    }


def format_report(analysis: dict) -> str:
    """Format optimization analysis into a readable CLI report."""
    lines = []
    lines.append("")
    lines.append("=" * 60)
    lines.append("  Cost Optimization Report")
    lines.append("=" * 60)

    lines.append(f"\n  Analysis period: last {analysis['analysis_period_days']} days")
    lines.append(f"  Total cost:      ${analysis['total_cost']:.4f}")
    lines.append(f"  Potential savings: ${analysis['potential_savings']:.4f}")

    if analysis["potential_savings"] > 0 and analysis["total_cost"] > 0:
        pct = analysis["potential_savings"] / analysis["total_cost"] * 100
        lines.append(f"  Savings potential: {pct:.0f}%")

    # Model tier breakdown
    lines.append(f"\n  {'─'*56}")
    lines.append("  Model Tier Usage:")
    for tier, data in analysis.get("tier_usage", {}).items():
        if data["turns"] > 0:
            lines.append(f"    {tier:<10}  turns={data['turns']:<6}  "
                         f"tokens={data['tokens']:>12,}  cost=${data['cost']:.4f}")

    # Cache efficiency
    if analysis.get("cache_efficiency"):
        lines.append(f"\n  {'─'*56}")
        lines.append("  Cache Efficiency:")
        for model, data in analysis["cache_efficiency"].items():
            ratio_bar = "█" * int(data["hit_ratio"] * 20) + "░" * (20 - int(data["hit_ratio"] * 20))
            lines.append(f"    {model[:30]:<30}  [{ratio_bar}] {data['hit_ratio']:.0%}")

    # Suggestions
    if analysis["suggestions"]:
        lines.append(f"\n  {'─'*56}")
        lines.append("  Recommendations:")
        priority_icons = {"high": "!!!", "medium": " !!", "low": "  !", "info": "  i"}
        for s in analysis["suggestions"]:
            icon = priority_icons.get(s["priority"], "  ?")
            lines.append(f"\n  [{icon}] {s['title']}")
            lines.append(f"      {s['detail']}")
            if s.get("savings", 0) > 0:
                lines.append(f"      Estimated savings: ${s['savings']:.4f}")
    else:
        lines.append("\n  No optimization suggestions at this time.")

    lines.append(f"\n{'='*60}\n")
    return "\n".join(lines)
