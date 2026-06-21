#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the remediation controller and webhook server.

Structure
---------
TestPolicyEvaluation  (6) — poll_once / query_prometheus / threshold logic
TestCordonLogic       (6) — build_cordon_patch, build_taint_patch, build_event,
                            remediate_node, state updates
TestWebhookServer     (6) — handle_replacement_confirmed / handle_false_positive /
                            HTTP response codes
TestMetrics           (6) — counter/gauge accumulation across polls
"""

import importlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup — make both modules importable without installing them
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_REMEDIATION_DIR = _REPO_ROOT / "remediation"

# Insert remediation dir so "import remediation_controller" works even though
# the file is named with a hyphen (we import via importlib below).
if str(_REMEDIATION_DIR) not in sys.path:
    sys.path.insert(0, str(_REMEDIATION_DIR))

# The controller file uses hyphens; load it via importlib.util.
import importlib.util as _ilu

def _load_hyphen_module(name: str, path: Path):
    spec = _ilu.spec_from_file_location(name, path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ctrl = _load_hyphen_module(
    "remediation_controller",
    _REMEDIATION_DIR / "remediation-controller.py",
)
_wh = _load_hyphen_module(
    "webhook_server",
    _REMEDIATION_DIR / "webhook-server.py",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _prometheus_response(results: list[dict]) -> dict:
    """Build a minimal Prometheus instant-query response."""
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": results,
        },
    }


def _prom_item(node: str, value: float) -> dict:
    return {
        "metric": {"node": node},
        "value": [time.time(), str(value)],
    }


def _make_kube_mock() -> MagicMock:
    kube = MagicMock()
    kube.patch_node.return_value = {}
    kube.create_event.return_value = {}
    return kube


def _reset_counters(module) -> None:
    """Zero out the module-level metric dicts between tests."""
    with module._metrics_lock:
        for d in module._counters.values():
            d.clear()
    if hasattr(module, "_gauges"):
        with module._metrics_lock:
            for name, series in module._gauges.items():
                if name == "remediation_nodes_at_risk":
                    series.clear()
                    series[()] = 0.0
                elif name == "remediation_controller_last_poll_timestamp":
                    series.clear()
                    series[()] = 0.0
                else:
                    series.clear()


# ---------------------------------------------------------------------------
# TestPolicyEvaluation
# ---------------------------------------------------------------------------

class TestPolicyEvaluation(unittest.TestCase):
    """Tests around the poll_once / threshold decision logic."""

    def setUp(self):
        _reset_counters(_ctrl)
        # Patch DRY_RUN to True so no real k8s calls happen unless we test
        # the live path explicitly.
        self._dry_run_patcher = patch.object(_ctrl, "DRY_RUN", True)
        self._dry_run_patcher.start()

    def tearDown(self):
        self._dry_run_patcher.stop()

    # 1. Threshold breach triggers a cordon action
    def test_threshold_breach_triggers_cordon(self):
        prom_data = _prometheus_response([_prom_item("node-a", 0.95)])
        kube = _make_kube_mock()

        with patch.object(_ctrl, "query_prometheus", return_value=[("node-a", 0.95)]), \
             patch.object(_ctrl, "_load_state", return_value={}), \
             patch.object(_ctrl, "_save_state"):
            _ctrl.poll_once(kube)

        # In dry-run mode the counter is incremented for "node-a"
        key = tuple(sorted({"action": "cordon", "node": "node-a", "dry_run": "true"}.items()))
        self.assertEqual(_ctrl._counters["remediation_actions_total"].get(key, 0), 1)

    # 2. Below threshold → no action taken
    def test_below_threshold_no_op(self):
        kube = _make_kube_mock()

        with patch.object(_ctrl, "query_prometheus", return_value=[("node-b", 0.50)]), \
             patch.object(_ctrl, "_load_state", return_value={}), \
             patch.object(_ctrl, "_save_state"):
            _ctrl.poll_once(kube)

        total = sum(_ctrl._counters["remediation_actions_total"].values())
        self.assertEqual(total, 0)

    # 3. Cooldown blocks repeat action within 4 h
    def test_cooldown_blocks_repeat_action(self):
        recent_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        state = {"node-c": {"last_action": recent_ts, "probability": 0.9, "dry_run": True}}
        kube = _make_kube_mock()

        with patch.object(_ctrl, "query_prometheus", return_value=[("node-c", 0.90)]), \
             patch.object(_ctrl, "_load_state", return_value=state), \
             patch.object(_ctrl, "_save_state"):
            _ctrl.poll_once(kube)

        total = sum(_ctrl._counters["remediation_actions_total"].values())
        self.assertEqual(total, 0)

    # 4. dry_run=True skips HTTP patch but still logs / increments counter
    def test_dry_run_skips_patch_increments_counter(self):
        kube = _make_kube_mock()

        with patch.object(_ctrl, "query_prometheus", return_value=[("node-d", 0.99)]), \
             patch.object(_ctrl, "_load_state", return_value={}), \
             patch.object(_ctrl, "_save_state"):
            _ctrl.poll_once(kube)

        kube.patch_node.assert_not_called()
        key = tuple(sorted({"action": "cordon", "node": "node-d", "dry_run": "true"}.items()))
        self.assertGreater(_ctrl._counters["remediation_actions_total"].get(key, 0), 0)

    # 5. Two nodes above threshold are both processed
    def test_two_nodes_above_threshold_both_processed(self):
        kube = _make_kube_mock()

        with patch.object(_ctrl, "query_prometheus",
                          return_value=[("node-e", 0.85), ("node-f", 0.92)]), \
             patch.object(_ctrl, "_load_state", return_value={}), \
             patch.object(_ctrl, "_save_state"):
            _ctrl.poll_once(kube)

        total = sum(_ctrl._counters["remediation_actions_total"].values())
        self.assertEqual(total, 2)

    # 6. Empty Prometheus response → no-op
    def test_empty_prometheus_response_no_op(self):
        kube = _make_kube_mock()

        with patch.object(_ctrl, "query_prometheus", return_value=[]), \
             patch.object(_ctrl, "_load_state", return_value={}), \
             patch.object(_ctrl, "_save_state"):
            _ctrl.poll_once(kube)

        total = sum(_ctrl._counters["remediation_actions_total"].values())
        self.assertEqual(total, 0)


# ---------------------------------------------------------------------------
# TestCordonLogic
# ---------------------------------------------------------------------------

class TestCordonLogic(unittest.TestCase):
    """Tests around the cordon/taint/event construction and state updates."""

    def setUp(self):
        _reset_counters(_ctrl)

    # 1. build_cordon_patch sets spec.unschedulable=True
    def test_cordon_patch_unschedulable_true(self):
        patch_doc = _ctrl.build_cordon_patch()
        self.assertTrue(patch_doc["spec"]["unschedulable"])

    # 2. Taint key is fault-resilience/predicted-failure
    def test_taint_patch_key(self):
        patch_doc = _ctrl.build_taint_patch(0.8)
        taint = patch_doc["spec"]["taints"][0]
        self.assertEqual(taint["key"], "fault-resilience/predicted-failure")

    # 3. Event POST includes probability value in the message
    def test_event_includes_probability(self):
        event = _ctrl.build_event("node-g", 0.87, dry_run=False)
        self.assertIn("0.870", event["message"])

    # 4. dry_run skips HTTP patch_node call
    def test_dry_run_skips_http_patch(self):
        kube = _make_kube_mock()
        state: dict = {}

        with patch.object(_ctrl, "DRY_RUN", True), \
             patch.object(_ctrl, "push_loki"):
            _ctrl.remediate_node(kube, "node-h", 0.88, state)

        kube.patch_node.assert_not_called()

    # 5. state.json written after action (dry-run updates state too)
    def test_state_written_after_action(self):
        kube = _make_kube_mock()
        state: dict = {}

        with patch.object(_ctrl, "DRY_RUN", True), \
             patch.object(_ctrl, "push_loki"):
            _ctrl.remediate_node(kube, "node-i", 0.91, state)

        self.assertIn("node-i", state)
        self.assertIn("last_action", state["node-i"])

    # 6. Cooldown timestamp updated in state after action
    def test_cooldown_timestamp_updated_in_state(self):
        kube = _make_kube_mock()
        state: dict = {}
        before = datetime.now(timezone.utc)

        with patch.object(_ctrl, "DRY_RUN", True), \
             patch.object(_ctrl, "push_loki"):
            _ctrl.remediate_node(kube, "node-j", 0.82, state)

        ts_str = state["node-j"]["last_action"]
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        self.assertGreaterEqual(ts, before)


# ---------------------------------------------------------------------------
# TestWebhookServer
# ---------------------------------------------------------------------------

class TestWebhookServer(unittest.TestCase):
    """Tests for webhook-server.py business logic and HTTP responses."""

    def setUp(self):
        _reset_counters(_wh)
        self._tmp = tempfile.mkdtemp()
        self._state_file = os.path.join(self._tmp, "state.json")
        self._fp_file = os.path.join(self._tmp, "false-positives.jsonl")
        self._retrain_file = os.path.join(self._tmp, "retrain-needed")

        # Patch module-level paths so tests don't touch /var/lib
        self._p1 = patch.object(_wh, "STATE_FILE", self._state_file)
        self._p2 = patch.object(_wh, "FALSE_POSITIVES_FILE", self._fp_file)
        self._p3 = patch.object(_wh, "RETRAIN_FILE", self._retrain_file)
        self._p1.start(); self._p2.start(); self._p3.start()

    def tearDown(self):
        self._p1.stop(); self._p2.stop(); self._p3.stop()

    def _write_state(self, data: dict) -> None:
        with open(self._state_file, "w") as f:
            json.dump(data, f)

    def _read_state(self) -> dict:
        with open(self._state_file) as f:
            return json.load(f)

    # 1. replacement-confirmed removes node from state
    def test_replacement_confirmed_removes_node_from_state(self):
        self._write_state({"node-k": {"last_action": "2026-06-20T00:00:00+00:00"}})
        _wh.handle_replacement_confirmed("node-k", "dimm")
        state = self._read_state()
        self.assertNotIn("node-k", state)

    # 2. false-positive increments counter
    def test_false_positive_increments_counter(self):
        before = _wh.get_counter("false_positive_total", {"alertname": "EDACHighCERate"})
        _wh.handle_false_positive("EDACHighCERate", "node-l")
        after = _wh.get_counter("false_positive_total", {"alertname": "EDACHighCERate"})
        self.assertEqual(after, before + 1)

    # 3. replacement-confirmed touches retrain-needed file
    def test_replacement_confirmed_touches_retrain_file(self):
        self._write_state({})
        _wh.handle_replacement_confirmed("node-m", "gpu")
        self.assertTrue(os.path.exists(self._retrain_file))

    # 4. /healthz returns 200 OK
    def test_healthz_returns_200(self):
        from http.server import HTTPServer
        import threading

        server = HTTPServer(("127.0.0.1", 0), _wh.WebhookHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.handle_request)
        t.daemon = True
        t.start()

        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"OK")

        server.server_close()

    # 5. /metrics returns prometheus text with webhook_server_up
    def test_metrics_returns_prometheus_text(self):
        from http.server import HTTPServer
        import threading

        server = HTTPServer(("127.0.0.1", 0), _wh.WebhookHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.handle_request)
        t.daemon = True
        t.start()

        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics") as resp:
            self.assertEqual(resp.status, 200)
            body = resp.read().decode()
        self.assertIn("webhook_server_up 1", body)

        server.server_close()

    # 6. Unknown endpoint returns 404
    def test_unknown_endpoint_returns_404(self):
        from http.server import HTTPServer
        import threading
        import urllib.error

        server = HTTPServer(("127.0.0.1", 0), _wh.WebhookHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.handle_request)
        t.daemon = True
        t.start()

        import urllib.request
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nonexistent")
            self.fail("Expected HTTPError 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

        server.server_close()


# ---------------------------------------------------------------------------
# TestMetrics
# ---------------------------------------------------------------------------

class TestMetrics(unittest.TestCase):
    """Tests around metric accumulation in the controller."""

    def setUp(self):
        _reset_counters(_ctrl)
        self._dry_run_patcher = patch.object(_ctrl, "DRY_RUN", True)
        self._dry_run_patcher.start()

    def tearDown(self):
        self._dry_run_patcher.stop()

    # 1. remediation_actions_total increments on cordon
    def test_remediation_actions_total_increments_on_cordon(self):
        kube = _make_kube_mock()

        with patch.object(_ctrl, "query_prometheus", return_value=[("node-n", 0.95)]), \
             patch.object(_ctrl, "_load_state", return_value={}), \
             patch.object(_ctrl, "_save_state"):
            _ctrl.poll_once(kube)

        total = sum(_ctrl._counters["remediation_actions_total"].values())
        self.assertEqual(total, 1)

    # 2. cooldown_active gauge reflects state
    def test_cooldown_active_gauge_reflects_state(self):
        recent_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        state = {"node-o": {"last_action": recent_ts, "probability": 0.9, "dry_run": True}}
        kube = _make_kube_mock()

        with patch.object(_ctrl, "query_prometheus", return_value=[("node-o", 0.90)]), \
             patch.object(_ctrl, "_load_state", return_value=state), \
             patch.object(_ctrl, "_save_state"):
            _ctrl.poll_once(kube)

        key = (("node", "node-o"),)
        self.assertEqual(_ctrl._gauges["remediation_cooldown_active"].get(key, 0.0), 1.0)

    # 3. nodes_at_risk gauge equals count above threshold
    def test_nodes_at_risk_gauge_count(self):
        kube = _make_kube_mock()

        with patch.object(_ctrl, "query_prometheus",
                          return_value=[("node-p", 0.85), ("node-q", 0.92), ("node-r", 0.50)]), \
             patch.object(_ctrl, "_load_state", return_value={}), \
             patch.object(_ctrl, "_save_state"):
            _ctrl.poll_once(kube)

        self.assertEqual(_ctrl._gauges["remediation_nodes_at_risk"][()], 2.0)

    # 4. poll_errors_total increments when Prometheus is unreachable
    def test_poll_errors_total_increments_on_prometheus_unreachable(self):
        kube = _make_kube_mock()

        with patch.object(_ctrl, "query_prometheus",
                          side_effect=RuntimeError("connection refused")):
            _ctrl.poll_once(kube)

        total = sum(_ctrl._counters["remediation_poll_errors_total"].values())
        self.assertEqual(total, 1)

    # 5. dry_run label appears in metrics output
    def test_dry_run_label_in_metrics(self):
        kube = _make_kube_mock()

        with patch.object(_ctrl, "query_prometheus", return_value=[("node-s", 0.88)]), \
             patch.object(_ctrl, "_load_state", return_value={}), \
             patch.object(_ctrl, "_save_state"):
            _ctrl.poll_once(kube)

        metrics_text = _ctrl.render_metrics()
        self.assertIn('dry_run="true"', metrics_text)

    # 6. Multiple polls accumulate counter correctly
    def test_multiple_polls_accumulate_counter(self):
        kube = _make_kube_mock()

        for _ in range(3):
            _reset_counters(_ctrl)  # reset between polls to test single-poll accumulation

        # Now run 3 polls in sequence without resetting
        _reset_counters(_ctrl)
        for _ in range(3):
            with patch.object(_ctrl, "query_prometheus", return_value=[("node-t", 0.91)]), \
                 patch.object(_ctrl, "_load_state", return_value={}), \
                 patch.object(_ctrl, "_save_state"):
                _ctrl.poll_once(kube)

        total = sum(_ctrl._counters["remediation_actions_total"].values())
        self.assertEqual(total, 3)


# ---------------------------------------------------------------------------
# TestWebhookServerHTTP
# ---------------------------------------------------------------------------

def _make_handler(wh_module, path: str, body: bytes = b"{}"):
    """
    Create a WebhookHandler instance with a mocked socket/server so we can
    call do_POST / do_GET without binding a real TCP socket.
    """
    handler = wh_module.WebhookHandler.__new__(wh_module.WebhookHandler)
    handler.path = path
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.server = MagicMock()
    handler.request = MagicMock()
    handler.client_address = ("127.0.0.1", 12345)
    handler.log_message = lambda *a: None  # suppress access log

    # Stub out HTTP primitives so we can inspect calls without a real socket
    handler._response_status = None
    handler._response_headers = {}

    def _send_response(code, message=None):
        handler._response_status = code

    def _send_header(k, v):
        handler._response_headers[k] = v

    def _end_headers():
        # Write a minimal HTTP/1.1 response line so wfile.getvalue() is readable
        handler.wfile.write(
            f"HTTP/1.1 {handler._response_status} -\r\n\r\n".encode()
        )

    handler.send_response = _send_response
    handler.send_header = _send_header
    handler.end_headers = _end_headers
    return handler


class TestWebhookServerHTTP(unittest.TestCase):
    """
    Tests for WebhookHandler HTTP paths, using a mocked handler instance
    (no real TCP socket).  Also covers helper functions not exercised by the
    business-logic tests above.
    """

    def setUp(self):
        _reset_counters(_wh)
        self._tmp = tempfile.mkdtemp()
        self._state_file = os.path.join(self._tmp, "state.json")
        self._fp_file = os.path.join(self._tmp, "false-positives.jsonl")
        self._retrain_file = os.path.join(self._tmp, "retrain-needed")

        self._p1 = patch.object(_wh, "STATE_FILE", self._state_file)
        self._p2 = patch.object(_wh, "FALSE_POSITIVES_FILE", self._fp_file)
        self._p3 = patch.object(_wh, "RETRAIN_FILE", self._retrain_file)
        self._p1.start(); self._p2.start(); self._p3.start()

    def tearDown(self):
        self._p1.stop(); self._p2.stop(); self._p3.stop()

    def _write_state(self, data: dict) -> None:
        with open(self._state_file, "w") as f:
            json.dump(data, f)

    # ------------------------------------------------------------------
    # 1. replacement-confirmed removes node and writes retrain signal
    # ------------------------------------------------------------------
    def test_replacement_confirmed_removes_node_from_state(self):
        self._write_state({"node-01": {"last_action": "2026-06-20T00:00:00+00:00"}})
        _wh.handle_replacement_confirmed("node-01", "dimm")
        with open(self._state_file) as f:
            state = json.load(f)
        self.assertNotIn("node-01", state)

    # ------------------------------------------------------------------
    # 2. replacement-confirmed touches the retrain-needed file
    # ------------------------------------------------------------------
    def test_replacement_confirmed_writes_retrain_signal(self):
        self._write_state({})
        _wh.handle_replacement_confirmed("node-01", "dimm")
        self.assertTrue(os.path.exists(self._retrain_file))

    # ------------------------------------------------------------------
    # 3. replacement-confirmed increments the counter
    # ------------------------------------------------------------------
    def test_replacement_confirmed_increments_counter(self):
        self._write_state({})
        before = _wh.get_counter("replacement_confirmed_total", {"component": "nvme"})
        _wh.handle_replacement_confirmed("node-02", "nvme")
        after = _wh.get_counter("replacement_confirmed_total", {"component": "nvme"})
        self.assertEqual(after, before + 1)

    # ------------------------------------------------------------------
    # 4. do_POST /webhook/replacement-confirmed with missing node → 400
    # ------------------------------------------------------------------
    def test_replacement_confirmed_missing_node_field_returns_400(self):
        body = json.dumps({"component": "dimm"}).encode()  # no "node" key
        h = _make_handler(_wh, "/webhook/replacement-confirmed", body)
        h.do_POST()
        self.assertEqual(h._response_status, 400)

    # ------------------------------------------------------------------
    # 5. false-positive increments counter
    # ------------------------------------------------------------------
    def test_false_positive_increments_counter(self):
        before = _wh.get_counter("false_positive_total", {"alertname": "MemoryFailure"})
        _wh.handle_false_positive("MemoryFailure", "node-01")
        after = _wh.get_counter("false_positive_total", {"alertname": "MemoryFailure"})
        self.assertEqual(after, before + 1)

    # ------------------------------------------------------------------
    # 6. false-positive appends an entry to false-positives.jsonl
    # ------------------------------------------------------------------
    def test_false_positive_appends_to_jsonl(self):
        _wh.handle_false_positive("MemoryFailure", "node-01")
        with open(self._fp_file) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["alertname"], "MemoryFailure")
        self.assertEqual(record["node"], "node-01")
        self.assertIn("timestamp", record)

    # ------------------------------------------------------------------
    # 7. do_POST routes /webhook/replacement-confirmed → handle_replacement_confirmed
    # ------------------------------------------------------------------
    def test_do_post_routes_replacement_confirmed(self):
        self._write_state({"node-03": {}})
        body = json.dumps({"node": "node-03", "component": "dimm"}).encode()
        h = _make_handler(_wh, "/webhook/replacement-confirmed", body)
        with patch.object(_wh, "handle_replacement_confirmed") as mock_fn:
            h.do_POST()
        mock_fn.assert_called_once_with("node-03", "dimm")
        self.assertEqual(h._response_status, 200)

    # ------------------------------------------------------------------
    # 8. do_POST routes /webhook/false-positive → handle_false_positive
    # ------------------------------------------------------------------
    def test_do_post_routes_false_positive(self):
        body = json.dumps({"alertname": "MemoryFailure", "node": "node-01"}).encode()
        h = _make_handler(_wh, "/webhook/false-positive", body)
        with patch.object(_wh, "handle_false_positive") as mock_fn:
            h.do_POST()
        mock_fn.assert_called_once_with("MemoryFailure", "node-01")
        self.assertEqual(h._response_status, 200)

    # ------------------------------------------------------------------
    # 9. do_POST with unknown path → 404
    # ------------------------------------------------------------------
    def test_do_post_unknown_path_returns_404(self):
        h = _make_handler(_wh, "/unknown")
        h.do_POST()
        self.assertEqual(h._response_status, 404)

    # ------------------------------------------------------------------
    # 10. do_GET /metrics → Prometheus text containing expected strings
    # ------------------------------------------------------------------
    def test_do_get_metrics_returns_prometheus_text(self):
        # Pre-populate a counter so render_metrics has a non-empty series
        _wh.counter_inc("replacement_confirmed_total", {"component": "gpu"})
        h = _make_handler(_wh, "/metrics")
        h.do_GET()
        output = h.wfile.getvalue().decode()
        self.assertIn("replacement_confirmed_total", output)
        self.assertIn("webhook_server_up 1", output)
        self.assertEqual(h._response_status, 200)

    # ------------------------------------------------------------------
    # 11. do_GET /healthz → 200 with body "OK"
    # ------------------------------------------------------------------
    def test_do_get_healthz_returns_200(self):
        h = _make_handler(_wh, "/healthz")
        h.do_GET()
        self.assertEqual(h._response_status, 200)
        output = h.wfile.getvalue().decode()
        self.assertIn("OK", output)

    # ------------------------------------------------------------------
    # 12. do_GET with unknown path → 404
    # ------------------------------------------------------------------
    def test_do_get_unknown_path_returns_404(self):
        h = _make_handler(_wh, "/notfound")
        h.do_GET()
        self.assertEqual(h._response_status, 404)

    # ------------------------------------------------------------------
    # 13. _labels_str returns correct Prometheus label syntax
    # ------------------------------------------------------------------
    def test_labels_str_nonempty(self):
        result = _wh._labels_str({"component": "dimm", "node": "n1"})
        self.assertTrue(result.startswith("{"))
        self.assertTrue(result.endswith("}"))
        self.assertIn('component="dimm"', result)
        self.assertIn('node="n1"', result)

    # ------------------------------------------------------------------
    # 14. render_metrics includes label series when counter has values
    # ------------------------------------------------------------------
    def test_render_metrics_with_counter_values(self):
        _wh.counter_inc("false_positive_total", {"alertname": "DiskError"})
        text = _wh.render_metrics()
        self.assertIn("false_positive_total", text)
        self.assertIn('alertname="DiskError"', text)

    # ------------------------------------------------------------------
    # 15. _load_state returns empty dict on bad JSON
    # ------------------------------------------------------------------
    def test_load_state_returns_empty_dict_on_bad_json(self):
        with open(self._state_file, "w") as f:
            f.write("not-valid-json{{{")
        result = _wh._load_state()
        self.assertEqual(result, {})

    # ------------------------------------------------------------------
    # 16. do_POST with invalid JSON body → 400
    # ------------------------------------------------------------------
    def test_do_post_invalid_json_returns_400(self):
        h = _make_handler(_wh, "/webhook/replacement-confirmed", b"not-json{{")
        h.do_POST()
        self.assertEqual(h._response_status, 400)

    # ------------------------------------------------------------------
    # 17. do_POST /webhook/false-positive with missing fields → 400
    # ------------------------------------------------------------------
    def test_do_post_false_positive_missing_fields_returns_400(self):
        body = json.dumps({"alertname": "MemFail"}).encode()  # no "node"
        h = _make_handler(_wh, "/webhook/false-positive", body)
        h.do_POST()
        self.assertEqual(h._response_status, 400)

    # ------------------------------------------------------------------
    # 18. _labels_str with empty dict returns empty string
    # ------------------------------------------------------------------
    def test_labels_str_empty_returns_empty_string(self):
        result = _wh._labels_str({})
        self.assertEqual(result, "")

    # ------------------------------------------------------------------
    # 19. _read_json with zero Content-Length returns empty dict
    # ------------------------------------------------------------------
    def test_read_json_zero_content_length_returns_empty_dict(self):
        h = _make_handler(_wh, "/webhook/replacement-confirmed", b"")
        h.headers = {"Content-Length": "0"}
        result = h._read_json()
        self.assertEqual(result, {})

    # ------------------------------------------------------------------
    # 20. do_POST /webhook/replacement-confirmed with handler error → 500
    # ------------------------------------------------------------------
    def test_do_post_replacement_confirmed_handler_error_returns_500(self):
        body = json.dumps({"node": "node-04", "component": "dimm"}).encode()
        h = _make_handler(_wh, "/webhook/replacement-confirmed", body)
        with patch.object(_wh, "handle_replacement_confirmed",
                          side_effect=RuntimeError("disk full")):
            h.do_POST()
        self.assertEqual(h._response_status, 500)

    # ------------------------------------------------------------------
    # 21. do_POST /webhook/false-positive with handler error → 500
    # ------------------------------------------------------------------
    def test_do_post_false_positive_handler_error_returns_500(self):
        body = json.dumps({"alertname": "MemFail", "node": "node-05"}).encode()
        h = _make_handler(_wh, "/webhook/false-positive", body)
        with patch.object(_wh, "handle_false_positive",
                          side_effect=RuntimeError("io error")):
            h.do_POST()
        self.assertEqual(h._response_status, 500)

    # ------------------------------------------------------------------
    # 22. do_POST /webhook/false-positive with invalid JSON → 400
    # ------------------------------------------------------------------
    def test_do_post_false_positive_invalid_json_returns_400(self):
        h = _make_handler(_wh, "/webhook/false-positive", b"not-json{{")
        h.do_POST()
        self.assertEqual(h._response_status, 400)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
