#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
bmc/ipmi-sel-poller.py — Out-of-band IPMI SEL event collector via ipmitool.

Runs from a management-plane Deployment (not per-node DaemonSet) so it survives
OS kernel crashes by communicating out-of-band through each node's BMC IP.

BMC targets are read from the BMC_TARGETS environment variable as JSON:
    [{"node": "node-01", "ip": "192.168.1.100", "user": "admin", "pass": "..."}]

Emits Prometheus textfile format to --output (default stdout).

Metrics:
    ipmi_sel_event_total{node, sensor_type, severity}      counter
    ipmi_sel_last_event_timestamp{node}                    gauge  (Unix ts of most recent event)
    ipmi_sel_last_record_id{node}                          gauge
    ipmi_bmc_poll_error_total{node}                        counter
    ipmi_bmc_collector_up{node}                            gauge  (1 if reachable)
    ipmi_bmc_collector_last_run_timestamp                  gauge
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_STATE_FILE = Path("/var/lib/hw-fault-bmc/sel-state.json")
DEFAULT_OUTPUT     = Path("-")   # stdout

# Sensor-name substrings that map a SEL Assert event → critical severity.
CRITICAL_SENSOR_PATTERNS = re.compile(r"CPU|Memory|Power|Thermal", re.IGNORECASE)

# Sensor-name → category label used in ipmi_sel_event_total{sensor_type=…}
SENSOR_TYPE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"CPU",     re.IGNORECASE), "cpu"),
    (re.compile(r"Mem",     re.IGNORECASE), "memory"),
    (re.compile(r"Power|PS\d|PSU", re.IGNORECASE), "power"),
    (re.compile(r"Therm|Temp|Fan", re.IGNORECASE), "thermal"),
]


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict[str, int]:
    """Return {node: last_record_id} from the state file, or {} if absent/corrupt."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_state(path: Path, state: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------

def sanitize_label(val: str) -> str:
    """Remove characters that are invalid in Prometheus label values."""
    return val.replace('"', "'").replace('\\', '/').replace('\n', ' ').strip() or "unknown"


def sensor_type_for(sensor_name: str) -> str:
    for pattern, label in SENSOR_TYPE_PATTERNS:
        if pattern.search(sensor_name):
            return label
    return "other"


def classify_severity(event_direction: str, sensor_name: str) -> str:
    """Return 'critical' or 'warning' based on direction and sensor name."""
    if event_direction.strip().lower() == "assert" and CRITICAL_SENSOR_PATTERNS.search(sensor_name):
        return "critical"
    return "warning"


# ---------------------------------------------------------------------------
# SEL parsing
# ---------------------------------------------------------------------------

def parse_sel_csv(raw: str) -> list[dict[str, Any]]:
    """
    Parse ipmitool sel list -c CSV output.

    Expected columns (no header row):
        record_id, timestamp, sensor_name, event_description,
        event_direction, severity_hint

    Malformed lines (wrong column count, bad record_id) are skipped with a
    warning to stderr.

    Returns list of dicts with keys:
        record_id (int), timestamp (float), sensor_name (str),
        event_description (str), event_direction (str), severity_hint (str)
    """
    events: list[dict[str, Any]] = []
    reader = csv.reader(io.StringIO(raw))
    for lineno, row in enumerate(reader, start=1):
        if len(row) < 6:
            if any(cell.strip() for cell in row):
                sys.stderr.write(
                    f"ipmi-sel-poller: skipping malformed line {lineno}: {row!r}\n"
                )
            continue
        try:
            record_id = int(row[0].strip(), 16) if row[0].strip().startswith("0x") \
                        else int(row[0].strip())
        except ValueError:
            sys.stderr.write(
                f"ipmi-sel-poller: bad record_id on line {lineno}: {row[0]!r}\n"
            )
            continue

        # Timestamp: ipmitool emits "MM/DD/YYYY HH:MM:SS" — parse to Unix float.
        ts_str = row[1].strip()
        try:
            import datetime
            ts = datetime.datetime.strptime(ts_str, "%m/%d/%Y %H:%M:%S").timestamp()
        except ValueError:
            ts = 0.0

        events.append({
            "record_id":        record_id,
            "timestamp":        ts,
            "sensor_name":      row[2].strip(),
            "event_description": row[3].strip(),
            "event_direction":  row[4].strip(),
            "severity_hint":    row[5].strip(),
        })
    return events


# ---------------------------------------------------------------------------
# ipmitool invocation
# ---------------------------------------------------------------------------

def run_ipmitool_sel(ip: str, user: str, password: str,  # pragma: no cover
                     timeout: int = 30) -> tuple[str | None, str | None]:
    """
    Run ipmitool sel list -c for a single BMC target.

    Returns (stdout_text, None) on success, (None, error_string) on failure.
    """
    cmd = [
        "ipmitool",
        "-H", ip,
        "-U", user,
        "-P", password,
        "-I", "lanplus",
        "sel", "list", "-c",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return None, (result.stderr.strip() or f"exit code {result.returncode}")
        return result.stdout, None
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except FileNotFoundError:
        return None, "ipmitool not found"
    except Exception as exc:
        return None, str(exc)


# ---------------------------------------------------------------------------
# Per-node collection
# ---------------------------------------------------------------------------

def collect_node_sel(
    target: dict,
    last_record_id: int,
    run_ipmitool_fn=run_ipmitool_sel,
) -> dict[str, Any]:
    """
    Collect SEL events for one node.

    Returns a result dict:
        node              str
        up                bool
        error             str | None
        new_events        list of parsed event dicts (only records > last_record_id)
        last_record_id    int  (highest seen, or unchanged if no new events)
        last_event_ts     float | None
    """
    node = target["node"]
    raw, error = run_ipmitool_fn(target["ip"], target["user"], target["pass"])
    if error:
        return {
            "node": node, "up": False, "error": error,
            "new_events": [], "last_record_id": last_record_id,
            "last_event_ts": None,
        }

    all_events = parse_sel_csv(raw or "")
    new_events  = [e for e in all_events if e["record_id"] > last_record_id]
    new_max_id  = max((e["record_id"] for e in new_events), default=last_record_id)
    last_event_ts = (
        max(e["timestamp"] for e in all_events) if all_events else None
    )
    return {
        "node": node, "up": True, "error": None,
        "new_events": new_events,
        "last_record_id": new_max_id,
        "last_event_ts": last_event_ts,
    }


# ---------------------------------------------------------------------------
# Prometheus output
# ---------------------------------------------------------------------------

def emit_metrics(
    results: list[dict[str, Any]],
    poll_errors: dict[str, int],
) -> str:
    """Render all collected data as Prometheus text format."""
    lines: list[str] = []
    now = time.time()

    # ---- ipmi_sel_event_total ------------------------------------------------
    lines.append("# HELP ipmi_sel_event_total Total IPMI SEL events observed per node.")
    lines.append("# TYPE ipmi_sel_event_total counter")
    for r in results:
        node = sanitize_label(r["node"])
        for evt in r["new_events"]:
            stype    = sanitize_label(sensor_type_for(evt["sensor_name"]))
            severity = sanitize_label(classify_severity(evt["event_direction"], evt["sensor_name"]))
            lines.append(
                f'ipmi_sel_event_total{{node="{node}",sensor_type="{stype}",severity="{severity}"}} 1'
            )

    # ---- ipmi_sel_last_event_timestamp ---------------------------------------
    lines.append("# HELP ipmi_sel_last_event_timestamp Unix timestamp of the most recent SEL event.")
    lines.append("# TYPE ipmi_sel_last_event_timestamp gauge")
    for r in results:
        if r["last_event_ts"] is not None:
            node = sanitize_label(r["node"])
            lines.append(
                f'ipmi_sel_last_event_timestamp{{node="{node}"}} {r["last_event_ts"]:.3f}'
            )

    # ---- ipmi_sel_last_record_id ---------------------------------------------
    lines.append("# HELP ipmi_sel_last_record_id Last SEL record ID seen per node.")
    lines.append("# TYPE ipmi_sel_last_record_id gauge")
    for r in results:
        node = sanitize_label(r["node"])
        lines.append(
            f'ipmi_sel_last_record_id{{node="{node}"}} {r["last_record_id"]}'
        )

    # ---- ipmi_bmc_poll_error_total -------------------------------------------
    lines.append("# HELP ipmi_bmc_poll_error_total Total ipmitool poll errors per node.")
    lines.append("# TYPE ipmi_bmc_poll_error_total counter")
    for r in results:
        node  = sanitize_label(r["node"])
        count = poll_errors.get(r["node"], 0)
        lines.append(f'ipmi_bmc_poll_error_total{{node="{node}"}} {count}')

    # ---- ipmi_bmc_collector_up -----------------------------------------------
    lines.append("# HELP ipmi_bmc_collector_up 1 if the BMC was reachable on the last poll.")
    lines.append("# TYPE ipmi_bmc_collector_up gauge")
    for r in results:
        node = sanitize_label(r["node"])
        up   = 1 if r["up"] else 0
        lines.append(f'ipmi_bmc_collector_up{{node="{node}"}} {up}')

    # ---- ipmi_bmc_collector_last_run_timestamp --------------------------------
    lines.append("# HELP ipmi_bmc_collector_last_run_timestamp Unix timestamp of the last collector run.")
    lines.append("# TYPE ipmi_bmc_collector_last_run_timestamp gauge")
    lines.append(f"ipmi_bmc_collector_last_run_timestamp {now:.3f}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):  # pragma: no cover
    parser = argparse.ArgumentParser(
        description="IPMI SEL out-of-band poller — emits Prometheus textfile metrics."
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help="Output .prom file path, or '-' for stdout (default: stdout)",
    )
    parser.add_argument(
        "--state-file", type=Path, default=DEFAULT_STATE_FILE,
        help="JSON state file tracking last-seen SEL record IDs per node",
    )
    parser.add_argument(
        "--timeout", type=int, default=30,
        help="ipmitool per-target timeout in seconds (default: 30)",
    )
    args = parser.parse_args(argv)

    # Load BMC targets from environment variable.
    raw_targets = os.environ.get("BMC_TARGETS", "[]")
    try:
        targets: list[dict] = json.loads(raw_targets)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"ipmi-sel-poller: invalid BMC_TARGETS JSON: {exc}\n")
        targets = []

    # Check whether ipmitool is available at all.
    ipmitool_present = shutil.which("ipmitool") is not None

    state       = load_state(args.state_file)
    results     = []
    poll_errors: dict[str, int] = {}

    for target in targets:
        node = target.get("node", "unknown")
        last_id = state.get(node, 0)

        if not ipmitool_present:
            results.append({
                "node": node, "up": False, "error": "ipmitool not found",
                "new_events": [], "last_record_id": last_id,
                "last_event_ts": None,
            })
            poll_errors[node] = poll_errors.get(node, 0) + 1
            continue

        result = collect_node_sel(target, last_id)
        results.append(result)

        if not result["up"]:
            poll_errors[node] = poll_errors.get(node, 0) + 1
        else:
            poll_errors.setdefault(node, 0)
            state[node] = result["last_record_id"]

    # Persist updated state only if we actually ran ipmitool successfully.
    if any(r["up"] for r in results):
        try:
            save_state(args.state_file, state)
        except OSError as exc:
            sys.stderr.write(f"ipmi-sel-poller: could not write state file: {exc}\n")

    output = emit_metrics(results, poll_errors)

    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        sys.stderr.write(
            f"ipmi-sel-poller: wrote {len(output)} bytes to {args.output}\n"
        )


if __name__ == "__main__":  # pragma: no cover
    main()
