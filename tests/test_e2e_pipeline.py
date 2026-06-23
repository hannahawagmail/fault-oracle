# SPDX-License-Identifier: Apache-2.0
"""
test_e2e_pipeline.py — End-to-end pipeline test: generate → parse → validate.

Validates the full data path from synthetic log generation through parsing
to structured event output, ensuring all components integrate correctly.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
GENERATE_SCRIPT = REPO_ROOT / "tools" / "generate_edac_log.py"
PARSE_SCRIPT = REPO_ROOT / "replay" / "parse_edac_trace.py"

sys.path.insert(0, str(REPO_ROOT / "replay"))
from parse_edac_trace import HardwareEvent, TraceParser  # noqa: E402


class TestGenerateParseRoundtrip:
    """Generate synthetic logs, parse them, validate output structure."""

    def test_generated_log_parses_without_error(self, tmp_path):
        """generate_edac_log.py output must parse cleanly."""
        log_file = tmp_path / "synthetic.log"
        result = subprocess.run(
            ["python3", str(GENERATE_SCRIPT), "--events", "10", "--output", str(log_file)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"generate failed: {result.stderr}"
        assert log_file.stat().st_size > 0

        parser = TraceParser()
        with open(log_file) as f:
            events = parser.parse_file(f)
        assert len(events) == 10

    def test_all_generated_events_are_ce(self, tmp_path):
        """Generation with --ue-rate 0 produces only CE events."""
        log_file = tmp_path / "ce_only.log"
        subprocess.run(
            [
                "python3",
                str(GENERATE_SCRIPT),
                "--events",
                "5",
                "--ue-rate",
                "0",
                "--output",
                str(log_file),
            ],
            capture_output=True,
            check=True,
        )
        parser = TraceParser()
        with open(log_file) as f:
            events = parser.parse_file(f)
        for ev in events:
            assert ev.event_type == "CE"
            assert ev.subsystem == "EDAC"

    def test_parsed_events_have_required_fields(self, tmp_path):
        """Each parsed event must have all required fields populated."""
        log_file = tmp_path / "fields.log"
        subprocess.run(
            ["python3", str(GENERATE_SCRIPT), "--events", "5", "--output", str(log_file)],
            capture_output=True,
            check=True,
        )
        parser = TraceParser()
        with open(log_file) as f:
            events = parser.parse_file(f)
        for ev in events:
            assert ev.controller is not None
            assert ev.count >= 1
            assert ev.raw_message

    def test_json_output_is_valid(self, tmp_path):
        """CLI JSON output must be valid and deserializable."""
        log_file = tmp_path / "json_test.log"
        json_file = tmp_path / "events.json"
        subprocess.run(
            ["python3", str(GENERATE_SCRIPT), "--events", "3", "--output", str(log_file)],
            capture_output=True,
            check=True,
        )
        result = subprocess.run(
            [
                "python3",
                str(PARSE_SCRIPT),
                "--input",
                str(log_file),
                "--output",
                str(json_file),
                "--no-stats",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"parse failed: {result.stderr}"
        data = json.loads(json_file.read_text())
        assert isinstance(data, list)
        assert len(data) == 3
        for item in data:
            assert "event_type" in item
            assert "seq" in item

    def test_sequence_numbers_contiguous(self, tmp_path):
        """Parsed events must have contiguous seq from 0."""
        log_file = tmp_path / "seq.log"
        subprocess.run(
            ["python3", str(GENERATE_SCRIPT), "--events", "7", "--output", str(log_file)],
            capture_output=True,
            check=True,
        )
        parser = TraceParser()
        with open(log_file) as f:
            events = parser.parse_file(f)
        seqs = [ev.seq for ev in events]
        assert seqs == list(range(7))

    def test_multiple_controllers(self, tmp_path):
        """Multi-controller generation produces events from different MCs."""
        log_file = tmp_path / "multi_mc.log"
        subprocess.run(
            [
                "python3",
                str(GENERATE_SCRIPT),
                "--events",
                "20",
                "--controllers",
                "3",
                "--output",
                str(log_file),
            ],
            capture_output=True,
            check=True,
        )
        parser = TraceParser()
        with open(log_file) as f:
            events = parser.parse_file(f)
        controllers = {ev.controller for ev in events}
        # With 20 events and 3 controllers, at least 2 should appear
        assert len(controllers) >= 2, f"Only got controllers: {controllers}"
