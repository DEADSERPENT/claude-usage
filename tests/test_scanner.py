import json
import sqlite3
from pathlib import Path

import pytest

from scanner import (
    project_name_from_cwd,
    parse_jsonl_file,
    aggregate_sessions,
    init_db,
    get_db,
    upsert_sessions,
    insert_turns,
    _column_exists,
    _table_exists,
)


# ── project_name_from_cwd ─────────────────────────────────────────────────────

class TestProjectNameFromCwd:
    def test_none(self):
        assert project_name_from_cwd(None) == "unknown"

    def test_empty_string(self):
        assert project_name_from_cwd("") == "unknown"

    def test_single_component(self):
        # "/myproject".split("/") → ["", "myproject"] → joined last 2 = "/myproject"
        result = project_name_from_cwd("/myproject")
        assert "myproject" in result

    def test_two_components(self):
        assert project_name_from_cwd("/home/myproject") == "home/myproject"

    def test_deep_path(self):
        result = project_name_from_cwd("/home/user/code/myproject")
        assert result == "code/myproject"

    def test_windows_backslash(self):
        result = project_name_from_cwd("C:\\Users\\dev\\myproject")
        assert "myproject" in result

    def test_trailing_slash(self):
        result = project_name_from_cwd("/home/user/myproject/")
        assert "myproject" in result

    def test_root_slash(self):
        result = project_name_from_cwd("/")
        assert result == "unknown" or result == ""


# ── parse_jsonl_file ──────────────────────────────────────────────────────────

class TestParseJsonlFile:
    def _write(self, tmp_path, records, name="test.jsonl"):
        f = tmp_path / name
        f.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n",
            encoding="utf-8",
        )
        return f

    def _assistant_record(self, session_id="sess-1", inp=1000, out=500,
                          model="claude-sonnet-4-6", tool=None):
        content = []
        if tool:
            content.append({"type": "tool_use", "name": tool})
        return {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": "2026-06-01T10:00:00Z",
            "cwd": f"/proj/{session_id}",
            "gitBranch": "main",
            "message": {
                "model": model,
                "usage": {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
                "content": content,
            },
        }

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("", encoding="utf-8")
        sessions, turns = parse_jsonl_file(f)
        assert sessions == []
        assert turns == []

    def test_single_valid_record(self, tmp_path):
        f = self._write(tmp_path, [self._assistant_record()])
        sessions, turns = parse_jsonl_file(f)
        assert len(sessions) == 1
        assert len(turns) == 1
        assert sessions[0]["session_id"] == "sess-1"
        assert turns[0]["input_tokens"] == 1000
        assert turns[0]["output_tokens"] == 500
        assert turns[0]["model"] == "claude-sonnet-4-6"

    def test_skips_zero_token_record(self, tmp_path):
        record = {
            "type": "assistant",
            "sessionId": "sess-1",
            "timestamp": "2026-06-01T10:00:00Z",
            "message": {"model": "m", "usage": {}, "content": []},
        }
        f = self._write(tmp_path, [record])
        _, turns = parse_jsonl_file(f)
        assert turns == []

    def test_user_type_skipped_for_turns(self, tmp_path):
        record = {
            "type": "user",
            "sessionId": "sess-1",
            "timestamp": "2026-06-01T10:00:00Z",
            "message": {"content": "Hello"},
        }
        f = self._write(tmp_path, [record])
        _, turns = parse_jsonl_file(f)
        assert turns == []

    def test_user_type_creates_session_meta(self, tmp_path):
        # user records should still register session metadata
        records = [
            {"type": "user", "sessionId": "sess-1",
             "timestamp": "2026-06-01T09:00:00Z", "cwd": "/proj"},
            self._assistant_record(),
        ]
        f = self._write(tmp_path, records)
        sessions, _ = parse_jsonl_file(f)
        assert any(s["session_id"] == "sess-1" for s in sessions)

    def test_invalid_json_line_skipped(self, tmp_path):
        f = tmp_path / "mixed.jsonl"
        valid = json.dumps(self._assistant_record())
        f.write_text(f"{valid}\nINVALID JSON\n", encoding="utf-8")
        _, turns = parse_jsonl_file(f)
        assert len(turns) == 1

    def test_tool_name_extracted(self, tmp_path):
        f = self._write(tmp_path, [self._assistant_record(tool="Bash")])
        _, turns = parse_jsonl_file(f)
        assert turns[0]["tool_name"] == "Bash"

    def test_no_tool_name_is_none(self, tmp_path):
        f = self._write(tmp_path, [self._assistant_record(tool=None)])
        _, turns = parse_jsonl_file(f)
        assert turns[0]["tool_name"] is None

    def test_multiple_sessions(self, tmp_path):
        records = [
            self._assistant_record("sess-A", inp=100, out=50),
            self._assistant_record("sess-B", inp=200, out=100),
        ]
        f = self._write(tmp_path, records)
        sessions, turns = parse_jsonl_file(f)
        assert len(sessions) == 2
        assert len(turns) == 2
        session_ids = {s["session_id"] for s in sessions}
        assert session_ids == {"sess-A", "sess-B"}

    def test_cache_tokens_parsed(self, tmp_path):
        record = {
            "type": "assistant",
            "sessionId": "sess-1",
            "timestamp": "2026-06-01T10:00:00Z",
            "message": {
                "model": "m",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 15,
                },
                "content": [],
            },
        }
        f = self._write(tmp_path, [record])
        _, turns = parse_jsonl_file(f)
        assert turns[0]["cache_read_tokens"] == 30
        assert turns[0]["cache_creation_tokens"] == 15

    def test_git_branch_captured(self, tmp_path):
        record = {
            "type": "assistant",
            "sessionId": "sess-1",
            "timestamp": "2026-06-01T10:00:00Z",
            "gitBranch": "feature/foo",
            "message": {"model": "m",
                        "usage": {"input_tokens": 100, "output_tokens": 50},
                        "content": []},
        }
        f = self._write(tmp_path, [record])
        sessions, _ = parse_jsonl_file(f)
        assert sessions[0]["git_branch"] == "feature/foo"

    def test_timestamps_ordered(self, tmp_path):
        records = [
            {**self._assistant_record(), "timestamp": "2026-06-01T12:00:00Z"},
            {**self._assistant_record(), "timestamp": "2026-06-01T09:00:00Z"},
        ]
        f = self._write(tmp_path, records)
        sessions, _ = parse_jsonl_file(f)
        assert sessions[0]["first_timestamp"] == "2026-06-01T09:00:00Z"
        assert sessions[0]["last_timestamp"] == "2026-06-01T12:00:00Z"

    def test_missing_session_id_skipped(self, tmp_path):
        record = {
            "type": "assistant",
            "timestamp": "2026-06-01T10:00:00Z",
            "message": {"model": "m",
                        "usage": {"input_tokens": 100, "output_tokens": 50},
                        "content": []},
        }
        f = self._write(tmp_path, [record])
        sessions, turns = parse_jsonl_file(f)
        assert turns == []


# ── aggregate_sessions ────────────────────────────────────────────────────────

class TestAggregateSessions:
    def _meta(self, sid="s1"):
        return {"session_id": sid, "project_name": "proj",
                "first_timestamp": "t", "last_timestamp": "t",
                "git_branch": "main", "model": None}

    def _turn(self, sid="s1", inp=100, out=50, cr=10, cc=5, model="m"):
        return {"session_id": sid, "input_tokens": inp, "output_tokens": out,
                "cache_read_tokens": cr, "cache_creation_tokens": cc, "model": model}

    def test_empty_inputs(self):
        assert aggregate_sessions([], []) == []

    def test_meta_no_turns(self):
        result = aggregate_sessions([self._meta()], [])
        assert len(result) == 1
        r = result[0]
        assert r["total_input_tokens"] == 0
        assert r["turn_count"] == 0

    def test_single_turn_aggregated(self):
        result = aggregate_sessions([self._meta()], [self._turn(inp=500, out=250)])
        r = result[0]
        assert r["total_input_tokens"] == 500
        assert r["total_output_tokens"] == 250
        assert r["turn_count"] == 1

    def test_multiple_turns_summed(self):
        turns = [self._turn(inp=100, out=50), self._turn(inp=200, out=100)]
        result = aggregate_sessions([self._meta()], turns)
        r = result[0]
        assert r["total_input_tokens"] == 300
        assert r["total_output_tokens"] == 150
        assert r["turn_count"] == 2

    def test_cache_tokens_summed(self):
        turns = [self._turn(cr=10, cc=5), self._turn(cr=20, cc=10)]
        result = aggregate_sessions([self._meta()], turns)
        r = result[0]
        assert r["total_cache_read"] == 30
        assert r["total_cache_creation"] == 15

    def test_model_set_from_turns(self):
        turns = [self._turn(model="claude-sonnet-4-6")]
        result = aggregate_sessions([self._meta()], turns)
        assert result[0]["model"] == "claude-sonnet-4-6"

    def test_multiple_sessions_separated(self):
        metas = [self._meta("s1"), self._meta("s2")]
        turns = [self._turn("s1", inp=100, out=50),
                 self._turn("s2", inp=200, out=100)]
        result = aggregate_sessions(metas, turns)
        assert len(result) == 2
        by_id = {r["session_id"]: r for r in result}
        assert by_id["s1"]["total_input_tokens"] == 100
        assert by_id["s2"]["total_input_tokens"] == 200

    def test_turns_for_unknown_session_ignored(self):
        # Turn references a session not in metas → no crash, not added
        result = aggregate_sessions([self._meta("s1")], [self._turn("s-UNKNOWN")])
        assert len(result) == 1
        assert result[0]["total_input_tokens"] == 0


# ── init_db / schema ──────────────────────────────────────────────────────────

class TestInitDb:
    def test_creates_core_tables(self, db_path):
        conn = sqlite3.connect(db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        for expected in ("sessions", "turns", "processed_files", "anomalies", "users"):
            assert expected in tables, f"Missing table: {expected}"

    def test_idempotent(self, db_path):
        conn = get_db(db_path)
        init_db(conn)  # second call should not raise
        conn.close()

    def test_user_id_column_on_sessions(self, db_path):
        conn = get_db(db_path)  # sets row_factory=sqlite3.Row needed by _column_exists
        assert _column_exists(conn, "sessions", "user_id")
        conn.close()

    def test_user_id_column_on_turns(self, db_path):
        conn = get_db(db_path)
        assert _column_exists(conn, "turns", "user_id")
        conn.close()

    def test_default_user_inserted(self, db_path):
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT * FROM users WHERE user_id='default'").fetchone()
        conn.close()
        assert row is not None

    def test_wal_mode_enabled(self, db_path):
        conn = sqlite3.connect(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"


# ── upsert_sessions / insert_turns (DB round-trip) ───────────────────────────

class TestUpsertSessions:
    def _session(self, sid="s1"):
        return {
            "session_id": sid,
            "project_name": "proj",
            "first_timestamp": "2026-06-01T10:00:00",
            "last_timestamp": "2026-06-01T11:00:00",
            "git_branch": "main",
            "total_input_tokens": 100,
            "total_output_tokens": 50,
            "total_cache_read": 10,
            "total_cache_creation": 5,
            "model": "claude-sonnet-4-6",
            "turn_count": 2,
        }

    def test_insert_new_session(self, db_path):
        conn = sqlite3.connect(db_path)
        upsert_sessions(conn, [self._session()])
        conn.commit()
        row = conn.execute("SELECT * FROM sessions WHERE session_id='s1'").fetchone()
        conn.close()
        assert row is not None

    def test_update_existing_session_adds_tokens(self, db_path):
        conn = sqlite3.connect(db_path)
        upsert_sessions(conn, [self._session()])
        conn.commit()
        # Upsert again with more tokens
        s2 = self._session()
        s2["total_input_tokens"] = 50
        s2["total_output_tokens"] = 25
        s2["turn_count"] = 1
        upsert_sessions(conn, [s2])
        conn.commit()
        row = conn.execute(
            "SELECT total_input_tokens, turn_count FROM sessions WHERE session_id='s1'"
        ).fetchone()
        conn.close()
        # Should be 100 + 50 = 150
        assert row[0] == 150
        assert row[1] == 3  # 2 + 1

    def test_insert_turns(self, db_path):
        conn = sqlite3.connect(db_path)
        upsert_sessions(conn, [self._session()])
        insert_turns(conn, [{
            "session_id": "s1",
            "timestamp": "2026-06-01T10:00:00",
            "model": "claude-sonnet-4-6",
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "tool_name": None,
            "cwd": "/proj",
        }])
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM turns WHERE session_id='s1'").fetchone()[0]
        conn.close()
        assert count == 1
