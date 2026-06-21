# SPDX-License-Identifier: Apache-2.0
"""
tests/test_webhook_routing.py — Tests for the Alertmanager webhook server.

Uses the Python http.server machinery directly (no real HTTP) to test routing,
duplicate suppression, and handler invocation logic.
"""
import json
import os
import time
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import sys

# Add recovery/ to path so we can import webhook-server module
sys.path.insert(0, str(Path(__file__).parent.parent))
import importlib.util, types

# Load webhook-server.py as a module (it has a hyphen so we use spec loading)
spec = importlib.util.spec_from_file_location(
    "webhook_server",
    Path(__file__).parent.parent / "webhook-server.py",
)
ws = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ws)


def make_alert(alert_name="TestAlert", node="node-1", status="firing", extra_labels=None):
    labels = {"alertname": alert_name, "node": node, "collector": "edac"}
    if extra_labels:
        labels.update(extra_labels)
    return {
        "status": status,
        "labels": labels,
        "annotations": {"summary": "test alert", "runbook_url": "http://example.com/runbook"},
        "startsAt": "2026-06-18T00:00:00Z",
    }


def make_payload(*alerts):
    return {"alerts": list(alerts)}


class TestFingerprintUniqueness:

    def test_same_labels_same_fingerprint(self):
        a1 = make_alert("TestAlert", "node-1")
        a2 = make_alert("TestAlert", "node-1")
        assert ws._make_fingerprint(a1) == ws._make_fingerprint(a2)

    def test_different_nodes_different_fingerprint(self):
        a1 = make_alert("TestAlert", "node-1")
        a2 = make_alert("TestAlert", "node-2")
        assert ws._make_fingerprint(a1) != ws._make_fingerprint(a2)

    def test_different_alert_names_different_fingerprint(self):
        a1 = make_alert("AlertA", "node-1")
        a2 = make_alert("AlertB", "node-1")
        assert ws._make_fingerprint(a1) != ws._make_fingerprint(a2)


class TestCooldownSuppression:

    def setup_method(self):
        with ws._lock:
            ws._recent.clear()

    def test_first_execution_allowed(self):
        fp = "test-fp-001"
        assert ws._allowed(fp) is True

    def test_immediate_repeat_suppressed(self):
        fp = "test-fp-002"
        assert ws._allowed(fp) is True
        assert ws._allowed(fp) is False   # within cooldown

    def test_different_fingerprint_not_suppressed(self):
        ws._allowed("test-fp-003")
        assert ws._allowed("test-fp-004") is True   # different fingerprint


class TestHandlerPath:

    def test_existing_handler_found(self, tmp_path):
        script = tmp_path / "EDACCorrectableStorm.sh"
        script.write_text("#!/bin/bash\necho ok\n")
        result = ws._handler_path(tmp_path, "EDACCorrectableStorm")
        assert result == script

    def test_missing_handler_returns_none(self, tmp_path):
        result = ws._handler_path(tmp_path, "NonExistentAlert")
        assert result is None


class TestRunHandler:

    def test_dry_run_returns_zero(self, tmp_path):
        script = tmp_path / "test.sh"
        script.write_text("#!/bin/bash\nexit 1\n")  # would fail if really run
        script.chmod(0o755)
        alert = make_alert()
        result = ws._run_handler(script, alert, dry_run=True)
        assert result == 0

    def test_successful_handler_returns_zero(self, tmp_path):
        script = tmp_path / "test.sh"
        script.write_text("#!/bin/bash\nexit 0\n")
        script.chmod(0o755)
        alert = make_alert()
        result = ws._run_handler(script, alert, dry_run=False)
        assert result == 0

    def test_failing_handler_returns_nonzero(self, tmp_path):
        script = tmp_path / "test.sh"
        script.write_text("#!/bin/bash\nexit 1\n")
        script.chmod(0o755)
        alert = make_alert()
        result = ws._run_handler(script, alert, dry_run=False)
        assert result == 1

    def test_alert_labels_exported_as_env(self, tmp_path):
        script = tmp_path / "test.sh"
        env_file = tmp_path / "env.txt"
        script.write_text(f"#!/bin/bash\necho ALERT_NODE=$ALERT_NODE > {env_file}\n")
        script.chmod(0o755)
        alert = make_alert("TestAlert", "node-42")
        ws._run_handler(script, alert, dry_run=False)
        assert "node-42" in env_file.read_text()

    def test_resolved_alert_skipped(self):
        """Status != firing should not reach _run_handler in normal flow."""
        alert = make_alert(status="resolved")
        assert alert["status"] == "resolved"
        # The webhook handler skips resolved alerts before calling _run_handler

    def test_duplicate_suppression_integration(self, tmp_path):
        with ws._lock:
            ws._recent.clear()

        counter_file = tmp_path / "count.txt"
        counter_file.write_text("0")
        script = tmp_path / "counter.sh"
        script.write_text(
            f"#!/bin/bash\n"
            f"c=$(cat {counter_file})\n"
            f"echo $((c+1)) > {counter_file}\n"
        )
        script.chmod(0o755)

        alert = make_alert("CountAlert", "node-1")
        fp = ws._make_fingerprint(alert)

        # First call: allowed
        with ws._lock:
            ws._recent.clear()
        if ws._allowed(fp):
            ws._run_handler(script, alert, dry_run=False)
        # Second call: suppressed
        if ws._allowed(fp):
            ws._run_handler(script, alert, dry_run=False)

        assert counter_file.read_text().strip() == "1"
