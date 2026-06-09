"""
query_engine.py - Local analytics DSL for claude-usage.

Provides a mini query language for filtering and aggregating usage data
directly from the CLI, similar to a simplified SQL but purpose-built for
token analytics.

Syntax:
    model=sonnet AND tokens > 1M
    project=my-app OR branch=main
    cost > 0.50 AND date >= 2026-04-01
    turns > 100 AND model=opus

Supported fields:
    model, project, branch, session, date, user,
    tokens, input, output, cost, turns, cache_read, cache_creation,
    tool, duration

Operators: =, !=, >, <, >=, <=, ~  (~ is substring/contains match)
Connectors: AND, OR
Values: strings, numbers, suffixes (K, M, B for thousands/millions/billions)
"""

import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from config import DB_PATH, calc_cost


def _parse_number(val: str) -> float:
    """Parse number with optional K/M/B suffix."""
    val = val.strip()
    multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    if val and val[-1].upper() in multipliers:
        return float(val[:-1]) * multipliers[val[-1].upper()]
    return float(val)


# Field aliases that map to DB columns or computed values
FIELD_MAP = {
    "model":          "model",
    "project":        "project_name",
    "branch":         "git_branch",
    "session":        "session_id",
    "date":           "date",
    "user":           "user_id",
    "tokens":         "tokens",           # computed: input + output
    "input":          "input_tokens",
    "output":         "output_tokens",
    "cost":           "cost",             # computed
    "turns":          "turn_count",
    "cache_read":     "cache_read",
    "cache_creation": "cache_creation",
    "tool":           "tool_name",
    "duration":       "duration",         # computed
}

_TOKEN_RE = re.compile(
    r'(\w+)\s*(~|!=|>=|<=|>|<|=)\s*'
    r'("[^"]*"|\'[^\']*\'|[\w./$*:-]+)'
)

_CONNECTOR_RE = re.compile(r'\b(AND|OR)\b', re.IGNORECASE)


def _tokenize(query: str) -> list[dict]:
    """Parse query string into a list of condition dicts and connectors."""
    tokens = []
    pos = 0
    query = query.strip()

    while pos < len(query):
        # Skip whitespace
        while pos < len(query) and query[pos] in " \t":
            pos += 1
        if pos >= len(query):
            break

        # Check for connector
        conn_match = _CONNECTOR_RE.match(query, pos)
        if conn_match:
            tokens.append({"type": "connector", "value": conn_match.group(1).upper()})
            pos = conn_match.end()
            continue

        # Check for condition
        cond_match = _TOKEN_RE.match(query, pos)
        if cond_match:
            field = cond_match.group(1).lower()
            op = cond_match.group(2)
            val = cond_match.group(3).strip("\"'")
            tokens.append({"type": "condition", "field": field, "op": op, "value": val})
            pos = cond_match.end()
            continue

        pos += 1  # skip unrecognized character

    return tokens


def _evaluate_condition(row: dict, cond: dict) -> bool:
    """Evaluate a single condition against a row dict."""
    field = cond["field"]
    op = cond["op"]
    val = cond["value"]

    # Get the actual value from the row
    if field in ("tokens",):
        actual = (row.get("total_input_tokens", 0) or 0) + (row.get("total_output_tokens", 0) or 0)
    elif field == "input":
        actual = row.get("total_input_tokens", 0) or 0
    elif field == "output":
        actual = row.get("total_output_tokens", 0) or 0
    elif field == "cost":
        actual = calc_cost(
            row.get("model", "default"),
            row.get("total_input_tokens", 0) or 0,
            row.get("total_output_tokens", 0) or 0,
            row.get("total_cache_read", 0) or 0,
            row.get("total_cache_creation", 0) or 0,
        )
    elif field == "turns":
        actual = row.get("turn_count", 0) or 0
    elif field == "cache_read":
        actual = row.get("total_cache_read", 0) or 0
    elif field == "cache_creation":
        actual = row.get("total_cache_creation", 0) or 0
    elif field == "model":
        actual = row.get("model", "") or ""
    elif field == "project":
        actual = row.get("project_name", "") or ""
    elif field == "branch":
        actual = row.get("git_branch", "") or ""
    elif field == "session":
        actual = row.get("session_id", "") or ""
    elif field == "date":
        actual = (row.get("last_timestamp", "") or "")[:10]
    elif field == "user":
        actual = row.get("user_id", "default") or "default"
    elif field == "duration":
        try:
            from datetime import datetime
            t1 = datetime.fromisoformat(row["first_timestamp"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(row["last_timestamp"].replace("Z", "+00:00"))
            actual = (t2 - t1).total_seconds() / 60
        except Exception:
            actual = 0
    else:
        actual = row.get(field, "")

    # Numeric comparisons — try first for all relational operators.
    # On ValueError (e.g. a date string like "2026-06-01") we fall through to
    # lexicographic string comparison, which works correctly for ISO-8601 dates.
    if op in (">", "<", ">=", "<=") or (op in ("=", "!=") and isinstance(actual, (int, float))):
        try:
            val_num = _parse_number(val)
            actual_num = float(actual) if isinstance(actual, str) else actual
            if op == ">":  return actual_num > val_num
            if op == "<":  return actual_num < val_num
            if op == ">=": return actual_num >= val_num
            if op == "<=": return actual_num <= val_num
            if op == "=":  return abs(actual_num - val_num) < 0.0001
            if op == "!=": return abs(actual_num - val_num) >= 0.0001
        except (ValueError, TypeError):
            pass  # fall through to string comparison

    # String comparisons (also handles date/branch/project range ops)
    actual_str = str(actual).lower()
    val_str = val.lower()

    if op == "=":  return actual_str == val_str
    if op == "!=": return actual_str != val_str
    if op == "~":  return val_str in actual_str
    if op == ">":  return actual_str > val_str
    if op == "<":  return actual_str < val_str
    if op == ">=": return actual_str >= val_str
    if op == "<=": return actual_str <= val_str

    return False


_SQL_OP_MAP = {
    "=": "=",
    "!=": "!=",
    ">": ">",
    "<": "<",
    ">=": ">=",
    "<=": "<=",
    "~": "LIKE",
}


def _compile_condition_sql(cond: dict) -> tuple[str, list] | None:
    """Compile one DSL condition to SQL, or return None if unsupported."""
    field = cond["field"]
    op = cond["op"]
    val = cond["value"]
    sql_op = _SQL_OP_MAP.get(op)
    if not sql_op:
        return None

    if field == "tokens":
        expr = "(total_input_tokens + total_output_tokens)"
    elif field == "input":
        expr = "total_input_tokens"
    elif field == "output":
        expr = "total_output_tokens"
    elif field == "turns":
        expr = "turn_count"
    elif field == "cache_read":
        expr = "total_cache_read"
    elif field == "cache_creation":
        expr = "total_cache_creation"
    elif field == "model":
        expr = "COALESCE(model, '')"
    elif field == "project":
        expr = "COALESCE(project_name, '')"
    elif field == "branch":
        expr = "COALESCE(git_branch, '')"
    elif field == "session":
        expr = "COALESCE(session_id, '')"
    elif field == "date":
        expr = "substr(COALESCE(last_timestamp, ''), 1, 10)"
    elif field == "user":
        expr = "COALESCE(user_id, 'default')"
    else:
        return None

    # Numeric fields only support non-substring operators.
    if field in ("tokens", "input", "output", "turns", "cache_read", "cache_creation"):
        if op == "~":
            return None
        try:
            value = _parse_number(val)
        except ValueError:
            return None
        return (f"{expr} {sql_op} ?", [value])

    # String-style fields
    value = val
    if op == "~":
        value = f"%{val}%"
    return (f"LOWER({expr}) {sql_op} LOWER(?)", [value])


def _build_sql_prefilter(tokens: list[dict]) -> tuple[str, list] | None:
    """
    Build a SQL WHERE clause from tokens when possible.

    For safety and simplicity we only push down:
    - A single condition query, or
    - Multi-condition queries where all connectors are AND.
    """
    if not tokens:
        return None

    connectors = [t["value"] for t in tokens if t["type"] == "connector"]
    if connectors and any(c != "AND" for c in connectors):
        return None

    where_parts = []
    params = []
    for tok in tokens:
        if tok["type"] != "condition":
            continue
        compiled = _compile_condition_sql(tok)
        if compiled is None:
            return None
        clause, clause_params = compiled
        where_parts.append(f"({clause})")
        params.extend(clause_params)

    if not where_parts:
        return None
    return (" AND ".join(where_parts), params)


def execute_query(query_str: str, db_path: Path = DB_PATH,
                  limit: int = 100) -> list[dict]:
    """
    Execute a query DSL string against the sessions table.

    Returns list of matching session dicts with computed fields.
    """
    if not db_path.exists():
        return []

    tokens = _tokenize(query_str)
    if not tokens:
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    sql_prefilter = _build_sql_prefilter(tokens)
    base_select = """
        SELECT session_id, project_name, first_timestamp, last_timestamp,
               git_branch, total_input_tokens, total_output_tokens,
               total_cache_read, total_cache_creation, model, turn_count,
               user_id
        FROM sessions
    """
    if sql_prefilter:
        where_clause, where_params = sql_prefilter
        rows = conn.execute(
            f"{base_select} WHERE {where_clause} ORDER BY last_timestamp DESC",
            where_params,
        ).fetchall()
    else:
        rows = conn.execute(
            f"{base_select} ORDER BY last_timestamp DESC"
        ).fetchall()
    conn.close()

    results = []
    for row in rows:
        row_dict = dict(row)
        match = _evaluate_with_connectors(row_dict, tokens)

        if match:
            # Add computed fields
            row_dict["total_tokens"] = (
                (row_dict.get("total_input_tokens", 0) or 0) +
                (row_dict.get("total_output_tokens", 0) or 0)
            )
            row_dict["est_cost"] = calc_cost(
                row_dict.get("model", "default"),
                row_dict.get("total_input_tokens", 0) or 0,
                row_dict.get("total_output_tokens", 0) or 0,
                row_dict.get("total_cache_read", 0) or 0,
                row_dict.get("total_cache_creation", 0) or 0,
            )
            results.append(row_dict)

        if len(results) >= limit:
            break

    return results


def _evaluate_with_connectors(row_dict: dict, tokens: list[dict]) -> bool:
    """Evaluate a full token list with AND/OR connectors."""
    conditions = []
    connectors = []

    for tok in tokens:
        if tok["type"] == "condition":
            conditions.append(_evaluate_condition(row_dict, tok))
        elif tok["type"] == "connector":
            connectors.append(tok["value"])

    if not conditions:
        return False

    result = conditions[0]
    for i, conn in enumerate(connectors):
        if i + 1 < len(conditions):
            if conn == "OR":
                result = result or conditions[i + 1]
            else:  # AND
                result = result and conditions[i + 1]

    return result


def format_results(results: list[dict], fmt: str = "table") -> str:
    """Format query results for display."""
    if not results:
        return "  No matching sessions found."

    if fmt == "json":
        import json
        return json.dumps(results, indent=2, default=str)

    lines = []
    lines.append(f"  Found {len(results)} matching session(s):\n")
    lines.append(f"  {'SESSION':<10} {'PROJECT':<30} {'MODEL':<25} "
                 f"{'TOKENS':>12} {'COST':>10} {'TURNS':>6} {'BRANCH':<20}")
    lines.append(f"  {'─'*10} {'─'*30} {'─'*25} {'─'*12} {'─'*10} {'─'*6} {'─'*20}")

    for r in results:
        sid = (r.get("session_id", "") or "")[:8]
        proj = (r.get("project_name", "") or "unknown")[:28]
        model = (r.get("model", "") or "unknown")[:23]
        tokens = r.get("total_tokens", 0)
        cost = r.get("est_cost", 0)
        turns = r.get("turn_count", 0) or 0
        branch = (r.get("git_branch", "") or "")[:18]

        tok_str = f"{tokens:,}" if tokens < 1_000_000 else f"{tokens/1_000_000:.2f}M"
        lines.append(f"  {sid:<10} {proj:<30} {model:<25} {tok_str:>12} ${cost:>9.4f} {turns:>6} {branch:<20}")

    total_tokens = sum(r.get("total_tokens", 0) for r in results)
    total_cost = sum(r.get("est_cost", 0) for r in results)
    lines.append(f"\n  Total: {total_tokens:,} tokens  ·  ${total_cost:.4f} cost  ·  {len(results)} sessions")

    return "\n".join(lines)
