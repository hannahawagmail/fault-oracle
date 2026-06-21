#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Remediation controller: watches failure_probability_7d Prometheus metric and
automatically cordons nodes whose predicted failure probability exceeds threshold.

Designed to run as a Kubernetes Deployment (not per-node) with access to the
Kubernetes API via ServiceAccount token.
"""

import json
import logging
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Lock, Thread

# ---------------------------------------------------------------------------
# Configuration (all from environment variables)
# ---------------------------------------------------------------------------

PROMETHEUS_URL: str = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
CORDON_THRESHOLD: float = float(os.environ.get("CORDON_THRESHOLD", "0.8"))
COOLDOWN_HOURS: float = float(os.environ.get("COOLDOWN_HOURS", "4"))
DRY_RUN: bool = os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes")
LOKI_URL: str = os.environ.get("LOKI_URL", "")
KUBECONFIG: str = os.environ.get("KUBECONFIG", "")
POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL", "60"))
STATE_FILE: str = os.environ.get(
    "STATE_FILE", "/var/lib/hw-fault-remediation/state.json"
)
METRICS_PORT: int = int(os.environ.get("METRICS_PORT", "8080"))

# In-cluster token paths
_SA_TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_SA_CA_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
_SA_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("remediation-controller")

# ---------------------------------------------------------------------------
# Prometheus metrics (minimal hand-rolled exposition)
# ---------------------------------------------------------------------------

_metrics_lock = Lock()

_counters: dict[str, dict[tuple, int]] = {
    "remediation_actions_total": {},
    "remediation_poll_errors_total": {},
}
_gauges: dict[str, dict[tuple, float]] = {
    "remediation_cooldown_active": {},
    "remediation_nodes_at_risk": {(): 0.0},
    "remediation_controller_last_poll_timestamp": {(): 0.0},
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


def gauge_set(name: str, value: float, labels: dict | None = None) -> None:
    key = tuple(sorted((labels or {}).items()))
    with _metrics_lock:
        _gauges[name][key] = value


def render_metrics() -> str:
    lines: list[str] = []
    with _metrics_lock:
        for name, series in _counters.items():
            lines.append(f"# TYPE {name} counter")
            for key, val in series.items():
                labels = dict(key)
                lines.append(f"{name}{_labels_str(labels)} {val}")
        for name, series in _gauges.items():
            lines.append(f"# TYPE {name} gauge")
            for key, val in series.items():
                labels = dict(key)
                lines.append(f"{name}{_labels_str(labels)} {val}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Kubernetes API client (no external deps)
# ---------------------------------------------------------------------------


class KubeClient:  # pragma: no cover
    """Minimal Kubernetes REST client using only stdlib."""

    def __init__(self) -> None:
        if KUBECONFIG:
            self._load_kubeconfig()
        else:
            self._load_incluster()

    def _load_incluster(self) -> None:
        try:
            with open(_SA_TOKEN_FILE) as f:
                self.token = f.read().strip()
        except FileNotFoundError:
            self.token = ""
        try:
            with open(_SA_NAMESPACE_FILE) as f:
                self.namespace = f.read().strip()
        except FileNotFoundError:
            self.namespace = "default"
        self.api_server = "https://kubernetes.default.svc"
        self.ca_file: str | None = _SA_CA_FILE if os.path.exists(_SA_CA_FILE) else None

    def _load_kubeconfig(self) -> None:
        import base64
        import tempfile

        with open(KUBECONFIG) as f:
            kc = json.load(f) if KUBECONFIG.endswith(".json") else __import__("yaml").safe_load(f)

        ctx_name = kc.get("current-context", "")
        ctx = next(
            (c["context"] for c in kc.get("contexts", []) if c["name"] == ctx_name),
            {},
        )
        cluster_name = ctx.get("cluster", "")
        user_name = ctx.get("user", "")
        self.namespace = ctx.get("namespace", "default")

        cluster = next(
            (c["cluster"] for c in kc.get("clusters", []) if c["name"] == cluster_name),
            {},
        )
        user = next(
            (u["user"] for u in kc.get("users", []) if u["name"] == user_name),
            {},
        )

        self.api_server = cluster.get("server", "https://localhost:6443")
        self.token = user.get("token", "")

        ca_data = cluster.get("certificate-authority-data", "")
        if ca_data:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
            tmp.write(base64.b64decode(ca_data))
            tmp.close()
            self.ca_file = tmp.name
        else:
            self.ca_file = cluster.get("certificate-authority")

    def _ssl_ctx(self) -> ssl.SSLContext | None:
        if not self.api_server.startswith("https"):
            return None
        ctx = ssl.create_default_context()
        if self.ca_file and os.path.exists(self.ca_file):
            ctx.load_verify_locations(self.ca_file)
        else:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _headers(self) -> dict:
        h = {"Content-Type": "application/merge-patch+json", "Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        content_type: str = "application/merge-patch+json",
    ) -> dict:
        url = self.api_server.rstrip("/") + path
        data = json.dumps(body).encode() if body is not None else None
        headers = self._headers()
        headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        ssl_ctx = self._ssl_ctx()
        try:
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Kubernetes API {method} {path} -> HTTP {e.code}: {e.read().decode()}") from e

    def patch_node(self, node_name: str, patch: dict) -> dict:
        return self._request("PATCH", f"/api/v1/nodes/{node_name}", patch)

    def get_node(self, node_name: str) -> dict:
        url = self.api_server.rstrip("/") + f"/api/v1/nodes/{node_name}"
        headers = self._headers()
        req = urllib.request.Request(url, headers=headers, method="GET")
        ssl_ctx = self._ssl_ctx()
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as resp:
            return json.loads(resp.read())

    def create_event(self, namespace: str, event: dict) -> dict:
        return self._request(
            "POST",
            f"/api/v1/namespaces/{namespace}/events",
            event,
            content_type="application/json",
        )


# ---------------------------------------------------------------------------
# State file
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


# ---------------------------------------------------------------------------
# Cooldown helpers
# ---------------------------------------------------------------------------


def _cooldown_active(state: dict, node: str) -> bool:
    entry = state.get(node)
    if not entry:
        return False
    last = entry.get("last_action")
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        elapsed_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
        return elapsed_hours < COOLDOWN_HOURS
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Prometheus query
# ---------------------------------------------------------------------------


def query_prometheus(metric: str = "failure_probability_7d") -> list[tuple[str, float]]:  # pragma: no cover
    """Return list of (node_name, value) for the instant query."""
    url = (
        PROMETHEUS_URL.rstrip("/")
        + "/api/v1/query?"
        + urllib.parse.urlencode({"query": metric})
    )
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        raise RuntimeError(f"Prometheus query failed: {exc}") from exc

    if data.get("status") != "success":
        raise RuntimeError(f"Prometheus returned status: {data.get('status')}")

    results = []
    for item in data.get("data", {}).get("result", []):
        node = item.get("metric", {}).get("node") or item.get("metric", {}).get("instance", "")
        if not node:
            continue
        try:
            value = float(item["value"][1])
        except (KeyError, IndexError, ValueError):
            continue
        results.append((node, value))
    return results


# ---------------------------------------------------------------------------
# Kubernetes actions
# ---------------------------------------------------------------------------


def build_cordon_patch() -> dict:
    return {"spec": {"unschedulable": True}}


def build_taint_patch(threshold: float) -> dict:
    return {
        "spec": {
            "taints": [
                {
                    "key": "fault-resilience/predicted-failure",
                    "effect": "NoSchedule",
                    "value": f"P>{threshold:.0%}",
                }
            ]
        }
    }


def build_event(node_name: str, probability: float, dry_run: bool) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "apiVersion": "v1",
        "kind": "Event",
        "metadata": {
            "name": f"hw-fault-remediation-{node_name}-{int(time.time())}",
            "namespace": "default",
        },
        "involvedObject": {
            "apiVersion": "v1",
            "kind": "Node",
            "name": node_name,
        },
        "reason": "PredictedFailure",
        "message": (
            "[DRY-RUN] " if dry_run else ""
        ) + f"Node {node_name} cordoned: failure_probability_7d={probability:.3f} > threshold={CORDON_THRESHOLD:.3f}",
        "type": "Warning",
        "eventTime": now,
        "reportingComponent": "hw-fault-remediation-controller",
        "reportingInstance": "remediation-controller",
        "firstTimestamp": now,
        "lastTimestamp": now,
        "count": 1,
        "source": {"component": "remediation-controller"},
    }


def push_loki(node: str, probability: float, action: str, dry_run: bool) -> None:  # pragma: no cover
    if not LOKI_URL:
        return
    ts_ns = str(int(time.time() * 1e9))
    payload = {
        "streams": [
            {
                "stream": {
                    "job": "remediation-controller",
                    "node": node,
                    "action": action,
                    "dry_run": str(dry_run).lower(),
                },
                "values": [
                    [
                        ts_ns,
                        json.dumps(
                            {
                                "node": node,
                                "probability": probability,
                                "action": action,
                                "dry_run": dry_run,
                                "threshold": CORDON_THRESHOLD,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }
                        ),
                    ]
                ],
            }
        ]
    }
    url = LOKI_URL.rstrip("/") + "/loki/api/v1/push"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as exc:
        log.warning("Loki push failed for node %s: %s", node, exc)


def remediate_node(  # pragma: no cover
    kube: KubeClient, node_name: str, probability: float, state: dict
) -> None:
    """Cordon + taint a node and record the action."""
    if DRY_RUN:
        log.info(
            "[DRY-RUN] Would cordon node=%s probability=%.4f threshold=%.4f",
            node_name,
            probability,
            CORDON_THRESHOLD,
        )
        _update_state_entry(state, node_name, probability, dry_run=True)
        counter_inc(
            "remediation_actions_total",
            {"action": "cordon", "node": node_name, "dry_run": "true"},
        )
        push_loki(node_name, probability, "cordon", dry_run=True)
        return

    log.info(
        "Cordoning node=%s probability=%.4f threshold=%.4f",
        node_name,
        probability,
        CORDON_THRESHOLD,
    )
    kube.patch_node(node_name, build_cordon_patch())
    kube.patch_node(node_name, build_taint_patch(CORDON_THRESHOLD))

    event = build_event(node_name, probability, dry_run=False)
    try:
        kube.create_event("default", event)
    except Exception as exc:
        log.warning("Event creation failed for node %s: %s", node_name, exc)

    push_loki(node_name, probability, "cordon", dry_run=False)
    _update_state_entry(state, node_name, probability, dry_run=False)
    counter_inc(
        "remediation_actions_total",
        {"action": "cordon", "node": node_name, "dry_run": "false"},
    )


def _update_state_entry(
    state: dict, node: str, probability: float, dry_run: bool
) -> None:
    state[node] = {
        "last_action": datetime.now(timezone.utc).isoformat(),
        "probability": probability,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------


def poll_once(kube: KubeClient) -> None:
    gauge_set("remediation_controller_last_poll_timestamp", time.time())
    try:
        results = query_prometheus()
    except Exception as exc:
        log.error("Prometheus poll error: %s", exc)
        counter_inc("remediation_poll_errors_total")
        return

    state = _load_state()
    at_risk = 0
    for node, value in results:
        if value > CORDON_THRESHOLD:
            at_risk += 1
            active = _cooldown_active(state, node)
            gauge_set(
                "remediation_cooldown_active", 1.0 if active else 0.0, {"node": node}
            )
            if active:
                log.info(
                    "Cooldown active for node=%s, skipping (last_action=%s)",
                    node,
                    state[node].get("last_action"),
                )
                continue
            try:
                remediate_node(kube, node, value, state)
            except Exception as exc:
                log.error("Remediation failed for node=%s: %s", node, exc)
        else:
            gauge_set("remediation_cooldown_active", 0.0, {"node": node})

    gauge_set("remediation_nodes_at_risk", float(at_risk))
    _save_state(state)


# ---------------------------------------------------------------------------
# Prometheus /metrics HTTP handler
# ---------------------------------------------------------------------------


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/metrics":
            body = render_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt: str, *args) -> None:  # silence access log
        pass


def start_metrics_server() -> None:  # pragma: no cover
    server = HTTPServer(("0.0.0.0", METRICS_PORT), MetricsHandler)
    t = Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info("Metrics server listening on :%d/metrics", METRICS_PORT)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:  # pragma: no cover
    log.info(
        "Remediation controller starting — prometheus=%s threshold=%.2f "
        "cooldown_hours=%.1f dry_run=%s poll_interval=%ds",
        PROMETHEUS_URL,
        CORDON_THRESHOLD,
        COOLDOWN_HOURS,
        DRY_RUN,
        POLL_INTERVAL,
    )
    start_metrics_server()
    kube = KubeClient()

    while True:
        poll_once(kube)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":  # pragma: no cover
    main()
