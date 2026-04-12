"""
sync.py - Cross-machine SQLite synchronization for claude-usage.

Generates portable JSON diffs of turns/sessions for transfer between machines.
Uses deterministic hashing to prevent duplicates during import.
"""

import json
import hashlib
import sqlite3
import socket
from datetime import datetime
from pathlib import Path

from config import DB_PATH, SYNC_DIR


def _turn_hash(turn: dict) -> str:
    """Deterministic hash for a turn to prevent duplicates."""
    parts = "|".join(str(turn.get(k, "")) for k in [
        "session_id", "timestamp", "model",
        "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_creation_tokens",
        "tool_name", "cwd"
    ])
    return hashlib.sha256(parts.encode()).hexdigest()[:16]


def _session_hash(session: dict) -> str:
    """Deterministic hash for a session."""
    parts = "|".join(str(session.get(k, "")) for k in [
        "session_id", "project_name", "first_timestamp", "model"
    ])
    return hashlib.sha256(parts.encode()).hexdigest()[:16]


def export_sync(db_path: Path = DB_PATH, output_path: Path = None,
                since: str = None) -> dict:
    """Export turns and sessions as a portable JSON sync file."""
    SYNC_DIR.mkdir(parents=True, exist_ok=True)

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = SYNC_DIR / f"sync_export_{ts}.json"

    if not db_path.exists():
        return {"status": "error", "message": "Database not found"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    turn_query = "SELECT * FROM turns"
    params = []
    if since:
        turn_query += " WHERE timestamp >= ?"
        params.append(since)
    turn_query += " ORDER BY timestamp"

    turns_raw = conn.execute(turn_query, params).fetchall()
    sessions_raw = conn.execute("SELECT * FROM sessions ORDER BY first_timestamp").fetchall()
    conn.close()

    turns = []
    for t in turns_raw:
        td = dict(t)
        td.pop("id", None)  # Remove autoincrement ID
        td["_hash"] = _turn_hash(td)
        turns.append(td)

    sessions = []
    for s in sessions_raw:
        sd = dict(s)
        sd["_hash"] = _session_hash(sd)
        sessions.append(sd)

    payload = {
        "format_version": 1,
        "exported_at": datetime.now().isoformat(),
        "source_host": socket.gethostname(),
        "db_path": str(db_path),
        "stats": {
            "turns": len(turns),
            "sessions": len(sessions),
        },
        "turns": turns,
        "sessions": sessions,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)

    return {
        "status": "exported",
        "path": str(output_path),
        "turns": len(turns),
        "sessions": len(sessions),
        "size_bytes": output_path.stat().st_size,
    }


def import_sync(sync_path: Path, db_path: Path = DB_PATH) -> dict:
    """Import a sync file, merging data without duplicates."""
    if not sync_path.exists():
        return {"status": "error", "message": f"Sync file not found: {sync_path}"}

    with open(sync_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    if payload.get("format_version") != 1:
        return {"status": "error", "message": "Unsupported sync format version"}

    from scanner import init_db
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_db(conn)

    # Build set of existing turn hashes
    existing_turns = conn.execute("SELECT session_id, timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, tool_name, cwd FROM turns").fetchall()
    existing_hashes = set()
    for t in existing_turns:
        existing_hashes.add(_turn_hash(dict(t)))

    # Import turns
    imported_turns = 0
    skipped_turns = 0
    for turn in payload.get("turns", []):
        h = turn.get("_hash") or _turn_hash(turn)
        if h in existing_hashes:
            skipped_turns += 1
            continue

        conn.execute("""
            INSERT INTO turns (session_id, timestamp, model, input_tokens, output_tokens,
                             cache_read_tokens, cache_creation_tokens, tool_name, cwd, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            turn.get("session_id"), turn.get("timestamp"), turn.get("model"),
            turn.get("input_tokens", 0), turn.get("output_tokens", 0),
            turn.get("cache_read_tokens", 0), turn.get("cache_creation_tokens", 0),
            turn.get("tool_name"), turn.get("cwd"), turn.get("user_id", "default"),
        ))
        existing_hashes.add(h)
        imported_turns += 1

    # Import sessions (upsert)
    imported_sessions = 0
    for sess in payload.get("sessions", []):
        existing = conn.execute(
            "SELECT session_id FROM sessions WHERE session_id = ?",
            (sess["session_id"],)
        ).fetchone()

        if not existing:
            conn.execute("""
                INSERT INTO sessions (session_id, project_name, first_timestamp, last_timestamp,
                    git_branch, total_input_tokens, total_output_tokens,
                    total_cache_read, total_cache_creation, model, turn_count, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sess["session_id"], sess.get("project_name"),
                sess.get("first_timestamp"), sess.get("last_timestamp"),
                sess.get("git_branch"),
                sess.get("total_input_tokens", 0), sess.get("total_output_tokens", 0),
                sess.get("total_cache_read", 0), sess.get("total_cache_creation", 0),
                sess.get("model"), sess.get("turn_count", 0),
                sess.get("user_id", "default"),
            ))
            imported_sessions += 1
        else:
            # Update if the incoming data has newer timestamps
            conn.execute("""
                UPDATE sessions SET
                    last_timestamp = MAX(last_timestamp, ?),
                    total_input_tokens = MAX(total_input_tokens, ?),
                    total_output_tokens = MAX(total_output_tokens, ?),
                    total_cache_read = MAX(total_cache_read, ?),
                    total_cache_creation = MAX(total_cache_creation, ?),
                    turn_count = MAX(turn_count, ?)
                WHERE session_id = ?
            """, (
                sess.get("last_timestamp", ""),
                sess.get("total_input_tokens", 0),
                sess.get("total_output_tokens", 0),
                sess.get("total_cache_read", 0),
                sess.get("total_cache_creation", 0),
                sess.get("turn_count", 0),
                sess["session_id"],
            ))

    conn.commit()
    conn.close()

    return {
        "status": "imported",
        "source_host": payload.get("source_host", "unknown"),
        "exported_at": payload.get("exported_at", "unknown"),
        "turns_imported": imported_turns,
        "turns_skipped": skipped_turns,
        "sessions_imported": imported_sessions,
    }
