import pytest
from query_engine import (
    _parse_number,
    _tokenize,
    _evaluate_condition,
    _evaluate_with_connectors,
    _compile_condition_sql,
    _build_sql_prefilter,
    execute_query,
    format_results,
)


# ── _parse_number ─────────────────────────────────────────────────────────────

class TestParseNumber:
    def test_plain_integer(self):
        assert _parse_number("1000") == 1000.0

    def test_k_suffix_upper(self):
        assert _parse_number("1K") == 1_000.0

    def test_k_suffix_lower(self):
        assert _parse_number("10k") == 10_000.0

    def test_m_suffix(self):
        assert _parse_number("1M") == 1_000_000.0

    def test_m_suffix_decimal(self):
        assert _parse_number("2.5M") == 2_500_000.0

    def test_b_suffix(self):
        assert _parse_number("1B") == 1_000_000_000.0

    def test_float(self):
        assert _parse_number("3.14") == pytest.approx(3.14)

    def test_strips_whitespace(self):
        assert _parse_number("  500  ") == 500.0

    def test_zero(self):
        assert _parse_number("0") == 0.0


# ── _tokenize ─────────────────────────────────────────────────────────────────

class TestTokenize:
    def test_single_equals(self):
        tokens = _tokenize("model=sonnet")
        assert len(tokens) == 1
        t = tokens[0]
        assert t["type"] == "condition"
        assert t["field"] == "model"
        assert t["op"] == "="
        assert t["value"] == "sonnet"

    def test_and_connector(self):
        tokens = _tokenize("model=sonnet AND tokens > 1M")
        assert len(tokens) == 3
        assert tokens[1] == {"type": "connector", "value": "AND"}

    def test_or_connector(self):
        tokens = _tokenize("project=alpha OR project=beta")
        assert tokens[1] == {"type": "connector", "value": "OR"}

    def test_connector_case_insensitive(self):
        tokens = _tokenize("model=sonnet and tokens > 1M")
        assert tokens[1]["value"] == "AND"

    def test_greater_than(self):
        tokens = _tokenize("cost > 0.50")
        assert tokens[0]["op"] == ">"
        assert tokens[0]["value"] == "0.50"

    def test_less_than(self):
        tokens = _tokenize("turns < 5")
        assert tokens[0]["op"] == "<"

    def test_gte(self):
        tokens = _tokenize("tokens >= 100K")
        assert tokens[0]["op"] == ">="

    def test_lte(self):
        tokens = _tokenize("cost <= 1.00")
        assert tokens[0]["op"] == "<="

    def test_not_equal(self):
        tokens = _tokenize("model != opus")
        assert tokens[0]["op"] == "!="

    def test_contains_operator(self):
        tokens = _tokenize("project ~ my-app")
        assert tokens[0]["op"] == "~"

    def test_double_quoted_value(self):
        tokens = _tokenize('project="my project"')
        assert tokens[0]["value"] == "my project"

    def test_single_quoted_value(self):
        tokens = _tokenize("project='my app'")
        assert tokens[0]["value"] == "my app"

    def test_empty_query(self):
        assert _tokenize("") == []

    def test_multiple_and_conditions(self):
        tokens = _tokenize("model=sonnet AND project=alpha AND tokens > 100K")
        conds = [t for t in tokens if t["type"] == "condition"]
        conns = [t for t in tokens if t["type"] == "connector"]
        assert len(conds) == 3
        assert len(conns) == 2
        assert all(c["value"] == "AND" for c in conns)


# ── _evaluate_condition ───────────────────────────────────────────────────────

class TestEvaluateCondition:
    @pytest.fixture
    def row(self):
        return {
            "total_input_tokens": 100_000,
            "total_output_tokens": 50_000,
            "total_cache_read": 10_000,
            "total_cache_creation": 5_000,
            "model": "claude-sonnet-4-6",
            "project_name": "my-project",
            "git_branch": "main",
            "session_id": "abc123def456",
            "last_timestamp": "2026-06-01T12:00:00",
            "first_timestamp": "2026-06-01T10:00:00",
            "turn_count": 20,
            "user_id": "default",
        }

    def test_model_equals_true(self, row):
        assert _evaluate_condition(row, {"field": "model", "op": "=", "value": "claude-sonnet-4-6"})

    def test_model_equals_false(self, row):
        assert not _evaluate_condition(row, {"field": "model", "op": "=", "value": "opus"})

    def test_model_equals_case_insensitive(self, row):
        assert _evaluate_condition(row, {"field": "model", "op": "=", "value": "CLAUDE-SONNET-4-6"})

    def test_model_not_equals(self, row):
        assert _evaluate_condition(row, {"field": "model", "op": "!=", "value": "opus"})

    def test_tokens_gt(self, row):
        # 100k + 50k = 150k
        assert _evaluate_condition(row, {"field": "tokens", "op": ">", "value": "100K"})
        assert not _evaluate_condition(row, {"field": "tokens", "op": ">", "value": "200K"})

    def test_tokens_lt(self, row):
        assert _evaluate_condition(row, {"field": "tokens", "op": "<", "value": "200K"})

    def test_tokens_gte(self, row):
        assert _evaluate_condition(row, {"field": "tokens", "op": ">=", "value": "150K"})

    def test_tokens_lte(self, row):
        assert _evaluate_condition(row, {"field": "tokens", "op": "<=", "value": "150K"})

    def test_input_tokens(self, row):
        assert _evaluate_condition(row, {"field": "input", "op": "=", "value": "100000"})

    def test_output_tokens(self, row):
        assert _evaluate_condition(row, {"field": "output", "op": ">", "value": "40K"})

    def test_cache_read(self, row):
        assert _evaluate_condition(row, {"field": "cache_read", "op": "=", "value": "10000"})

    def test_cache_creation(self, row):
        assert _evaluate_condition(row, {"field": "cache_creation", "op": "=", "value": "5000"})

    def test_turns_comparison(self, row):
        assert _evaluate_condition(row, {"field": "turns", "op": ">=", "value": "20"})
        assert not _evaluate_condition(row, {"field": "turns", "op": ">", "value": "20"})

    def test_project_exact(self, row):
        assert _evaluate_condition(row, {"field": "project", "op": "=", "value": "my-project"})

    def test_project_contains(self, row):
        assert _evaluate_condition(row, {"field": "project", "op": "~", "value": "project"})

    def test_branch(self, row):
        assert _evaluate_condition(row, {"field": "branch", "op": "=", "value": "main"})

    def test_date_equal(self, row):
        assert _evaluate_condition(row, {"field": "date", "op": "=", "value": "2026-06-01"})

    def test_date_gt(self, row):
        assert _evaluate_condition(row, {"field": "date", "op": ">", "value": "2026-05-01"})

    def test_user(self, row):
        assert _evaluate_condition(row, {"field": "user", "op": "=", "value": "default"})

    def test_m_suffix_numeric(self, row):
        assert _evaluate_condition(row, {"field": "tokens", "op": ">", "value": "0.1M"})

    def test_unknown_op_returns_false(self, row):
        assert not _evaluate_condition(row, {"field": "model", "op": "??", "value": "x"})


# ── _evaluate_with_connectors ─────────────────────────────────────────────────

class TestEvaluateWithConnectors:
    @pytest.fixture
    def row(self):
        return {
            "total_input_tokens": 100_000,
            "total_output_tokens": 50_000,
            "total_cache_read": 0,
            "total_cache_creation": 0,
            "model": "claude-sonnet-4-6",
            "project_name": "alpha",
            "git_branch": "main",
            "session_id": "xyz",
            "last_timestamp": "2026-06-01T10:00:00",
            "first_timestamp": "2026-06-01T09:00:00",
            "turn_count": 10,
            "user_id": "default",
        }

    def test_single_true(self, row):
        tokens = _tokenize("model=claude-sonnet-4-6")
        assert _evaluate_with_connectors(row, tokens) is True

    def test_single_false(self, row):
        tokens = _tokenize("model=opus")
        assert _evaluate_with_connectors(row, tokens) is False

    def test_and_both_true(self, row):
        tokens = _tokenize("model=claude-sonnet-4-6 AND project=alpha")
        assert _evaluate_with_connectors(row, tokens) is True

    def test_and_first_false(self, row):
        tokens = _tokenize("model=opus AND project=alpha")
        assert _evaluate_with_connectors(row, tokens) is False

    def test_and_second_false(self, row):
        tokens = _tokenize("model=claude-sonnet-4-6 AND project=beta")
        assert _evaluate_with_connectors(row, tokens) is False

    def test_or_first_true(self, row):
        tokens = _tokenize("model=claude-sonnet-4-6 OR project=beta")
        assert _evaluate_with_connectors(row, tokens) is True

    def test_or_second_true(self, row):
        tokens = _tokenize("model=opus OR project=alpha")
        assert _evaluate_with_connectors(row, tokens) is True

    def test_or_both_false(self, row):
        tokens = _tokenize("model=opus OR project=beta")
        assert _evaluate_with_connectors(row, tokens) is False

    def test_empty_returns_false(self, row):
        assert _evaluate_with_connectors(row, []) is False


# ── _compile_condition_sql ────────────────────────────────────────────────────

class TestCompileConditionSql:
    def test_model_equals(self):
        sql, params = _compile_condition_sql({"field": "model", "op": "=", "value": "sonnet"})
        assert "LOWER" in sql
        assert any("sonnet" in str(p) for p in params)

    def test_tokens_gt(self):
        sql, params = _compile_condition_sql({"field": "tokens", "op": ">", "value": "1M"})
        assert "total_input_tokens + total_output_tokens" in sql
        assert params == [1_000_000.0]

    def test_input_tokens(self):
        sql, params = _compile_condition_sql({"field": "input", "op": ">", "value": "50000"})
        assert "total_input_tokens" in sql
        assert params == [50_000.0]

    def test_output_tokens(self):
        sql, params = _compile_condition_sql({"field": "output", "op": "<", "value": "100K"})
        assert "total_output_tokens" in sql

    def test_turns(self):
        sql, params = _compile_condition_sql({"field": "turns", "op": ">=", "value": "10"})
        assert "turn_count" in sql

    def test_cache_read(self):
        sql, params = _compile_condition_sql({"field": "cache_read", "op": ">", "value": "1000"})
        assert "total_cache_read" in sql

    def test_project_like(self):
        sql, params = _compile_condition_sql({"field": "project", "op": "~", "value": "app"})
        assert "LIKE" in sql
        assert "%app%" in params

    def test_branch_equals(self):
        result = _compile_condition_sql({"field": "branch", "op": "=", "value": "main"})
        assert result is not None

    def test_date_field(self):
        result = _compile_condition_sql({"field": "date", "op": ">=", "value": "2026-01-01"})
        assert result is not None

    def test_user_field(self):
        result = _compile_condition_sql({"field": "user", "op": "=", "value": "default"})
        assert result is not None

    def test_numeric_tilde_returns_none(self):
        assert _compile_condition_sql({"field": "tokens", "op": "~", "value": "100K"}) is None

    def test_unsupported_field_returns_none(self):
        # "duration" is computed, not push-downable
        assert _compile_condition_sql({"field": "duration", "op": ">", "value": "60"}) is None

    def test_cost_field_returns_none(self):
        # cost is computed, not in DB directly
        assert _compile_condition_sql({"field": "cost", "op": ">", "value": "0.5"}) is None


# ── _build_sql_prefilter ──────────────────────────────────────────────────────

class TestBuildSqlPrefilter:
    def test_empty_returns_none(self):
        assert _build_sql_prefilter([]) is None

    def test_single_condition(self):
        tokens = _tokenize("model=sonnet")
        result = _build_sql_prefilter(tokens)
        assert result is not None
        sql, params = result
        assert len(params) >= 1

    def test_and_conditions(self):
        tokens = _tokenize("model=sonnet AND tokens > 100K")
        result = _build_sql_prefilter(tokens)
        assert result is not None
        sql, _ = result
        assert "AND" in sql

    def test_or_returns_none(self):
        tokens = _tokenize("model=sonnet OR model=opus")
        assert _build_sql_prefilter(tokens) is None

    def test_unsupported_field_returns_none(self):
        tokens = _tokenize("duration > 60")
        assert _build_sql_prefilter(tokens) is None

    def test_mixed_and_unsupported_returns_none(self):
        tokens = _tokenize("model=sonnet AND duration > 60")
        assert _build_sql_prefilter(tokens) is None


# ── execute_query (integration) ───────────────────────────────────────────────

class TestExecuteQuery:
    def test_no_db_returns_empty(self, tmp_path):
        assert execute_query("model=sonnet", tmp_path / "nonexistent.db") == []

    def test_empty_query_returns_empty(self, populated_db):
        assert execute_query("", populated_db) == []

    def test_model_filter(self, populated_db):
        results = execute_query("model=claude-sonnet-4-6", populated_db)
        assert len(results) == 1
        assert results[0]["session_id"] == "sess-aaa"

    def test_opus_model_filter(self, populated_db):
        results = execute_query("model=claude-opus-4-6", populated_db)
        assert len(results) == 1
        assert results[0]["session_id"] == "sess-bbb"

    def test_project_exact(self, populated_db):
        results = execute_query("project=proj/alpha", populated_db)
        assert len(results) == 2

    def test_tokens_threshold(self, populated_db):
        # sess-bbb: 200k+80k = 280k tokens
        results = execute_query("tokens > 250K", populated_db)
        assert len(results) == 1
        assert results[0]["session_id"] == "sess-bbb"

    def test_or_query(self, populated_db):
        results = execute_query("model=claude-sonnet-4-6 OR model=claude-haiku-4-5", populated_db)
        assert len(results) == 2

    def test_and_query(self, populated_db):
        results = execute_query("project=proj/alpha AND model=claude-sonnet-4-6", populated_db)
        assert len(results) == 1
        assert results[0]["session_id"] == "sess-aaa"

    def test_contains_operator(self, populated_db):
        results = execute_query("project ~ alpha", populated_db)
        assert len(results) == 2

    def test_branch_filter(self, populated_db):
        results = execute_query("branch=feat", populated_db)
        assert len(results) == 1
        assert results[0]["session_id"] == "sess-bbb"

    def test_result_has_computed_fields(self, populated_db):
        results = execute_query("model=claude-sonnet-4-6", populated_db)
        assert len(results) == 1
        r = results[0]
        assert "total_tokens" in r
        assert "est_cost" in r
        assert r["total_tokens"] == 150_000
        assert r["est_cost"] > 0

    def test_limit_respected(self, populated_db):
        results = execute_query("project ~ proj", populated_db, limit=2)
        assert len(results) <= 2

    def test_not_equal_operator(self, populated_db):
        results = execute_query("model != claude-opus-4-6", populated_db)
        assert all(r["model"] != "claude-opus-4-6" for r in results)

    def test_turns_filter(self, populated_db):
        # sess-aaa has 10 turns, sess-bbb has 15
        results = execute_query("turns > 12", populated_db)
        assert len(results) == 1
        assert results[0]["session_id"] == "sess-bbb"


# ── format_results ────────────────────────────────────────────────────────────

class TestFormatResults:
    def test_empty_results(self):
        out = format_results([])
        assert "No matching" in out

    def test_table_format(self):
        rows = [{"session_id": "abc123", "project_name": "proj", "model": "claude-sonnet-4-6",
                 "total_tokens": 100_000, "est_cost": 0.50, "turn_count": 5, "git_branch": "main"}]
        out = format_results(rows, fmt="table")
        assert "abc123" in out
        assert "proj" in out

    def test_json_format(self):
        import json
        rows = [{"session_id": "abc", "project_name": "p", "model": "m",
                 "total_tokens": 1000, "est_cost": 0.01, "turn_count": 1, "git_branch": "b"}]
        out = format_results(rows, fmt="json")
        parsed = json.loads(out)
        assert isinstance(parsed, list)
        assert parsed[0]["session_id"] == "abc"
