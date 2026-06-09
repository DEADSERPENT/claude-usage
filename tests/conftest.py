import sys
import sqlite3
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from scanner import get_db, init_db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = get_db(path)
    init_db(conn)
    conn.close()
    return path


@pytest.fixture
def populated_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO sessions (session_id, project_name, first_timestamp, last_timestamp, "
        "git_branch, total_input_tokens, total_output_tokens, total_cache_read, "
        "total_cache_creation, model, turn_count, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("sess-aaa", "proj/alpha", "2026-06-01T10:00:00", "2026-06-01T11:00:00",
             "main", 100_000, 50_000, 10_000, 5_000, "claude-sonnet-4-6", 10, "default"),
            ("sess-bbb", "proj/beta",  "2026-06-02T09:00:00", "2026-06-02T10:00:00",
             "feat", 200_000, 80_000, 20_000, 8_000, "claude-opus-4-6",   15, "default"),
            ("sess-ccc", "proj/alpha", "2026-06-05T14:00:00", "2026-06-05T15:00:00",
             "main",  50_000, 30_000,  5_000,  2_000, "claude-haiku-4-5",  5, "default"),
        ],
    )
    conn.executemany(
        "INSERT INTO turns (session_id, timestamp, model, input_tokens, output_tokens, "
        "cache_read_tokens, cache_creation_tokens, tool_name, cwd, user_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("sess-aaa", "2026-06-01T10:00:00", "claude-sonnet-4-6",
             10_000,  5_000, 1_000,   500, None,   "/proj/alpha", "default"),
            ("sess-aaa", "2026-06-01T10:30:00", "claude-sonnet-4-6",
             90_000, 45_000, 9_000, 4_500, "Bash", "/proj/alpha", "default"),
            ("sess-bbb", "2026-06-02T09:00:00", "claude-opus-4-6",
             200_000, 80_000, 20_000, 8_000, "Read", "/proj/beta", "default"),
        ],
    )
    conn.commit()
    conn.close()
    return db_path
