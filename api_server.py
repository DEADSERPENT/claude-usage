"""
api_server.py - Local REST API server for claude-usage.

Exposes a comprehensive API for programmatic access to usage data.
Runs independently on a configurable port (default 8081).

Endpoints:
  GET  /api/v1/usage/today           Today's usage summary
  GET  /api/v1/usage/stats           All-time statistics
  GET  /api/v1/usage/daily           Daily aggregates (with ?days=N)
  GET  /api/v1/sessions              Session list (with filters)
  GET  /api/v1/sessions/:id          Session detail with turns
  GET  /api/v1/sessions/:id/turns    Turn-level data for a session
  GET  /api/v1/cost                  Cost breakdown
  GET  /api/v1/cost/forecast         Cost forecast / projection
  GET  /api/v1/models                Model usage summary
  GET  /api/v1/projects              Project usage summary
  GET  /api/v1/branches              Git branch usage comparison
  GET  /api/v1/tools                 Tool usage summary
  GET  /api/v1/anomalies             Recent anomalies
  GET  /api/v1/optimize              Cost optimization suggestions
  GET  /api/v1/users                 User list
  GET  /api/v1/health                System health
  GET  /api/v1/search                FTS5 / LIKE session search (?q=)
  GET  /api/v1/tags                  List tags; POST to add tag to session
  GET  /api/v1/tags/:tag             Sessions for a tag
  GET  /api/v1/timetravel            Time-travel query (?date=)
  GET  /api/v1/simulate              What-if simulator info (POST runs simulation)
  GET  /api/v1/retention             Retention / rollup status
  GET  /api/v1/cache-thrashing       Cache thrashing analysis
  GET  /api/v1/circuit-breaker       Circuit breaker status
  GET  /api/v1/plugins             Plugin list
  GET  /api/v1/layout                Dashboard layout config
  GET  /api/v1/heatmap               365-day heatmap data
  GET  /api/v1/events                SSE stream for dashboard updates
  POST /api/v1/scan                  Trigger a scan
  POST /api/v1/query                 Execute query DSL
  POST /api/v1/retention/rollup      Trigger manual rollup
  POST /api/v1/circuit-breaker/reset Reset circuit breaker
"""

import json
import sqlite3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, date, timedelta
from pathlib import Path

from config import (DB_PATH, API_PORT, SCAN_INTERVAL_SECS,
                    DAILY_LIMIT_USD, ACTIVE_USER, PRICING)
from config import calc_cost


def _get_conn():
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


_ALLOWED_ORIGINS = {
    "http://localhost", "http://127.0.0.1",
    f"http://localhost:{API_PORT}", f"http://127.0.0.1:{API_PORT}",
}


def _safe_origin(handler) -> str:
    """Return the request Origin only if it's a trusted localhost origin."""
    origin = handler.headers.get("Origin", "")
    if not origin:
        return ""
    normalized = origin.rstrip("/")
    if normalized in _ALLOWED_ORIGINS:
        return normalized
    for prefix in ("http://localhost:", "http://127.0.0.1:"):
        if normalized.startswith(prefix):
            return normalized
    return ""


def _check_auth(handler) -> bool:
    """Validate bearer token if AUTH_SECRET_FILE contains a secret."""
    from config import AUTH_SECRET_FILE
    if not AUTH_SECRET_FILE.exists():
        return True
    try:
        secret = AUTH_SECRET_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return True
    if not secret:
        return True
    auth = handler.headers.get("Authorization", "")
    return auth == f"Bearer {secret}"


class APIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        origin = _safe_origin(self)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, msg, status=400):
        self._send_json({"error": msg}, status)

    def _require_auth(self) -> bool:
        if not _check_auth(self):
            self._send_error("Unauthorized", 401)
            return False
        return True

    def _params(self):
        return parse_qs(urlparse(self.path).query)

    def _param(self, key, default=None):
        vals = self._params().get(key, [])
        return vals[0] if vals else default

    def do_OPTIONS(self):
        self.send_response(200)
        origin = _safe_origin(self)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")

        if path != "/api/v1/health" and not self._require_auth():
            return

        routes = {
            "/api/v1/health":       self._health,
            "/api/v1/usage/today":  self._usage_today,
            "/api/v1/usage/stats":  self._usage_stats,
            "/api/v1/usage/daily":  self._usage_daily,
            "/api/v1/sessions":     self._sessions,
            "/api/v1/cost":         self._cost,
            "/api/v1/cost/forecast": self._cost_forecast,
            "/api/v1/models":       self._models,
            "/api/v1/projects":     self._projects,
            "/api/v1/branches":     self._branches,
            "/api/v1/tools":        self._tools,
            "/api/v1/anomalies":    self._anomalies,
            "/api/v1/optimize":     self._optimize,
            "/api/v1/users":        self._users,
            "/api/v1/search":         self._search,
            "/api/v1/tags":           self._tags_list,
            "/api/v1/timetravel":     self._timetravel,
            "/api/v1/simulate":       self._simulate_get,
            "/api/v1/cache-thrashing": self._cache_thrashing,
            "/api/v1/circuit-breaker": self._circuit_breaker_status,
            "/api/v1/plugins":        self._plugins_list,
            "/api/v1/layout":         self._layout_get,
            "/api/v1/heatmap":        self._heatmap_data,
            "/api/v1/retention":      self._retention_status,
            "/api/v1/events":         self._sse_events,
        }

        # Check for parameterized routes
        if path.startswith("/api/v1/tags/"):
            tag_name = path.split("/api/v1/tags/")[1]
            return self._tags_by_name(tag_name)
        if path.startswith("/api/v1/sessions/") and "/turns" in path:
            sid = path.split("/api/v1/sessions/")[1].split("/turns")[0]
            return self._session_turns(sid)
        elif path.startswith("/api/v1/sessions/"):
            sid = path.split("/api/v1/sessions/")[1]
            return self._session_detail(sid)

        handler = routes.get(path)
        if handler:
            try:
                handler()
            except Exception as e:
                self._send_error(str(e), 500)
        else:
            self._send_error("Not found", 404)

    def do_POST(self):
        if not self._require_auth():
            return

        path = urlparse(self.path).path.rstrip("/")
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length else ""

        if path == "/api/v1/scan":
            self._trigger_scan()
        elif path == "/api/v1/query":
            self._execute_query(body)
        elif path == "/api/v1/tags":
            self._add_tag(body)
        elif path == "/api/v1/retention/rollup":
            self._trigger_rollup(body)
        elif path == "/api/v1/simulate":
            self._simulate_post(body)
        elif path == "/api/v1/circuit-breaker/reset":
            self._circuit_breaker_reset(body)
        elif path == "/api/v1/layout":
            self._save_layout(body)
        else:
            self._send_error("Not found", 404)

    # ── Route handlers ────────────────────────────────────────────────────────

    def _health(self):
        db_exists = DB_PATH.exists()
        db_size = DB_PATH.stat().st_size if db_exists else 0
        self._send_json({
            "status": "healthy" if db_exists else "no_database",
            "db_path": str(DB_PATH),
            "db_size_bytes": db_size,
            "active_user": ACTIVE_USER,
            "api_version": "v1",
            "timestamp": datetime.now().isoformat(),
        })

    def _usage_today(self):
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)

        today_str = date.today().isoformat()
        rows = conn.execute("""
            SELECT COALESCE(model, 'unknown') as model,
                   SUM(input_tokens) as inp, SUM(output_tokens) as out,
                   SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
                   COUNT(*) as turns, COUNT(DISTINCT session_id) as sessions
            FROM turns WHERE substr(timestamp, 1, 10) = ?
            GROUP BY model ORDER BY inp + out DESC
        """, (today_str,)).fetchall()
        conn.close()

        models = []
        totals = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0,
                  "turns": 0, "sessions": 0, "cost": 0}
        for r in rows:
            inp, out = r["inp"] or 0, r["out"] or 0
            cr, cc = r["cr"] or 0, r["cc"] or 0
            cost = calc_cost(r["model"], inp, out, cr, cc)
            models.append({
                "model": r["model"], "input": inp, "output": out,
                "cache_read": cr, "cache_creation": cc,
                "turns": r["turns"], "sessions": r["sessions"],
                "cost": round(cost, 6),
            })
            totals["input"] += inp; totals["output"] += out
            totals["cache_read"] += cr; totals["cache_creation"] += cc
            totals["turns"] += r["turns"]; totals["sessions"] += r["sessions"]
            totals["cost"] += cost

        totals["cost"] = round(totals["cost"], 6)
        self._send_json({"date": today_str, "models": models, "totals": totals})

    def _usage_stats(self):
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)

        row = conn.execute("""
            SELECT SUM(total_input_tokens) as inp, SUM(total_output_tokens) as out,
                   SUM(total_cache_read) as cr, SUM(total_cache_creation) as cc,
                   SUM(turn_count) as turns, COUNT(*) as sessions,
                   MIN(first_timestamp) as first, MAX(last_timestamp) as last
            FROM sessions
        """).fetchone()
        conn.close()

        total_cost = calc_cost("default", row["inp"] or 0, row["out"] or 0,
                               row["cr"] or 0, row["cc"] or 0)
        self._send_json({
            "input_tokens": row["inp"] or 0, "output_tokens": row["out"] or 0,
            "cache_read": row["cr"] or 0, "cache_creation": row["cc"] or 0,
            "total_turns": row["turns"] or 0, "total_sessions": row["sessions"] or 0,
            "first_activity": row["first"], "last_activity": row["last"],
            "est_total_cost": round(total_cost, 6),
        })

    def _usage_daily(self):
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)

        days = int(self._param("days", "30"))
        cutoff = (date.today() - timedelta(days=days)).isoformat()

        rows = conn.execute("""
            SELECT substr(timestamp, 1, 10) as day,
                   SUM(input_tokens) as inp, SUM(output_tokens) as out,
                   SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
                   COUNT(*) as turns, COUNT(DISTINCT session_id) as sessions
            FROM turns WHERE substr(timestamp, 1, 10) >= ?
            GROUP BY day ORDER BY day
        """, (cutoff,)).fetchall()
        conn.close()

        daily = []
        for r in rows:
            cost = calc_cost("default", r["inp"] or 0, r["out"] or 0,
                             r["cr"] or 0, r["cc"] or 0)
            daily.append({
                "day": r["day"], "input": r["inp"] or 0, "output": r["out"] or 0,
                "cache_read": r["cr"] or 0, "cache_creation": r["cc"] or 0,
                "turns": r["turns"], "sessions": r["sessions"],
                "cost": round(cost, 6),
            })
        self._send_json({"days": days, "data": daily})

    def _sessions(self):
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)

        limit = int(self._param("limit", "50"))
        offset = int(self._param("offset", "0"))
        model = self._param("model")
        project = self._param("project")
        branch = self._param("branch")
        user = self._param("user")

        query = """
            SELECT session_id, project_name, first_timestamp, last_timestamp,
                   git_branch, total_input_tokens, total_output_tokens,
                   total_cache_read, total_cache_creation, model, turn_count, user_id
            FROM sessions WHERE 1=1
        """
        params = []
        if model:
            query += " AND model LIKE ?"
            params.append(f"%{model}%")
        if project:
            query += " AND project_name LIKE ?"
            params.append(f"%{project}%")
        if branch:
            query += " AND git_branch LIKE ?"
            params.append(f"%{branch}%")
        if user:
            query += " AND user_id = ?"
            params.append(user)
        query += " ORDER BY last_timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()
        total = conn.execute("SELECT COUNT(*) as cnt FROM sessions").fetchone()["cnt"]
        conn.close()

        sessions = []
        for r in rows:
            cost = calc_cost(r["model"], r["total_input_tokens"] or 0,
                             r["total_output_tokens"] or 0,
                             r["total_cache_read"] or 0,
                             r["total_cache_creation"] or 0)
            sessions.append({
                "session_id": r["session_id"],
                "project": r["project_name"],
                "branch": r["git_branch"],
                "first_timestamp": r["first_timestamp"],
                "last_timestamp": r["last_timestamp"],
                "model": r["model"],
                "turns": r["turn_count"],
                "input": r["total_input_tokens"],
                "output": r["total_output_tokens"],
                "cache_read": r["total_cache_read"],
                "cache_creation": r["total_cache_creation"],
                "cost": round(cost, 6),
                "user_id": r["user_id"],
            })
        self._send_json({"total": total, "limit": limit, "offset": offset,
                         "sessions": sessions})

    def _session_detail(self, session_id):
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)

        # Support prefix matching for short IDs
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ? OR session_id LIKE ?",
            (session_id, f"{session_id}%")
        ).fetchone()

        if not row:
            conn.close()
            return self._send_error("Session not found", 404)

        turns = conn.execute("""
            SELECT timestamp, model, input_tokens, output_tokens,
                   cache_read_tokens, cache_creation_tokens, tool_name, cwd
            FROM turns WHERE session_id = ? ORDER BY timestamp
        """, (row["session_id"],)).fetchall()
        conn.close()

        self._send_json({
            "session": dict(row),
            "turns": [dict(t) for t in turns],
        })

    def _session_turns(self, session_id):
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)

        turns = conn.execute("""
            SELECT timestamp, model, input_tokens, output_tokens,
                   cache_read_tokens, cache_creation_tokens, tool_name, cwd
            FROM turns WHERE session_id = ? OR session_id LIKE ?
            ORDER BY timestamp
        """, (session_id, f"{session_id}%")).fetchall()
        conn.close()

        self._send_json({"session_id": session_id, "turns": [dict(t) for t in turns]})

    def _cost(self):
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)

        days = int(self._param("days", "30"))
        cutoff = (date.today() - timedelta(days=days)).isoformat()

        rows = conn.execute("""
            SELECT COALESCE(model, 'unknown') as model,
                   SUM(input_tokens) as inp, SUM(output_tokens) as out,
                   SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
                   COUNT(*) as turns
            FROM turns WHERE substr(timestamp, 1, 10) >= ?
            GROUP BY model ORDER BY inp + out DESC
        """, (cutoff,)).fetchall()
        conn.close()

        total_cost = 0
        by_model = []
        for r in rows:
            cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0,
                             r["cr"] or 0, r["cc"] or 0)
            total_cost += cost
            by_model.append({
                "model": r["model"], "cost": round(cost, 6), "turns": r["turns"],
                "input": r["inp"] or 0, "output": r["out"] or 0,
            })

        self._send_json({
            "period_days": days, "total_cost": round(total_cost, 6),
            "daily_limit": DAILY_LIMIT_USD, "by_model": by_model,
        })

    def _cost_forecast(self):
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)

        today_str = date.today().isoformat()
        now = datetime.now()

        today_row = conn.execute("""
            SELECT SUM(input_tokens) as inp, SUM(output_tokens) as out,
                   SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc
            FROM turns WHERE substr(timestamp, 1, 10) = ?
        """, (today_str,)).fetchone()

        # 2h burn rate
        cut2h = (datetime.utcnow() - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%S')
        burn_row = conn.execute(
            "SELECT SUM(input_tokens + output_tokens) as t FROM turns WHERE timestamp >= ?",
            (cut2h,)
        ).fetchone()
        conn.close()

        inp = today_row["inp"] or 0
        out = today_row["out"] or 0
        cr = today_row["cr"] or 0
        cc = today_row["cc"] or 0
        today_cost = calc_cost("default", inp, out, cr, cc)
        today_tokens = inp + out

        burn_tokens = (burn_row["t"] or 0) / 120  # per minute
        cpt = today_cost / max(today_tokens, 1)
        hours_left = max(0, 24 - now.hour - now.minute / 60)
        proj_tokens = today_tokens + burn_tokens * 60 * hours_left
        proj_cost = today_cost + burn_tokens * 60 * hours_left * cpt

        self._send_json({
            "today_cost": round(today_cost, 6),
            "today_tokens": today_tokens,
            "burn_rate_per_min": round(burn_tokens, 1),
            "projected_eod_tokens": int(proj_tokens),
            "projected_eod_cost": round(proj_cost, 6),
            "hours_remaining": round(hours_left, 1),
            "daily_limit": DAILY_LIMIT_USD,
        })

    def _models(self):
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)

        rows = conn.execute("""
            SELECT COALESCE(model, 'unknown') as model,
                   SUM(input_tokens) as inp, SUM(output_tokens) as out,
                   SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
                   COUNT(*) as turns, COUNT(DISTINCT session_id) as sessions
            FROM turns GROUP BY model ORDER BY inp + out DESC
        """).fetchall()
        conn.close()

        models = []
        for r in rows:
            cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0,
                             r["cr"] or 0, r["cc"] or 0)
            models.append({
                "model": r["model"], "input": r["inp"] or 0, "output": r["out"] or 0,
                "cache_read": r["cr"] or 0, "cache_creation": r["cc"] or 0,
                "turns": r["turns"], "sessions": r["sessions"],
                "cost": round(cost, 6),
            })
        self._send_json({"models": models})

    def _projects(self):
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)

        rows = conn.execute("""
            SELECT project_name,
                   SUM(total_input_tokens) as inp, SUM(total_output_tokens) as out,
                   SUM(total_cache_read) as cr, SUM(total_cache_creation) as cc,
                   SUM(turn_count) as turns, COUNT(*) as sessions
            FROM sessions GROUP BY project_name
            ORDER BY inp + out DESC
        """).fetchall()
        conn.close()

        projects = []
        for r in rows:
            cost = calc_cost("default", r["inp"] or 0, r["out"] or 0,
                             r["cr"] or 0, r["cc"] or 0)
            projects.append({
                "project": r["project_name"], "input": r["inp"] or 0,
                "output": r["out"] or 0, "turns": r["turns"],
                "sessions": r["sessions"], "cost": round(cost, 6),
            })
        self._send_json({"projects": projects})

    def _branches(self):
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)

        days = int(self._param("days", "30"))
        cutoff = (date.today() - timedelta(days=days)).isoformat()

        rows = conn.execute("""
            SELECT COALESCE(s.git_branch, '(none)') as branch,
                   SUM(s.total_input_tokens) as inp,
                   SUM(s.total_output_tokens) as out,
                   SUM(s.total_cache_read) as cr,
                   SUM(s.total_cache_creation) as cc,
                   SUM(s.turn_count) as turns,
                   COUNT(*) as sessions
            FROM sessions s
            WHERE s.last_timestamp >= ?
            GROUP BY branch
            ORDER BY inp + out DESC
        """, (cutoff,)).fetchall()
        conn.close()

        branches = []
        for r in rows:
            cost = calc_cost("default", r["inp"] or 0, r["out"] or 0,
                             r["cr"] or 0, r["cc"] or 0)
            branches.append({
                "branch": r["branch"], "input": r["inp"] or 0,
                "output": r["out"] or 0, "cache_read": r["cr"] or 0,
                "cache_creation": r["cc"] or 0,
                "turns": r["turns"], "sessions": r["sessions"],
                "cost": round(cost, 6),
            })
        self._send_json({"period_days": days, "branches": branches})

    def _tools(self):
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)

        rows = conn.execute("""
            SELECT tool_name, COUNT(*) as count,
                   SUM(input_tokens + output_tokens) as tokens,
                   AVG(input_tokens + output_tokens) as avg_tokens
            FROM turns WHERE tool_name IS NOT NULL
            GROUP BY tool_name ORDER BY count DESC LIMIT 30
        """).fetchall()
        conn.close()

        self._send_json({"tools": [
            {"tool": r["tool_name"], "count": r["count"],
             "total_tokens": r["tokens"] or 0,
             "avg_tokens": int(r["avg_tokens"] or 0)}
            for r in rows
        ]})

    def _anomalies(self):
        from anomaly import get_recent_anomalies
        days = int(self._param("days", "7"))
        anomalies = get_recent_anomalies(DB_PATH, days=days)
        self._send_json({"anomalies": anomalies})

    def _optimize(self):
        from optimizer import analyze
        days = int(self._param("days", "30"))
        result = analyze(DB_PATH, days=days)
        self._send_json(result)

    def _users(self):
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)

        rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()

        # Get per-user stats
        user_stats = conn.execute("""
            SELECT user_id,
                   SUM(input_tokens + output_tokens) as tokens,
                   COUNT(*) as turns,
                   COUNT(DISTINCT session_id) as sessions
            FROM turns GROUP BY user_id
        """).fetchall()
        conn.close()

        stats_map = {r["user_id"]: dict(r) for r in user_stats}
        users = []
        for r in rows:
            s = stats_map.get(r["user_id"], {})
            users.append({
                "user_id": r["user_id"],
                "display_name": r["display_name"],
                "role": r["role"],
                "created_at": r["created_at"],
                "last_active": r["last_active"],
                "tokens": s.get("tokens", 0),
                "turns": s.get("turns", 0),
                "sessions": s.get("sessions", 0),
            })
        self._send_json({"active_user": ACTIVE_USER, "users": users})

    def _search(self):
        q = self._param("q", "")
        if not q:
            return self._send_error("Missing 'q' parameter")
        try:
            from scanner import search_sessions_fts
            results = search_sessions_fts(q, DB_PATH, limit=int(self._param("limit", "50")))
            self._send_json({"query": q, "count": len(results), "results": results})
        except Exception:
            # Fallback to LIKE search
            conn = _get_conn()
            if not conn:
                return self._send_error("Database not found", 404)
            rows = conn.execute("""
                SELECT session_id, project_name, git_branch, model, turn_count,
                       total_input_tokens, total_output_tokens, last_timestamp
                FROM sessions
                WHERE project_name LIKE ? OR git_branch LIKE ? OR model LIKE ? OR session_id LIKE ?
                ORDER BY last_timestamp DESC LIMIT ?
            """, (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%", int(self._param("limit", "50")))).fetchall()
            conn.close()
            self._send_json({"query": q, "count": len(rows), "results": [dict(r) for r in rows]})

    def _tags_list(self):
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)
        try:
            rows = conn.execute("""
                SELECT tag_name, COUNT(*) as session_count,
                       GROUP_CONCAT(DISTINCT session_id) as session_ids
                FROM tags GROUP BY tag_name ORDER BY session_count DESC
            """).fetchall()
            conn.close()
            self._send_json({"tags": [dict(r) for r in rows]})
        except Exception:
            conn.close()
            self._send_json({"tags": []})

    def _tags_by_name(self, tag_name):
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)
        rows = conn.execute("""
            SELECT s.session_id, s.project_name, s.model, s.turn_count,
                   s.total_input_tokens, s.total_output_tokens, s.last_timestamp
            FROM tags t JOIN sessions s ON t.session_id = s.session_id
            WHERE t.tag_name = ?
            ORDER BY s.last_timestamp DESC
        """, (tag_name,)).fetchall()
        conn.close()
        sessions = []
        for r in rows:
            cost = calc_cost(r["model"], r["total_input_tokens"] or 0,
                           r["total_output_tokens"] or 0, 0, 0)
            d = dict(r)
            d["cost"] = round(cost, 6)
            sessions.append(d)
        self._send_json({"tag": tag_name, "sessions": sessions})

    def _timetravel(self):
        dt = self._param("date")
        if not dt:
            return self._send_error("Missing 'date' parameter")
        from archiver import time_travel_query
        result = time_travel_query(dt, DB_PATH)
        self._send_json(result)

    def _simulate_get(self):
        self._send_json({
            "description": "POST custom pricing JSON to simulate costs",
            "current_pricing": dict(PRICING),
            "example_body": {
                "pricing": {"claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30}},
                "days": 30
            }
        })

    def _cache_thrashing(self):
        from optimizer import analyze_cache_thrashing
        days = int(self._param("days", "30"))
        results = analyze_cache_thrashing(DB_PATH, days)
        self._send_json({"period_days": days, "thrashing_sessions": results})

    def _circuit_breaker_status(self):
        from circuit_breaker import get_status
        self._send_json(get_status(DB_PATH))

    def _plugins_list(self):
        from plugins import load_plugins, list_loaded
        load_plugins()
        self._send_json({"plugins": list_loaded()})

    def _layout_get(self):
        from config import LAYOUT_CONFIG
        import json as _json
        layout = {"cards": ["stats", "daily_chart", "model_chart", "project_chart", "tools_chart", "hourly_chart", "sessions_table", "forecast", "anomalies", "branches", "model_cost", "heatmap", "query_playground"]}
        if LAYOUT_CONFIG.exists():
            try:
                layout = _json.loads(LAYOUT_CONFIG.read_text(encoding="utf-8"))
            except Exception:
                pass
        self._send_json(layout)

    def _heatmap_data(self):
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)
        rows = conn.execute("""
            SELECT substr(timestamp, 1, 10) as day,
                   SUM(input_tokens + output_tokens) as tokens,
                   COUNT(*) as turns
            FROM turns
            WHERE timestamp >= date('now', '-365 days')
            GROUP BY day ORDER BY day
        """).fetchall()
        conn.close()
        self._send_json({"days": [{"day": r["day"], "tokens": r["tokens"] or 0, "turns": r["turns"] or 0} for r in rows]})

    def _retention_status(self):
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)
        total_turns = conn.execute("SELECT COUNT(*) as c FROM turns").fetchone()["c"]
        oldest = conn.execute("SELECT MIN(timestamp) as t FROM turns").fetchone()["t"]
        rollup_count = 0
        try:
            rollup_count = conn.execute("SELECT COUNT(*) as c FROM daily_rollups").fetchone()["c"]
        except Exception:
            pass
        conn.close()
        from config import RETENTION_DAYS
        self._send_json({
            "total_turns": total_turns,
            "oldest_turn": oldest,
            "retention_days": RETENTION_DAYS,
            "rollup_entries": rollup_count,
        })

    def _trigger_scan(self):
        from scanner import scan
        result = scan(verbose=False)
        self._send_json({"status": "ok", "result": result})

    def _execute_query(self, body):
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return self._send_error("Invalid JSON body")

        query_str = payload.get("query", "")
        if not query_str:
            return self._send_error("Missing 'query' field")

        from query_engine import execute_query
        results = execute_query(query_str, DB_PATH,
                                limit=int(payload.get("limit", 100)))

        self._send_json({"query": query_str, "count": len(results),
                         "results": results})

    def _add_tag(self, body):
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return self._send_error("Invalid JSON")
        session_id = payload.get("session_id", "")
        tag = payload.get("tag", "")
        if not session_id or not tag:
            return self._send_error("Missing session_id or tag")
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)
        # Support prefix matching
        full_sid = conn.execute(
            "SELECT session_id FROM sessions WHERE session_id LIKE ?",
            (f"{session_id}%",)
        ).fetchone()
        if not full_sid:
            conn.close()
            return self._send_error("Session not found", 404)
        try:
            conn.execute("INSERT OR IGNORE INTO tags (session_id, tag_name) VALUES (?, ?)",
                        (full_sid["session_id"], tag))
            conn.commit()
        except Exception as e:
            conn.close()
            return self._send_error(str(e))
        conn.close()
        self._send_json({"status": "ok", "session_id": full_sid["session_id"], "tag": tag})

    def _trigger_rollup(self, body):
        from scanner import rollup_old_data
        from config import RETENTION_DAYS
        result = rollup_old_data(DB_PATH, RETENTION_DAYS)
        self._send_json({"status": "ok", "result": result})

    def _simulate_post(self, body):
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return self._send_error("Invalid JSON")
        custom_pricing = payload.get("pricing", {})
        days = int(payload.get("days", 30))
        if not custom_pricing:
            return self._send_error("Missing 'pricing' in body")

        from config import calc_cost_with_pricing
        conn = _get_conn()
        if not conn:
            return self._send_error("Database not found", 404)
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        rows = conn.execute("""
            SELECT COALESCE(model, 'unknown') as model,
                   SUM(input_tokens) as inp, SUM(output_tokens) as out,
                   SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc
            FROM turns WHERE substr(timestamp, 1, 10) >= ?
            GROUP BY model
        """, (cutoff,)).fetchall()
        conn.close()

        actual_total = 0
        simulated_total = 0
        by_model = []
        for r in rows:
            actual = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
            simulated = calc_cost_with_pricing(custom_pricing, r["model"], r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0)
            actual_total += actual
            simulated_total += simulated
            by_model.append({
                "model": r["model"],
                "actual_cost": round(actual, 6),
                "simulated_cost": round(simulated, 6),
                "delta": round(simulated - actual, 6),
            })

        self._send_json({
            "period_days": days,
            "actual_total": round(actual_total, 4),
            "simulated_total": round(simulated_total, 4),
            "savings": round(actual_total - simulated_total, 4),
            "by_model": by_model,
            "custom_pricing": custom_pricing,
        })

    def _circuit_breaker_reset(self, body):
        from circuit_breaker import unblock_claude_binary
        result = unblock_claude_binary()
        self._send_json({"status": "ok", "unblocked": result})

    def _save_layout(self, body):
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return self._send_error("Invalid JSON")
        from config import LAYOUT_CONFIG
        LAYOUT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        LAYOUT_CONFIG.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self._send_json({"status": "ok"})

    def _sse_events(self):
        """Server-Sent Events endpoint for real-time dashboard updates."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        origin = _safe_origin(self)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()

        import time as _time
        last_data = None
        try:
            while True:
                try:
                    from dashboard import get_dashboard_data
                    data = get_dashboard_data()
                    data_str = json.dumps(data, default=str)

                    if data_str != last_data:
                        self.wfile.write(f"event: update\ndata: {data_str}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        last_data = data_str
                    else:
                        self.wfile.write(f": heartbeat\n\n".encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                except Exception:
                    pass
                _time.sleep(5)
        except (BrokenPipeError, ConnectionResetError):
            pass


def serve(port=None):
    if port is None:
        port = API_PORT
    server = ThreadingHTTPServer(("localhost", port), APIHandler)
    print(f"API server running at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAPI server stopped.")


if __name__ == "__main__":
    serve()
