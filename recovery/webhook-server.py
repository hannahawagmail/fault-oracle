#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
recovery/webhook-server.py — Alertmanager webhook receiver for automated remediation.

Receives Alertmanager webhook payloads, routes each alert fingerprint to a
handler script, and suppresses duplicate executions within a cooldown window.

Usage:
    python3 recovery/webhook-server.py [--port 9095] [--handlers-dir recovery/handlers] [--dry-run]

Handler scripts:
    recovery/handlers/<alertname>.sh  — executed when the named alert fires.
    Receives alert labels as environment variables: ALERT_NODE, ALERT_COLLECTOR, etc.

Exit codes from handlers:
    0 — remediation succeeded
    1 — remediation failed (logged, no retry)
    2 — prerequisite missing (logged)
"""
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Lock

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_PORT = 9095
DEFAULT_HANDLERS_DIR = Path(__file__).parent / "handlers"
COOLDOWN_SECONDS = 300   # 5 min: don't re-run the same handler within this window
MAX_CONCURRENT = 4       # max simultaneous handler executions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("webhook-server")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_lock = Lock()
_recent: dict[str, float] = {}   # fingerprint → last execution timestamp
_running = 0


def _allowed(fingerprint: str) -> bool:
    """Return True if the fingerprint is not in cooldown."""
    now = time.time()
    with _lock:
        last = _recent.get(fingerprint, 0)
        if now - last < COOLDOWN_SECONDS:
            return False
        _recent[fingerprint] = now
        return True


def _make_fingerprint(alert: dict) -> str:
    labels = alert.get("labels", {})
    key = json.dumps(labels, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _handler_path(handlers_dir: Path, alert_name: str) -> Path | None:
    p = handlers_dir / f"{alert_name}.sh"
    return p if p.exists() else None


def _run_handler(script: Path, alert: dict, dry_run: bool) -> int:
    """Execute a handler script with alert labels as environment variables."""
    env = os.environ.copy()
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})

    for k, v in labels.items():
        env[f"ALERT_{k.upper()}"] = str(v)
    for k, v in annotations.items():
        env[f"ALERT_ANNOTATION_{k.upper()}"] = str(v)

    env["ALERT_STATUS"] = alert.get("status", "firing")
    env["ALERT_STARTS_AT"] = alert.get("startsAt", "")

    if dry_run:
        log.info("DRY-RUN: would execute %s (node=%s)", script.name, labels.get("node", "?"))
        return 0

    log.info("Executing handler: %s (node=%s, alert=%s)",
             script.name, labels.get("node", "?"), labels.get("alertname", "?"))
    try:
        result = subprocess.run(
            ["bash", str(script)],
            env=env,
            timeout=120,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            log.info("Handler succeeded: %s", script.name)
        else:
            log.warning("Handler exited %d: %s\n%s",
                        result.returncode, script.name, result.stderr[:500])
        return result.returncode
    except subprocess.TimeoutExpired:
        log.error("Handler timed out: %s", script.name)
        return 1
    except Exception as exc:
        log.error("Handler error: %s: %s", script.name, exc)
        return 1


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class WebhookHandler(BaseHTTPRequestHandler):
    handlers_dir: Path = DEFAULT_HANDLERS_DIR
    dry_run: bool = False

    def log_message(self, fmt, *args):  # suppress default access log
        pass

    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            log.error("JSON decode error: %s", e)
            self.send_response(400)
            self.end_headers()
            return

        alerts = payload.get("alerts", [])
        log.info("Received %d alert(s) from Alertmanager", len(alerts))

        for alert in alerts:
            if alert.get("status") != "firing":
                continue   # ignore resolved

            fp = _make_fingerprint(alert)
            alert_name = alert.get("labels", {}).get("alertname", "")

            script = _handler_path(self.handlers_dir, alert_name)
            if not script:
                log.debug("No handler for alert: %s", alert_name)
                continue

            if not _allowed(fp):
                log.info("Suppressing duplicate: %s (fingerprint %s)", alert_name, fp)
                continue

            _run_handler(script, alert, self.dry_run)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--handlers-dir", type=Path, default=DEFAULT_HANDLERS_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="info",
                        choices=["debug", "info", "warning", "error"])
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level.upper())

    WebhookHandler.handlers_dir = args.handlers_dir
    WebhookHandler.dry_run = args.dry_run

    log.info("Webhook server starting on port %d (dry_run=%s)", args.port, args.dry_run)
    log.info("Handlers directory: %s", args.handlers_dir)

    server = HTTPServer(("", args.port), WebhookHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutdown")


if __name__ == "__main__":
    main()
