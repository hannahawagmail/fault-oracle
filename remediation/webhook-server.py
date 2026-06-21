#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
remediation/webhook-server.py — Operator-facing webhook receiver for hardware
replacement confirmations and false-positive feedback.

Endpoints
---------
POST /webhook/replacement-confirmed
    Body: {"node": "<name>", "component": "dimm|gpu|nvme"}
    Removes the node entry from state.json (clears cooldown), touches the
    ML retrain-needed file, increments replacement_confirmed_total counter.

POST /webhook/false-positive
    Body: {"alertname": "<name>", "node": "<name>"}
    Increments false_positive_total counter and appends the payload to the
    false-positives audit log.

GET /metrics
    Prometheus text-format exposition.

GET /healthz
    Returns 200 OK (liveness probe).

Runs on port 8081 (metrics controller uses 8080).
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Lock

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT: int = int(os.environ.get("WEBHOOK_PORT", "8081"))
STATE_FILE: str = os.environ.get(
    "STATE_FILE", "/var/lib/hw-fault-remediation/state.json"
)
FALSE_POSITIVES_FILE: str = os.environ.get(
    "FALSE_POSITIVES_FILE",
    "/var/lib/hw-fault-remediation/false-positives.jsonl",
)
RETRAIN_FILE: str = os.environ.get(
    "RETRAIN_FILE", "/var/lib/hw-fault-ml/retrain-needed"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("webhook-server")

# ---------------------------------------------------------------------------
# Prometheus metrics (hand-rolled, no deps)
# ---------------------------------------------------------------------------

_metrics_lock = Lock()

_counters: dict[str, dict[tuple, int]] = {
    "replacement_confirmed_total": {},
    "false_positive_total": {},
}


def _labels_str(labels: dict) -> str:
    if not labels:
        return ""
    parts = [f'{k}="{v}"' for k, v in sorted(labels.items())]
    return "{" + ",".join(parts) + "}"


def counter_inc(name: str, labels: dict | None = None) -> None:
    key = tuple(sorted((labels or {}).items()))
    with _metrics_lock:
        _counters[name][key] = _counters[name].get(key, 0) + 1


def get_counter(name: str, labels: dict | None = None) -> int:
    """Return current counter value (used in tests)."""
    key = tuple(sorted((labels or {}).items()))
    with _metrics_lock:
        return _counters[name].get(key, 0)


def render_metrics() -> str:
    lines: list[str] = []
    with _metrics_lock:
        for name, series in _counters.items():
            lines.append(f"# TYPE {name} counter")
            for key, val in series.items():
                labels = dict(key)
                lines.append(f"{name}{_labels_str(labels)} {val}")
    lines.append("# TYPE webhook_server_up gauge")
    lines.append("webhook_server_up 1")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# State file helpers
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    path = Path(STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, str(path))


def _touch_retrain() -> None:
    path = Path(RETRAIN_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _append_false_positive(record: dict) -> None:
    path = Path(FALSE_POSITIVES_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Business logic (importable for tests)
# ---------------------------------------------------------------------------


def handle_replacement_confirmed(node: str, component: str) -> None:
    """Remove node from state, touch retrain file, increment counter."""
    state = _load_state()
    if node in state:
        del state[node]
        _save_state(state)
        log.info("Removed node=%s from state after %s replacement", node, component)
    else:
        log.info("Node=%s not in state (replacement confirmed anyway)", node)

    _touch_retrain()
    counter_inc("replacement_confirmed_total", {"component": component})
    log.info(
        "replacement_confirmed component=%s node=%s", component, node
    )


def handle_false_positive(alertname: str, node: str) -> None:
    """Record false positive, increment counter."""
    record = {
        "alertname": alertname,
        "node": node,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _append_false_positive(record)
    counter_inc("false_positive_total", {"alertname": alertname})
    log.info("false_positive alertname=%s node=%s", alertname, node)


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------


class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # silence access log
        pass

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("JSON decode error: %s", exc)
            return None

    def _send(self, status: int, body: bytes, content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    # POST handlers
    # ------------------------------------------------------------------

    def do_POST(self) -> None:
        if self.path == "/webhook/replacement-confirmed":
            payload = self._read_json()
            if payload is None:
                self._send(400, b"invalid JSON")
                return
            node = payload.get("node", "")
            component = payload.get("component", "")
            if not node or not component:
                self._send(400, b"missing node or component")
                return
            try:
                handle_replacement_confirmed(node, component)
            except Exception as exc:
                log.error("replacement-confirmed error: %s", exc)
                self._send(500, str(exc).encode())
                return
            self._send(200, b"ok")

        elif self.path == "/webhook/false-positive":
            payload = self._read_json()
            if payload is None:
                self._send(400, b"invalid JSON")
                return
            alertname = payload.get("alertname", "")
            node = payload.get("node", "")
            if not alertname or not node:
                self._send(400, b"missing alertname or node")
                return
            try:
                handle_false_positive(alertname, node)
            except Exception as exc:
                log.error("false-positive error: %s", exc)
                self._send(500, str(exc).encode())
                return
            self._send(200, b"ok")

        else:
            self._send(404, b"not found")

    # ------------------------------------------------------------------
    # GET handlers
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        if self.path == "/metrics":
            body = render_metrics().encode()
            self._send(200, body, "text/plain; version=0.0.4")
        elif self.path == "/healthz":
            self._send(200, b"OK")
        else:
            self._send(404, b"not found")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:  # pragma: no cover — entry point, requires live socket
    log.info(
        "Webhook server starting on port %d (state=%s)", PORT, STATE_FILE
    )
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutdown")


if __name__ == "__main__":  # pragma: no cover
    main()
