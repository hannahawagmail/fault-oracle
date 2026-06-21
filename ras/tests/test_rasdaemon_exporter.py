# SPDX-License-Identifier: Apache-2.0
"""Tests for ras/rasdaemon-exporter.py."""
import sqlite3
import sys
import time
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from rasdaemon_exporter import (
    query_mc_events, query_aer_events, emit_metrics, sanitize_label
)


def make_mc_db(path: Path, rows: list) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE mc_event (
            id INTEGER PRIMARY KEY,
            timestamp REAL,
            error_type TEXT,
            mc TEXT,
            top_layer TEXT,
            mid_layer TEXT
        )
    """)
    conn.executemany(
        "INSERT INTO mc_event (timestamp, error_type, mc, top_layer, mid_layer) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return conn


class TestQueryMCEvents:

    def test_basic_count(self, tmp_path):
        now = time.time()
        conn = make_mc_db(tmp_path / "db.db", [
            (now, "CE", "0", "0", "0"),
            (now, "CE", "0", "0", "0"),
            (now, "UE", "0", "1", "0"),
        ])
        counts = query_mc_events(conn, 0)
        assert counts[("CE", "0", "0", "0")] == 2
        assert counts[("UE", "0", "1", "0")] == 1

    def test_since_filter(self, tmp_path):
        now = time.time()
        conn = make_mc_db(tmp_path / "db.db", [
            (now - 7200, "CE", "0", "0", "0"),   # 2 hours ago
            (now - 100,  "CE", "1", "0", "0"),   # recent
        ])
        counts = query_mc_events(conn, now - 3600)  # last 1 hour
        assert ("CE", "1", "0", "0") in counts
        assert ("CE", "0", "0", "0") not in counts

    def test_empty_db(self, tmp_path):
        conn = make_mc_db(tmp_path / "db.db", [])
        assert query_mc_events(conn, 0) == {}

    def test_multiple_mcs(self, tmp_path):
        now = time.time()
        conn = make_mc_db(tmp_path / "db.db", [
            (now, "CE", "0", "0", "0"),
            (now, "CE", "1", "0", "0"),
            (now, "CE", "2", "0", "0"),
        ])
        counts = query_mc_events(conn, 0)
        assert len(counts) == 3

    def test_label_sanitization(self, tmp_path):
        now = time.time()
        conn = make_mc_db(tmp_path / "db.db", [
            (now, 'bad"quote', "0", "layer\nwith\nnewlines", "0"),
        ])
        counts = query_mc_events(conn, 0)
        for key in counts:
            for part in key:
                assert '"' not in part
                assert '\n' not in part


class TestQueryAEREvents:

    def test_missing_table_returns_empty(self, tmp_path):
        db = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
        assert query_aer_events(conn, 0) == {}


class TestEmitMetrics:

    def test_output_is_valid_prometheus_text(self):
        mc = {("CE", "0", "0", "0"): 5}
        aer = {("correctable", "0000:01:00.0", "BadTLP"): 2}
        output = emit_metrics(mc, aer, 10)
        assert "# HELP ras_mc_event_total" in output
        assert "# TYPE ras_mc_event_total counter" in output
        assert 'ras_mc_event_total{type="CE"' in output
        assert 'ras_aer_event_total{severity="correctable"' in output

    def test_empty_counts_still_valid(self):
        output = emit_metrics({}, {}, 0)
        assert "ras_exporter_last_run_timestamp" in output
        assert "ras_exporter_db_events_total 0" in output

    def test_total_events_in_output(self):
        output = emit_metrics({}, {}, 42)
        assert "ras_exporter_db_events_total 42" in output


class TestSanitizeLabel:
    def test_removes_double_quotes(self):
        assert '"' not in sanitize_label('bad"value')

    def test_removes_newlines(self):
        assert '\n' not in sanitize_label("line1\nline2")

    def test_empty_becomes_unknown(self):
        assert sanitize_label("") == "unknown"
        assert sanitize_label("   ") == "unknown"
