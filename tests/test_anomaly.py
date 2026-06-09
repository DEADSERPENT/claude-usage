import math
import sqlite3
from datetime import date, timedelta

import pytest

from anomaly import _mean_stddev, detect_anomalies, get_recent_anomalies, acknowledge_anomaly


# ── _mean_stddev ──────────────────────────────────────────────────────────────

class TestMeanStddev:
    def test_empty(self):
        mean, std = _mean_stddev([])
        assert mean == 0.0
        assert std == 0.0

    def test_single_value(self):
        mean, std = _mean_stddev([42.0])
        assert mean == 42.0
        assert std == 0.0

    def test_identical_values(self):
        mean, std = _mean_stddev([5.0, 5.0, 5.0])
        assert mean == pytest.approx(5.0)
        assert std == pytest.approx(0.0)

    def test_known_values(self):
        # [2, 4, 4, 4, 5, 5, 7, 9]: mean=5, sample std=sqrt(32/7)≈2.138
        import math
        values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        mean, std = _mean_stddev(values)
        assert mean == pytest.approx(5.0)
        assert std == pytest.approx(math.sqrt(32 / 7))

    def test_two_values(self):
        mean, std = _mean_stddev([0.0, 10.0])
        assert mean == pytest.approx(5.0)
        # sample variance = ((0-5)^2 + (10-5)^2) / (2-1) = 50; std = sqrt(50)
        assert std == pytest.approx(math.sqrt(50))

    def test_result_is_non_negative_std(self):
        for values in ([1.0], [1.0, 2.0], [1.0, 2.0, 3.0]):
            _, std = _mean_stddev(values)
            assert std >= 0.0


# ── detect_anomalies ──────────────────────────────────────────────────────────

def _insert_day_tokens(conn, day_str: str, session_id: str, tokens: int):
    conn.execute(
        "INSERT INTO turns (session_id, timestamp, model, input_tokens, output_tokens, "
        "cache_read_tokens, cache_creation_tokens, tool_name, cwd, user_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (session_id, f"{day_str}T10:00:00", "claude-sonnet-4-6",
         tokens // 2, tokens - tokens // 2, 0, 0, None, "/proj", "default"),
    )


def _baseline_and_spike(db_path, baseline_tokens: int, spike_tokens: int,
                        baseline_days: int = 7):
    """Insert `baseline_days` of stable usage + a spike today."""
    conn = sqlite3.connect(db_path)
    today = date.today()
    for i in range(1, baseline_days + 1):
        day = (today - timedelta(days=i)).isoformat()
        _insert_day_tokens(conn, day, f"base-{i}", baseline_tokens)
    _insert_day_tokens(conn, today.isoformat(), "spike-today", spike_tokens)
    conn.commit()
    conn.close()


class TestDetectAnomalies:
    def test_no_db_returns_empty(self, tmp_path):
        assert detect_anomalies(tmp_path / "nonexistent.db") == []

    def test_empty_db_returns_empty(self, db_path):
        assert detect_anomalies(db_path) == []

    def test_stable_usage_no_anomaly(self, db_path):
        _baseline_and_spike(db_path, baseline_tokens=10_000, spike_tokens=10_000)
        result = detect_anomalies(db_path, window_days=7, spike_factor=3.0)
        token_anomalies = [a for a in result if a["metric"] == "daily_tokens"]
        assert token_anomalies == []

    def test_large_spike_detected(self, db_path):
        # 10x spike over baseline of 10k → 100k today
        _baseline_and_spike(db_path, baseline_tokens=10_000, spike_tokens=100_000)
        result = detect_anomalies(db_path, window_days=7, spike_factor=2.0)
        metrics = [a["metric"] for a in result]
        assert "daily_tokens" in metrics

    def test_moderate_spike_below_factor_not_detected(self, db_path):
        # 1.5x spike, factor=3.0 → should NOT trigger
        _baseline_and_spike(db_path, baseline_tokens=10_000, spike_tokens=15_000)
        result = detect_anomalies(db_path, window_days=7, spike_factor=3.0)
        token_anomalies = [a for a in result if a["metric"] == "daily_tokens"]
        assert token_anomalies == []

    def test_anomaly_fields_present(self, db_path):
        _baseline_and_spike(db_path, baseline_tokens=10_000, spike_tokens=100_000)
        result = detect_anomalies(db_path, window_days=7, spike_factor=2.0)
        required = {"metric", "value", "baseline", "factor", "severity", "message"}
        for a in result:
            assert required.issubset(a.keys()), f"Missing keys in anomaly: {a}"

    def test_anomaly_severity_values(self, db_path):
        _baseline_and_spike(db_path, baseline_tokens=10_000, spike_tokens=100_000)
        result = detect_anomalies(db_path, window_days=7, spike_factor=2.0)
        valid_severities = {"info", "warning", "critical"}
        for a in result:
            assert a["severity"] in valid_severities

    def test_critical_severity_for_extreme_spike(self, db_path):
        # 20x spike with factor=2.0 → factor > spike_factor*2 → critical
        _baseline_and_spike(db_path, baseline_tokens=10_000, spike_tokens=200_000)
        result = detect_anomalies(db_path, window_days=7, spike_factor=2.0)
        token_anomalies = [a for a in result if a["metric"] == "daily_tokens"]
        assert len(token_anomalies) >= 1
        assert token_anomalies[0]["severity"] == "critical"

    def test_anomalies_stored_in_db(self, db_path):
        _baseline_and_spike(db_path, baseline_tokens=10_000, spike_tokens=100_000)
        result = detect_anomalies(db_path, window_days=7, spike_factor=2.0)
        if result:
            stored = get_recent_anomalies(db_path, days=1)
            assert len(stored) >= 1

    def test_factor_gt_one_for_spike(self, db_path):
        _baseline_and_spike(db_path, baseline_tokens=10_000, spike_tokens=100_000)
        result = detect_anomalies(db_path, window_days=7, spike_factor=2.0)
        token_anomalies = [a for a in result if a["metric"] == "daily_tokens"]
        assert len(token_anomalies) == 1
        assert token_anomalies[0]["factor"] > 1.0


# ── get_recent_anomalies ──────────────────────────────────────────────────────

class TestGetRecentAnomalies:
    def test_no_db(self, tmp_path):
        assert get_recent_anomalies(tmp_path / "nonexistent.db") == []

    def test_empty_db(self, db_path):
        assert get_recent_anomalies(db_path) == []

    def test_returns_stored_anomaly(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO anomalies (metric, value, baseline, factor, severity, message) "
            "VALUES (?,?,?,?,?,?)",
            ("daily_tokens", 100_000, 10_000, 10.0, "critical", "Big spike!"),
        )
        conn.commit()
        conn.close()

        result = get_recent_anomalies(db_path, days=7)
        assert len(result) == 1
        assert result[0]["metric"] == "daily_tokens"

    def test_limit_respected(self, db_path):
        conn = sqlite3.connect(db_path)
        for i in range(10):
            conn.execute(
                "INSERT INTO anomalies (metric, value, baseline, factor, severity, message) "
                "VALUES (?,?,?,?,?,?)",
                (f"metric_{i}", float(i), 1.0, float(i), "warning", f"msg {i}"),
            )
        conn.commit()
        conn.close()

        result = get_recent_anomalies(db_path, days=7, limit=3)
        assert len(result) <= 3

    def test_old_anomalies_excluded(self, db_path):
        conn = sqlite3.connect(db_path)
        # Insert anomaly with very old detected_at
        conn.execute(
            "INSERT INTO anomalies (detected_at, metric, value, baseline, factor, severity, message) "
            "VALUES (?,?,?,?,?,?,?)",
            ("2000-01-01T00:00:00", "daily_tokens", 1, 0, 1.0, "warning", "old"),
        )
        conn.commit()
        conn.close()

        result = get_recent_anomalies(db_path, days=7)
        assert result == []


# ── acknowledge_anomaly ───────────────────────────────────────────────────────

class TestAcknowledgeAnomaly:
    def test_acknowledge_existing(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO anomalies (id, metric, value, baseline, factor, severity, message) "
            "VALUES (1, 'daily_tokens', 100, 10, 10.0, 'warning', 'test')",
        )
        conn.commit()
        conn.close()

        assert acknowledge_anomaly(db_path, 1) is True
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT acknowledged FROM anomalies WHERE id=1").fetchone()
        conn.close()
        assert row[0] == 1

    def test_acknowledge_nonexistent_returns_false(self, db_path):
        assert acknowledge_anomaly(db_path, 9999) is False
