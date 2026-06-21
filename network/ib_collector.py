#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
network/ib-collector.py — Collect InfiniBand port error counters.

Emits Prometheus textfile metrics to stdout or --output file. Degrades
gracefully when no InfiniBand hardware is present.

Metrics emitted:
    ib_port_error_total{device,port,counter}     — per-port error counter
    ib_port_link_rate_gbps{device,port}          — link rate in Gbps
    ib_port_state{device,port}                   — 1=Active, 0=other
    ib_collector_up                              — 1 if IB detected, 0 otherwise
    ib_collector_last_run_timestamp              — Unix timestamp of last run

Usage:
    python3 network/ib-collector.py [--output PATH]
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_OUTPUT = Path("/var/lib/node_exporter/textfile_collector/ib_collector.prom")

IB_SYSFS_ROOT = "/sys/class/infiniband"

# Counters extracted from perfquery output
PERFQUERY_COUNTERS = [
    "SymbolErrorCounter",
    "LinkRecovers",
    "LinkDowned",
    "PortRcvErrors",
    "PortXmtDiscards",
    "VL15Dropped",
]


def _run(cmd: list, timeout: int = 10) -> tuple:  # pragma: no cover
    """Run a subprocess. Returns (stdout, returncode). Never raises on bad exit."""
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return result.stdout.decode("utf-8", errors="replace"), result.returncode
    except FileNotFoundError:
        return "", -1
    except subprocess.TimeoutExpired:
        return "", -2


def detect_ib() -> bool:
    """Return True if InfiniBand devices are present in sysfs."""
    if not os.path.isdir(IB_SYSFS_ROOT):
        return False
    try:
        entries = os.listdir(IB_SYSFS_ROOT)
    except OSError:
        return False
    return len(entries) > 0


def list_ib_devices() -> list:
    """Return list of IB device names from /sys/class/infiniband/."""
    try:
        return sorted(os.listdir(IB_SYSFS_ROOT))
    except OSError:
        return []


def parse_ibstat(output: str) -> dict:
    """
    Parse ibstat output for one or more CAs.

    Returns dict keyed by device name:
        {
          "mlx5_0": {
            "ca_type": "MT4119",
            "num_ports": 1,
            "ports": {
              "1": {"state": "Active", "phys_state": "LinkUp", "rate_gbps": 100}
            }
          }
        }
    """
    devices = {}
    current_device = None
    current_port = None

    for line in output.splitlines():
        # Match CA header: CA 'mlx5_0'
        m = re.match(r"^CA '(.+)'", line)
        if m:
            current_device = m.group(1)
            current_port = None
            devices[current_device] = {"ca_type": "", "num_ports": 0, "ports": {}}
            continue

        if current_device is None:
            continue

        stripped = line.strip()

        # CA type
        m = re.match(r"CA type:\s*(.+)", stripped)
        if m:
            devices[current_device]["ca_type"] = m.group(1).strip()
            continue

        # Number of ports
        m = re.match(r"Number of ports:\s*(\d+)", stripped)
        if m:
            devices[current_device]["num_ports"] = int(m.group(1))
            continue

        # Port header: Port 1:
        m = re.match(r"Port (\d+):", stripped)
        if m:
            current_port = m.group(1)
            devices[current_device]["ports"][current_port] = {
                "state": "Unknown",
                "phys_state": "Unknown",
                "rate_gbps": 0,
            }
            continue

        if current_port is None:
            continue

        port_data = devices[current_device]["ports"][current_port]

        # State
        m = re.match(r"State:\s*(.+)", stripped)
        if m:
            port_data["state"] = m.group(1).strip()
            continue

        # Physical state
        m = re.match(r"Physical state:\s*(.+)", stripped)
        if m:
            port_data["phys_state"] = m.group(1).strip()
            continue

        # Rate: "100 Gb/sec" → 100
        m = re.match(r"Rate:\s*(\d+)\s*Gb/sec", stripped)
        if m:
            port_data["rate_gbps"] = int(m.group(1))
            continue

    return devices


def parse_perfquery(output: str) -> dict:
    """
    Parse perfquery output.

    Returns dict of counter_name → int for the counters listed in
    PERFQUERY_COUNTERS. Missing counters default to 0.
    """
    result = {c: 0 for c in PERFQUERY_COUNTERS}
    for line in output.splitlines():
        for counter in PERFQUERY_COUNTERS:
            # Lines look like: "SymbolErrorCounter:......................0"
            if line.startswith(counter + ":"):
                value_str = line.split(":", 1)[1].strip().lstrip(".")
                try:
                    result[counter] = int(value_str)
                except ValueError:
                    pass
    return result


def collect_device_metrics(device: str) -> dict:  # pragma: no cover
    """
    Run ibstat for a device and perfquery for each active port.

    Returns:
        {
          "ibstat_ok": bool,
          "ports": {
            "1": {
              "state": str,
              "rate_gbps": int,
              "counters": {counter_name: int, ...}
            }
          }
        }
    """
    stdout, rc = _run(["ibstat", device])
    if rc != 0:
        return {"ibstat_ok": False, "ports": {}}

    parsed = parse_ibstat(stdout)
    if device not in parsed:
        return {"ibstat_ok": False, "ports": {}}

    dev_info = parsed[device]
    result = {"ibstat_ok": True, "ports": {}}

    for port_num, port_data in dev_info["ports"].items():
        port_entry = {
            "state": port_data["state"],
            "rate_gbps": port_data["rate_gbps"],
            "counters": {},
        }

        if port_data["state"] == "Active":
            pq_out, pq_rc = _run(["perfquery", "-x", device, port_num])
            if pq_rc == 0:
                port_entry["counters"] = parse_perfquery(pq_out)
            # If perfquery is absent/fails, counters remain empty dict

        result["ports"][port_num] = port_entry

    return result


def emit_metrics(
    ib_present: bool,
    device_metrics: dict,
    now: float,
) -> str:
    """Render all IB metrics in Prometheus text format."""
    lines = []

    lines += [
        "# HELP ib_collector_up 1 if InfiniBand hardware detected and collector operational, 0 otherwise.",
        "# TYPE ib_collector_up gauge",
        f"ib_collector_up {1 if ib_present else 0}",
    ]

    if ib_present:
        lines += [
            "# HELP ib_port_state 1 if port state is Active, 0 otherwise.",
            "# TYPE ib_port_state gauge",
        ]
        for device, info in sorted(device_metrics.items()):
            for port, port_data in sorted(info["ports"].items()):
                state_val = 1 if port_data["state"] == "Active" else 0
                lines.append(
                    f'ib_port_state{{device="{device}",port="{port}"}} {state_val}'
                )

        lines += [
            "# HELP ib_port_link_rate_gbps InfiniBand port link rate in Gbps.",
            "# TYPE ib_port_link_rate_gbps gauge",
        ]
        for device, info in sorted(device_metrics.items()):
            for port, port_data in sorted(info["ports"].items()):
                lines.append(
                    f'ib_port_link_rate_gbps{{device="{device}",port="{port}"}} {port_data["rate_gbps"]}'
                )

        lines += [
            "# HELP ib_port_error_total InfiniBand port error counter totals.",
            "# TYPE ib_port_error_total counter",
        ]
        for device, info in sorted(device_metrics.items()):
            for port, port_data in sorted(info["ports"].items()):
                for counter_name, counter_val in sorted(port_data["counters"].items()):
                    lines.append(
                        f'ib_port_error_total{{device="{device}",port="{port}",counter="{counter_name}"}} {counter_val}'
                    )

    lines += [
        "# HELP ib_collector_last_run_timestamp Unix timestamp of the last IB collector run.",
        "# TYPE ib_collector_last_run_timestamp gauge",
        f"ib_collector_last_run_timestamp {now:.3f}",
    ]

    return "\n".join(lines) + "\n"


def main():  # pragma: no cover
    parser = argparse.ArgumentParser(
        description="Collect InfiniBand port error metrics and emit Prometheus textfile output."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output .prom file (- for stdout)",
    )
    args = parser.parse_args()

    now = time.time()

    if not detect_ib():
        sys.stderr.write("ib-collector: no InfiniBand devices detected, emitting collector_up=0\n")
        output = emit_metrics(False, {}, now)
    else:
        devices = list_ib_devices()
        sys.stderr.write(f"ib-collector: detected IB devices: {devices}\n")

        device_metrics = {}
        collector_up = True
        for device in devices:
            info = collect_device_metrics(device)
            if not info["ibstat_ok"]:
                collector_up = False
            device_metrics[device] = info

        output = emit_metrics(collector_up, device_metrics, now)

    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        sys.stderr.write(f"ib-collector: wrote to {args.output}\n")


if __name__ == "__main__":  # pragma: no cover
    main()
