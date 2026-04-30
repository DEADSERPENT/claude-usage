"""
scanner.py - Scans Claude Code JSONL transcript files and stores data in SQLite.

Includes schema migration support for adding new features (user_id, anomalies,
etc.) to existing databases without data loss.
"""

import json
import os
import glob
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone

from config import DB_PATH, PROJECTS_DIR, ACTIVE_USER


def get_db(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _column_exists(conn, table: str, column: str) -> bool:
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(c["name"] == column for c in cols)


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM sqlite_master WHERE type='table' AND name=?",
        (table,)
    ).fetchone()
    return row["cnt"] > 0


def _get_schema_version(conn) -> int:
    """Get current schema version from migrations tracking table."""
    try:
        row = conn.execute("SELECT MAX(version) as v FROM applied_migrations").fetchone()
        return row["v"] or 0 if row else 0
    except Exception:
        return 0


def _apply_migrations(conn):
    """Apply pending schema migrations."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS applied_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT,
            applied_at TEXT DEFAULT (datetime('now'))
        )
    """)

    current = _get_schema_version(conn)

    migrations = [
        (1, "add_tags_table", """
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                tag_name TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(session_id, tag_name)
            );
            CREATE INDEX IF NOT EXISTS idx_tags_session ON tags(session_id);
            CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(tag_name);
        """),
        (2, "add_rollup_table", """
            CREATE TABLE IF NOT EXISTS daily_rollups (
                day TEXT NOT NULL,
                model TEXT,
                project_name TEXT,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                total_cache_read INTEGER DEFAULT 0,
                total_cache_creation INTEGER DEFAULT 0,
                turn_count INTEGER DEFAULT 0,
                session_count INTEGER DEFAULT 0,
                est_cost_usd REAL DEFAULT 0,
                rolled_up_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (day, model, project_name)
            );
        """),
        (3, "add_fts5_sessions", """
            CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
                session_id, project_name, git_branch, model,
                content='sessions',
                content_rowid='rowid'
            );
        """),
        (4, "add_webhook_log", """
            CREATE TABLE IF NOT EXISTS webhook_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fired_at TEXT DEFAULT (datetime('now')),
                metric TEXT,
                level TEXT,
                url TEXT,
                status TEXT,
                response_code INTEGER
            );
        """),
        (5, "add_auth_tokens", """
            CREATE TABLE IF NOT EXISTS auth_tokens (
                token TEXT PRIMARY KEY,
                user_id TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                expires_at TEXT,
                role TEXT DEFAULT 'viewer'
            );
        """),
    ]

    for version, name, sql in migrations:
        if version > current:
            try:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO applied_migrations (version, name) VALUES (?, ?)",
                    (version, name)
                )
                conn.commit()
            except Exception as e:
                print(f"  Migration {version} ({name}) failed: {e}")


def init_db(conn):
    # WAL mode allows concurrent readers and writers without blocking
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id      TEXT PRIMARY KEY,
            project_name    TEXT,
            first_timestamp TEXT,
            last_timestamp  TEXT,
            git_branch      TEXT,
            total_input_tokens      INTEGER DEFAULT 0,
            total_output_tokens     INTEGER DEFAULT 0,
            total_cache_read        INTEGER DEFAULT 0,
            total_cache_creation    INTEGER DEFAULT 0,
            model           TEXT,
            turn_count      INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS turns (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id              TEXT,
            timestamp               TEXT,
            model                   TEXT,
            input_tokens            INTEGER DEFAULT 0,
            output_tokens           INTEGER DEFAULT 0,
            cache_read_tokens       INTEGER DEFAULT 0,
            cache_creation_tokens   INTEGER DEFAULT 0,
            tool_name               TEXT,
            cwd                     TEXT
        );

        CREATE TABLE IF NOT EXISTS processed_files (
            path    TEXT PRIMARY KEY,
            mtime   REAL,
            lines   INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
        CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp);
        CREATE INDEX IF NOT EXISTS idx_sessions_first ON sessions(first_timestamp);
    """)

    # ── Schema migrations ─────────────────────────────────────────────────────
    # Add user_id to sessions and turns for multi-user simulation
    if not _column_exists(conn, "sessions", "user_id"):
        conn.execute("ALTER TABLE sessions ADD COLUMN user_id TEXT DEFAULT 'default'")
    if not _column_exists(conn, "turns", "user_id"):
        conn.execute("ALTER TABLE turns ADD COLUMN user_id TEXT DEFAULT 'default'")

    # Users table for multi-user management
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     TEXT PRIMARY KEY,
            display_name TEXT,
            role        TEXT DEFAULT 'admin',
            created_at  TEXT DEFAULT (datetime('now')),
            last_active TEXT
        );

        CREATE TABLE IF NOT EXISTS anomalies (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT DEFAULT (datetime('now')),
            metric      TEXT,
            value       REAL,
            baseline    REAL,
            factor      REAL,
            severity    TEXT,
            message     TEXT,
            session_id  TEXT,
            user_id     TEXT DEFAULT 'default',
            acknowledged INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_anomalies_date ON anomalies(detected_at);
        CREATE INDEX IF NOT EXISTS idx_turns_user ON turns(user_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
    """)

    # Ensure default user exists
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, display_name, role) VALUES ('default', 'Default User', 'admin')"
    )

    _apply_migrations(conn)

    conn.commit()


def rebuild_fts(db_path=DB_PATH):
    """Rebuild the FTS5 index for full-text search."""
    conn = get_db(db_path)
    try:
        conn.execute("DELETE FROM sessions_fts")
        conn.execute("""
            INSERT INTO sessions_fts(rowid, session_id, project_name, git_branch, model)
            SELECT rowid, session_id, COALESCE(project_name, ''),
                   COALESCE(git_branch, ''), COALESCE(model, '')
            FROM sessions
        """)
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def search_sessions_fts(query: str, db_path=DB_PATH, limit: int = 50) -> list[dict]:
    """Full-text search across sessions."""
    conn = get_db(db_path)
    try:
        rows = conn.execute("""
            SELECT s.session_id, s.project_name, s.git_branch, s.model,
                   s.turn_count, s.total_input_tokens, s.total_output_tokens,
                   s.last_timestamp
            FROM sessions_fts
            JOIN sessions s ON s.rowid = sessions_fts.rowid
            WHERE sessions_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (query, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def rollup_old_data(db_path=DB_PATH, retention_days: int = 90):
    """Squash raw turn data older than retention_days into daily aggregates."""
    from config import calc_cost

    conn = get_db(db_path)
    cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")

    # Aggregate old turns into daily_rollups
    rows = conn.execute("""
        SELECT substr(timestamp, 1, 10) as day,
               COALESCE(model, 'unknown') as model,
               COALESCE(
                   (SELECT project_name FROM sessions WHERE session_id = t.session_id),
                   'unknown'
               ) as project_name,
               SUM(input_tokens) as inp,
               SUM(output_tokens) as out,
               SUM(cache_read_tokens) as cr,
               SUM(cache_creation_tokens) as cc,
               COUNT(*) as turn_count,
               COUNT(DISTINCT session_id) as session_count
        FROM turns t
        WHERE substr(timestamp, 1, 10) < ?
        GROUP BY day, model, project_name
    """, (cutoff,)).fetchall()

    for r in rows:
        cost = calc_cost(r["model"], r["inp"] or 0, r["out"] or 0,
                        r["cr"] or 0, r["cc"] or 0)
        conn.execute("""
            INSERT OR REPLACE INTO daily_rollups
                (day, model, project_name, total_input_tokens, total_output_tokens,
                 total_cache_read, total_cache_creation, turn_count, session_count, est_cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (r["day"], r["model"], r["project_name"],
              r["inp"] or 0, r["out"] or 0, r["cr"] or 0, r["cc"] or 0,
              r["turn_count"], r["session_count"], round(cost, 6)))

    # Delete old turns
    deleted = conn.execute(
        "DELETE FROM turns WHERE substr(timestamp, 1, 10) < ?", (cutoff,)
    ).rowcount

    if deleted > 0:
        conn.execute("VACUUM")

    conn.commit()
    conn.close()

    return {"rolled_up_days": len(rows), "deleted_turns": deleted, "cutoff": cutoff}


def project_name_from_cwd(cwd):
    """Derive a friendly project name from cwd path."""
    if not cwd:
        return "unknown"
    # Normalize to forward slashes, take last 2 components
    parts = cwd.replace("\\", "/").rstrip("/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[-1] if parts else "unknown"


def parse_jsonl_file(filepath):
    """Parse a JSONL file and yield (session_data, turns) tuples."""
    turns = []
    session_meta = {}  # session_id -> dict

    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rtype = record.get("type")
                if rtype not in ("assistant", "user"):
                    continue

                session_id = record.get("sessionId")
                if not session_id:
                    continue

                timestamp = record.get("timestamp", "")
                cwd = record.get("cwd", "")
                git_branch = record.get("gitBranch", "")

                # Update session metadata from any record
                if session_id not in session_meta:
                    session_meta[session_id] = {
                        "session_id": session_id,
                        "project_name": project_name_from_cwd(cwd),
                        "first_timestamp": timestamp,
                        "last_timestamp": timestamp,
                        "git_branch": git_branch,
                        "model": None,
                    }
                else:
                    meta = session_meta[session_id]
                    if timestamp and (not meta["first_timestamp"] or timestamp < meta["first_timestamp"]):
                        meta["first_timestamp"] = timestamp
                    if timestamp and (not meta["last_timestamp"] or timestamp > meta["last_timestamp"]):
                        meta["last_timestamp"] = timestamp
                    if git_branch and not meta["git_branch"]:
                        meta["git_branch"] = git_branch

                if rtype == "assistant":
                    msg = record.get("message", {})
                    usage = msg.get("usage", {})
                    model = msg.get("model", "")

                    input_tokens = usage.get("input_tokens", 0) or 0
                    output_tokens = usage.get("output_tokens", 0) or 0
                    cache_read = usage.get("cache_read_input_tokens", 0) or 0
                    cache_creation = usage.get("cache_creation_input_tokens", 0) or 0

                    # Only record turns that have actual token usage
                    if input_tokens + output_tokens + cache_read + cache_creation == 0:
                        continue

                    # Extract tool name from content if present
                    tool_name = None
                    for item in msg.get("content", []):
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            tool_name = item.get("name")
                            break

                    if model:
                        session_meta[session_id]["model"] = model

                    turns.append({
                        "session_id": session_id,
                        "timestamp": timestamp,
                        "model": model,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_read_tokens": cache_read,
                        "cache_creation_tokens": cache_creation,
                        "tool_name": tool_name,
                        "cwd": cwd,
                    })

    except Exception as e:
        print(f"  Warning: error reading {filepath}: {e}")

    return list(session_meta.values()), turns


def aggregate_sessions(session_metas, turns):
    """Aggregate turn data back into session-level stats."""
    from collections import defaultdict

    session_stats = defaultdict(lambda: {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read": 0,
        "total_cache_creation": 0,
        "turn_count": 0,
        "model": None,
    })

    for t in turns:
        s = session_stats[t["session_id"]]
        s["total_input_tokens"] += t["input_tokens"]
        s["total_output_tokens"] += t["output_tokens"]
        s["total_cache_read"] += t["cache_read_tokens"]
        s["total_cache_creation"] += t["cache_creation_tokens"]
        s["turn_count"] += 1
        if t["model"]:
            s["model"] = t["model"]

    # Merge into session_metas
    result = []
    for meta in session_metas:
        sid = meta["session_id"]
        stats = session_stats[sid]
        result.append({**meta, **stats})
    return result


def upsert_sessions(conn, sessions, user_id=None):
    uid = user_id or ACTIVE_USER
    for s in sessions:
        # Check if session exists
        existing = conn.execute(
            "SELECT total_input_tokens, total_output_tokens, total_cache_read, "
            "total_cache_creation, turn_count FROM sessions WHERE session_id = ?",
            (s["session_id"],)
        ).fetchone()

        if existing is None:
            conn.execute("""
                INSERT INTO sessions
                    (session_id, project_name, first_timestamp, last_timestamp,
                     git_branch, total_input_tokens, total_output_tokens,
                     total_cache_read, total_cache_creation, model, turn_count, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                s["session_id"], s["project_name"], s["first_timestamp"],
                s["last_timestamp"], s["git_branch"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s["model"], s["turn_count"], uid
            ))
        else:
            # Update: add new tokens on top of existing (since we only insert new turns)
            conn.execute("""
                UPDATE sessions SET
                    last_timestamp = MAX(last_timestamp, ?),
                    total_input_tokens = total_input_tokens + ?,
                    total_output_tokens = total_output_tokens + ?,
                    total_cache_read = total_cache_read + ?,
                    total_cache_creation = total_cache_creation + ?,
                    turn_count = turn_count + ?,
                    model = COALESCE(?, model)
                WHERE session_id = ?
            """, (
                s["last_timestamp"],
                s["total_input_tokens"], s["total_output_tokens"],
                s["total_cache_read"], s["total_cache_creation"],
                s["turn_count"], s["model"],
                s["session_id"]
            ))


def insert_turns(conn, turns, user_id=None):
    uid = user_id or ACTIVE_USER
    conn.executemany("""
        INSERT INTO turns
            (session_id, timestamp, model, input_tokens, output_tokens,
             cache_read_tokens, cache_creation_tokens, tool_name, cwd, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (t["session_id"], t["timestamp"], t["model"],
         t["input_tokens"], t["output_tokens"],
         t["cache_read_tokens"], t["cache_creation_tokens"],
         t["tool_name"], t["cwd"], uid)
        for t in turns
    ])


def scan(projects_dir=PROJECTS_DIR, db_path=DB_PATH, verbose=True, user_id=None):
    conn = get_db(db_path)
    init_db(conn)

    jsonl_files = glob.glob(str(projects_dir / "**" / "*.jsonl"), recursive=True)
    jsonl_files.sort()

    new_files = 0
    updated_files = 0
    skipped_files = 0
    total_turns = 0
    total_sessions = set()

    for filepath in jsonl_files:
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            continue

        row = conn.execute(
            "SELECT mtime, lines FROM processed_files WHERE path = ?",
            (filepath,)
        ).fetchone()

        if row and abs(row["mtime"] - mtime) < 0.01:
            skipped_files += 1
            continue

        is_new = row is None
        if verbose:
            status = "NEW" if is_new else "UPD"
            print(f"  [{status}] {os.path.relpath(filepath, projects_dir)}")

        session_metas, turns = parse_jsonl_file(filepath)

        if turns or session_metas:
            sessions = aggregate_sessions(session_metas, turns)

            # For incremental updates: only insert turns not already in DB
            if not is_new:
                old_lines = row["lines"] if row else 0
                current_lines = sum(1 for _ in open(filepath, encoding="utf-8", errors="replace"))

                if current_lines <= old_lines:
                    conn.execute("UPDATE processed_files SET mtime = ? WHERE path = ?",
                                 (mtime, filepath))
                    conn.commit()
                    skipped_files += 1
                    continue

                # Only process the new lines
                new_turns = []
                new_metas = {}
                try:
                    with open(filepath, encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f):
                            if i < old_lines:
                                continue
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                record = json.loads(line)
                            except json.JSONDecodeError:
                                continue

                            rtype = record.get("type")
                            if rtype != "assistant":
                                continue

                            session_id = record.get("sessionId")
                            if not session_id:
                                continue

                            msg = record.get("message", {})
                            usage = msg.get("usage", {})
                            input_tokens = usage.get("input_tokens", 0) or 0
                            output_tokens = usage.get("output_tokens", 0) or 0
                            cache_read = usage.get("cache_read_input_tokens", 0) or 0
                            cache_creation = usage.get("cache_creation_input_tokens", 0) or 0

                            if input_tokens + output_tokens + cache_read + cache_creation == 0:
                                continue

                            tool_name = None
                            for item in msg.get("content", []):
                                if isinstance(item, dict) and item.get("type") == "tool_use":
                                    tool_name = item.get("name")
                                    break

                            new_turns.append({
                                "session_id": session_id,
                                "timestamp": record.get("timestamp", ""),
                                "model": msg.get("model", ""),
                                "input_tokens": input_tokens,
                                "output_tokens": output_tokens,
                                "cache_read_tokens": cache_read,
                                "cache_creation_tokens": cache_creation,
                                "tool_name": tool_name,
                                "cwd": record.get("cwd", ""),
                            })
                except Exception as e:
                    print(f"  Warning: {e}")

                turns = new_turns
                sessions = aggregate_sessions(list(new_metas.values()) or [], turns)
                for meta in session_metas:
                    sessions_to_update = [s for s in sessions if s["session_id"] == meta["session_id"]]
                    if not sessions_to_update:
                        sessions.append({**meta,
                                         "total_input_tokens": 0,
                                         "total_output_tokens": 0,
                                         "total_cache_read": 0,
                                         "total_cache_creation": 0,
                                         "turn_count": 0,
                                         "model": meta.get("model")})

                updated_files += 1
            else:
                new_files += 1

            upsert_sessions(conn, sessions, user_id=user_id)
            insert_turns(conn, turns, user_id=user_id)

            for s in sessions:
                total_sessions.add(s["session_id"])
            total_turns += len(turns)

        # Record file as processed.
        line_count = 0
        last_line = ""
        with open(filepath, encoding="utf-8", errors="replace") as _f:
            for _line in _f:
                line_count += 1
                last_line = _line
        if last_line.strip():
            try:
                json.loads(last_line.strip())
            except json.JSONDecodeError:
                line_count -= 1
        conn.execute("""
            INSERT OR REPLACE INTO processed_files (path, mtime, lines)
            VALUES (?, ?, ?)
        """, (filepath, mtime, line_count))
        conn.commit()

    if verbose:
        print(f"\nScan complete:")
        print(f"  New files:     {new_files}")
        print(f"  Updated files: {updated_files}")
        print(f"  Skipped files: {skipped_files}")
        print(f"  Turns added:   {total_turns}")
        print(f"  Sessions seen: {len(total_sessions)}")

        # Show cost pulse after scan
        try:
            from insights import generate_pulse, format_pulse_cli
            pulse = generate_pulse(db_path)
            pulse_str = format_pulse_cli(pulse)
            if pulse_str:
                print()
                print(pulse_str)
        except Exception:
            pass

    conn.close()

    # Evaluate threshold hooks after every scan (silently — never crash scanner)
    try:
        from hooks import check_and_fire
        from config import HOOKS_PATH
        check_and_fire(db_path, HOOKS_PATH)
    except Exception:
        pass

    # Run anomaly detection after each scan
    try:
        from anomaly import detect_anomalies
        detect_anomalies(db_path)
    except Exception:
        pass

    # Rebuild FTS index
    try:
        rebuild_fts(db_path)
    except Exception:
        pass

    # Fire plugin hooks
    try:
        from plugins import run_hook
        scan_result = {
            "new": new_files, "updated": updated_files,
            "skipped": skipped_files, "turns": total_turns,
            "sessions": len(total_sessions),
        }
        run_hook("after_scan", scan_result)
    except Exception:
        pass

    # Fire on_alert for any anomalies detected this scan
    try:
        from anomaly import get_recent_anomalies
        from plugins import run_hook as _run_alert_hook
        recent = get_recent_anomalies(db_path, days=0, limit=10)
        for anomaly_record in recent:
            if not anomaly_record.get("acknowledged"):
                _run_alert_hook("on_alert", anomaly_record)
    except Exception:
        pass

    # Automatic circuit breaker check (runs only when enabled in config)
    breaker_result = None
    try:
        from circuit_breaker import auto_check
        breaker_result = auto_check(db_path)
    except Exception:
        pass

    return {"new": new_files, "updated": updated_files, "skipped": skipped_files,
            "turns": total_turns, "sessions": len(total_sessions),
            "breaker": breaker_result}


if __name__ == "__main__":
    print(f"Scanning {PROJECTS_DIR} ...")
    scan()
