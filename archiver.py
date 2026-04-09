"""
archiver.py - Data compression, archival, and time-travel for claude-usage.

Features:
- Archive old data by month into compressed SQLite snapshots
- Restore archived data for analysis
- Time-travel: query DB state at a specific point in time
- Export full snapshots for backup

Archives are stored as gzipped SQLite files:
    ~/.claude/usage_archives/2026-01.db.gz
    ~/.claude/usage_archives/2026-02.db.gz
"""

import gzip
import shutil
import sqlite3
import os
from datetime import datetime, date, timedelta
from pathlib import Path

from config import DB_PATH, ARCHIVE_DIR


def ensure_archive_dir():
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


def get_archivable_months(db_path: Path = DB_PATH, keep_months: int = 2) -> list[str]:
    """
    Returns list of year-month strings (e.g. '2026-01') that can be archived.
    Keeps the most recent `keep_months` months in the live DB.
    """
    if not db_path.exists():
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT DISTINCT substr(timestamp, 1, 7) as month
        FROM turns
        ORDER BY month
    """).fetchall()
    conn.close()

    months = [r["month"] for r in rows]
    if len(months) <= keep_months:
        return []

    return months[:-keep_months]


def archive_month(month: str, db_path: Path = DB_PATH,
                  archive_dir: Path = ARCHIVE_DIR) -> dict:
    """
    Archive all data for a given month into a compressed file.

    Creates: archive_dir/YYYY-MM.db.gz
    Removes archived turns from the live database.
    """
    ensure_archive_dir()

    archive_db_path = archive_dir / f"{month}.db"
    archive_gz_path = archive_dir / f"{month}.db.gz"

    if archive_gz_path.exists():
        return {"status": "skipped", "month": month, "reason": "archive already exists"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Count what we'll archive
    turn_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM turns WHERE substr(timestamp, 1, 7) = ?",
        (month,)
    ).fetchone()["cnt"]

    if turn_count == 0:
        conn.close()
        return {"status": "skipped", "month": month, "reason": "no data for this month"}

    session_ids = conn.execute("""
        SELECT DISTINCT session_id FROM turns
        WHERE substr(timestamp, 1, 7) = ?
    """, (month,)).fetchall()
    session_id_list = [r["session_id"] for r in session_ids]

    # Create archive database
    archive_conn = sqlite3.connect(archive_db_path)
    archive_conn.execute("PRAGMA journal_mode=WAL;")
    archive_conn.executescript("""
        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY,
            session_id TEXT, timestamp TEXT, model TEXT,
            input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0, cache_creation_tokens INTEGER DEFAULT 0,
            tool_name TEXT, cwd TEXT, user_id TEXT DEFAULT 'default'
        );
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY, project_name TEXT,
            first_timestamp TEXT, last_timestamp TEXT, git_branch TEXT,
            total_input_tokens INTEGER DEFAULT 0, total_output_tokens INTEGER DEFAULT 0,
            total_cache_read INTEGER DEFAULT 0, total_cache_creation INTEGER DEFAULT 0,
            model TEXT, turn_count INTEGER DEFAULT 0, user_id TEXT DEFAULT 'default'
        );
        CREATE TABLE IF NOT EXISTS archive_meta (
            key TEXT PRIMARY KEY, value TEXT
        );
    """)

    # Copy turns
    turns = conn.execute(
        "SELECT * FROM turns WHERE substr(timestamp, 1, 7) = ?",
        (month,)
    ).fetchall()

    for t in turns:
        cols = t.keys()
        placeholders = ",".join("?" for _ in cols)
        col_names = ",".join(cols)
        archive_conn.execute(
            f"INSERT OR IGNORE INTO turns ({col_names}) VALUES ({placeholders})",
            tuple(t[c] for c in cols)
        )

    # Copy related sessions
    if session_id_list:
        placeholders = ",".join("?" for _ in session_id_list)
        sessions = conn.execute(
            f"SELECT * FROM sessions WHERE session_id IN ({placeholders})",
            session_id_list
        ).fetchall()
        for s in sessions:
            cols = s.keys()
            ph = ",".join("?" for _ in cols)
            cn = ",".join(cols)
            archive_conn.execute(
                f"INSERT OR IGNORE INTO sessions ({cn}) VALUES ({ph})",
                tuple(s[c] for c in cols)
            )

    # Store metadata
    archive_conn.execute(
        "INSERT OR REPLACE INTO archive_meta (key, value) VALUES ('month', ?)", (month,)
    )
    archive_conn.execute(
        "INSERT OR REPLACE INTO archive_meta (key, value) VALUES ('archived_at', ?)",
        (datetime.now().isoformat(),)
    )
    archive_conn.execute(
        "INSERT OR REPLACE INTO archive_meta (key, value) VALUES ('turn_count', ?)",
        (str(turn_count),)
    )
    archive_conn.commit()
    archive_conn.close()

    # Compress
    with open(archive_db_path, "rb") as f_in:
        with gzip.open(archive_gz_path, "wb", compresslevel=9) as f_out:
            shutil.copyfileobj(f_in, f_out)
    os.remove(archive_db_path)

    # Remove archived turns from live DB
    conn.execute("DELETE FROM turns WHERE substr(timestamp, 1, 7) = ?", (month,))
    conn.commit()

    # Recalculate session stats for affected sessions (they may still have
    # turns from other months)
    for sid in session_id_list:
        remaining = conn.execute("""
            SELECT SUM(input_tokens) as inp, SUM(output_tokens) as out,
                   SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
                   COUNT(*) as turns, MIN(timestamp) as first, MAX(timestamp) as last
            FROM turns WHERE session_id = ?
        """, (sid,)).fetchone()

        if remaining["turns"] and remaining["turns"] > 0:
            conn.execute("""
                UPDATE sessions SET
                    total_input_tokens = ?, total_output_tokens = ?,
                    total_cache_read = ?, total_cache_creation = ?,
                    turn_count = ?, first_timestamp = ?, last_timestamp = ?
                WHERE session_id = ?
            """, (
                remaining["inp"] or 0, remaining["out"] or 0,
                remaining["cr"] or 0, remaining["cc"] or 0,
                remaining["turns"], remaining["first"], remaining["last"], sid
            ))
        else:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))

    conn.execute("VACUUM")
    conn.commit()
    conn.close()

    original_size = archive_gz_path.stat().st_size
    return {
        "status": "archived",
        "month": month,
        "turns": turn_count,
        "sessions": len(session_id_list),
        "archive_path": str(archive_gz_path),
        "archive_size_bytes": original_size,
    }


def list_archives(archive_dir: Path = ARCHIVE_DIR) -> list[dict]:
    """List all available archives."""
    if not archive_dir.exists():
        return []

    archives = []
    for gz_file in sorted(archive_dir.glob("*.db.gz")):
        month = gz_file.stem.replace(".db", "")
        size = gz_file.stat().st_size

        # Try to read metadata
        meta = {"month": month, "turn_count": "unknown"}
        try:
            tmp_path = archive_dir / f"_tmp_{month}.db"
            with gzip.open(gz_file, "rb") as f_in:
                with open(tmp_path, "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)
            tmp_conn = sqlite3.connect(tmp_path)
            tmp_conn.row_factory = sqlite3.Row
            for row in tmp_conn.execute("SELECT key, value FROM archive_meta").fetchall():
                meta[row["key"]] = row["value"]
            tmp_conn.close()
            os.remove(tmp_path)
        except Exception:
            pass

        archives.append({
            "month": month,
            "file": str(gz_file),
            "size_bytes": size,
            "size_human": f"{size / 1024:.1f} KB" if size < 1_048_576 else f"{size / 1_048_576:.2f} MB",
            "turns": meta.get("turn_count", "?"),
            "archived_at": meta.get("archived_at", "?"),
        })

    return archives


def restore_archive(month: str, db_path: Path = DB_PATH,
                    archive_dir: Path = ARCHIVE_DIR) -> dict:
    """Restore archived data back into the live database."""
    gz_path = archive_dir / f"{month}.db.gz"
    if not gz_path.exists():
        return {"status": "error", "message": f"Archive {month}.db.gz not found"}

    tmp_path = archive_dir / f"_restore_{month}.db"
    with gzip.open(gz_path, "rb") as f_in:
        with open(tmp_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

    archive_conn = sqlite3.connect(tmp_path)
    archive_conn.row_factory = sqlite3.Row
    live_conn = sqlite3.connect(db_path)
    live_conn.row_factory = sqlite3.Row

    # Restore turns
    turns = archive_conn.execute("SELECT * FROM turns").fetchall()
    turn_count = 0
    for t in turns:
        cols = [c for c in t.keys() if c != "id"]
        placeholders = ",".join("?" for _ in cols)
        col_names = ",".join(cols)
        live_conn.execute(
            f"INSERT OR IGNORE INTO turns ({col_names}) VALUES ({placeholders})",
            tuple(t[c] for c in cols)
        )
        turn_count += 1

    # Restore sessions
    sessions = archive_conn.execute("SELECT * FROM sessions").fetchall()
    sess_count = 0
    for s in sessions:
        existing = live_conn.execute(
            "SELECT session_id FROM sessions WHERE session_id = ?",
            (s["session_id"],)
        ).fetchone()
        if not existing:
            cols = s.keys()
            ph = ",".join("?" for _ in cols)
            cn = ",".join(cols)
            live_conn.execute(
                f"INSERT INTO sessions ({cn}) VALUES ({ph})",
                tuple(s[c] for c in cols)
            )
            sess_count += 1

    live_conn.commit()
    live_conn.close()
    archive_conn.close()
    os.remove(tmp_path)

    return {
        "status": "restored",
        "month": month,
        "turns_restored": turn_count,
        "sessions_restored": sess_count,
    }


def time_travel_query(at_datetime: str, db_path: Path = DB_PATH,
                      archive_dir: Path = ARCHIVE_DIR) -> dict:
    """
    Reconstruct usage state at a specific point in time.
    Queries both live DB and archives as needed.

    at_datetime: ISO format string like '2026-04-01 10:00' or '2026-04-01'
    """
    # Normalize to full datetime
    if len(at_datetime) == 10:
        at_datetime += "T23:59:59"
    at_datetime = at_datetime.replace(" ", "T")

    result = {
        "as_of": at_datetime,
        "total_tokens": 0,
        "total_input": 0,
        "total_output": 0,
        "total_turns": 0,
        "total_sessions": 0,
        "total_cost": 0.0,
        "by_model": {},
        "by_project": {},
        "sources": [],
    }

    from config import PRICING

    def _cost(model, inp, out, cr, cc):
        p = PRICING.get(model)
        if p is None:
            for key in PRICING:
                if key != "default" and model and model.startswith(key):
                    p = PRICING[key]
                    break
        if p is None:
            p = PRICING.get("default", {})
        return (inp * p.get("input", 0) / 1e6 + out * p.get("output", 0) / 1e6 +
                cr * p.get("cache_read", 0) / 1e6 + cc * p.get("cache_write", 0) / 1e6)

    def _aggregate_db(conn, cutoff):
        rows = conn.execute("""
            SELECT COALESCE(model, 'unknown') as model,
                   SUM(input_tokens) as inp, SUM(output_tokens) as out,
                   SUM(cache_read_tokens) as cr, SUM(cache_creation_tokens) as cc,
                   COUNT(*) as turns, COUNT(DISTINCT session_id) as sessions
            FROM turns
            WHERE timestamp <= ?
            GROUP BY model
        """, (cutoff,)).fetchall()

        for r in rows:
            model = r["model"]
            inp, out = r["inp"] or 0, r["out"] or 0
            cr, cc = r["cr"] or 0, r["cc"] or 0
            cost = _cost(model, inp, out, cr, cc)

            result["total_input"] += inp
            result["total_output"] += out
            result["total_turns"] += r["turns"]
            result["total_sessions"] += r["sessions"]
            result["total_cost"] += cost

            if model not in result["by_model"]:
                result["by_model"][model] = {"input": 0, "output": 0, "cost": 0, "turns": 0}
            result["by_model"][model]["input"] += inp
            result["by_model"][model]["output"] += out
            result["by_model"][model]["cost"] += cost
            result["by_model"][model]["turns"] += r["turns"]

        # By project
        proj_rows = conn.execute("""
            SELECT s.project_name,
                   SUM(t.input_tokens + t.output_tokens) as tokens,
                   COUNT(DISTINCT t.session_id) as sessions
            FROM turns t LEFT JOIN sessions s USING(session_id)
            WHERE t.timestamp <= ?
            GROUP BY s.project_name
            ORDER BY tokens DESC LIMIT 10
        """, (cutoff,)).fetchall()

        for r in proj_rows:
            proj = r["project_name"] or "unknown"
            result["by_project"][proj] = {
                "tokens": r["tokens"] or 0,
                "sessions": r["sessions"],
            }

    # Query live database
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _aggregate_db(conn, at_datetime)
        conn.close()
        result["sources"].append("live_db")

    # Query relevant archives
    target_month = at_datetime[:7]
    if archive_dir.exists():
        for gz_file in archive_dir.glob("*.db.gz"):
            archive_month = gz_file.stem.replace(".db", "")
            if archive_month <= target_month:
                try:
                    tmp_path = archive_dir / f"_tt_{archive_month}.db"
                    with gzip.open(gz_file, "rb") as f_in:
                        with open(tmp_path, "wb") as f_out:
                            shutil.copyfileobj(f_in, f_out)
                    arch_conn = sqlite3.connect(tmp_path)
                    arch_conn.row_factory = sqlite3.Row
                    _aggregate_db(arch_conn, at_datetime)
                    arch_conn.close()
                    os.remove(tmp_path)
                    result["sources"].append(f"archive:{archive_month}")
                except Exception:
                    pass

    result["total_tokens"] = result["total_input"] + result["total_output"]
    result["total_cost"] = round(result["total_cost"], 4)

    return result


def create_snapshot(db_path: Path = DB_PATH, output_path: Path = None) -> dict:
    """Create a full compressed snapshot of the current database."""
    if not db_path.exists():
        return {"status": "error", "message": "Database not found"}

    if output_path is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        ensure_archive_dir()
        output_path = ARCHIVE_DIR / f"snapshot_{timestamp}.db.gz"

    with open(db_path, "rb") as f_in:
        with gzip.open(output_path, "wb", compresslevel=9) as f_out:
            shutil.copyfileobj(f_in, f_out)

    return {
        "status": "created",
        "path": str(output_path),
        "size_bytes": output_path.stat().st_size,
        "original_size": db_path.stat().st_size,
    }
