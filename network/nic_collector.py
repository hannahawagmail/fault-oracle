#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
network/nic-collector.py — Collect Ethernet/RoCE NIC sysfs error statistics.

Emits Prometheus textfile metrics to stdout or --output file. Degrades
gracefully when ethtool is absent or RoCE counters are unavailable.

Metrics emitted:
    nic_errors_total{interface,direction,error_type}  — NIC error counters
    roce_errors_total{interface,counter}              — RoCE-specific error counters
    nic_collector_up                                  — 1 if interfaces found, 0 otherwise
    nic_collector_last_run_timestamp                  — Unix timestamp of last run

Usage:
    python3 network/nic-collector.py [--output PATH]
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_OUTPUT = Path("/var/lib/node_exporter/textfile_collector/nic_collector.prom")

NET_SYSFS_ROOT = "/sys/class/net"

# sysfs statistics counters to collect, split by direction
STATS_RX = ["rx_errors", "rx_dropped", "rx_crc_errors", "rx_missed_errors"]
STATS_TX = ["tx_errors", "tx_dropped"]

# RoCE driver names (matched as substrings in driver path)
ROCE_DRIVERS = ["mlx5_core", "mlx4_core"]

# ethtool counter names relevant to RoCE
ETHTOOL_ROCE_COUNTERS = ["out_of_buffer", "req_rnr_retry_exceeded"]


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


def list_physical_interfaces() -> list:
    """
    Return sorted list of physical (non-loopback, non-virtual) interface names.

    An interface is considered physical if:
      - It is not 'lo'
      - /sys/class/net/$IFACE/device exists
    """
    try:
        all_ifaces = os.listdir(NET_SYSFS_ROOT)
    except OSError:
        return []

    physical = []
    for iface in sorted(all_ifaces):
        if iface == "lo":
            continue
        device_path = os.path.join(NET_SYSFS_ROOT, iface, "device")
        if os.path.exists(device_path):
            physical.append(iface)
    return physical


def read_sysfs_stat(iface: str, stat: str) -> int:
    """Read a single integer from /sys/class/net/$IFACE/statistics/$STAT."""
    path = os.path.join(NET_SYSFS_ROOT, iface, "statistics", stat)
    try:
        with open(path, "r") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def is_roce_interface(iface: str) -> bool:
    """
    Return True if the interface uses a RoCE-capable driver (mlx5_core or mlx4_core).

    Checks the resolved driver symlink path:
      /sys/class/net/$IFACE/device/driver → contains mlx5_core or mlx4_core
    """
    driver_link = os.path.join(NET_SYSFS_ROOT, iface, "device", "driver")
    try:
        target = os.readlink(driver_link)
        driver_name = os.path.basename(target)
        return any(roce in driver_name for roce in ROCE_DRIVERS)
    except OSError:
        return False


def collect_ethtool_roce(iface: str) -> dict:  # pragma: no cover
    """
    Run ethtool -S $IFACE and extract RoCE-relevant counters.

    Returns dict of counter_name → int. Absent binary or non-zero exit
    returns empty dict gracefully.
    """
    stdout, rc = _run(["ethtool", "-S", iface])
    if rc != 0:
        return {}

    result = {}
    for line in stdout.splitlines():
        stripped = line.strip()
        for counter in ETHTOOL_ROCE_COUNTERS:
            if counter in stripped:
                # Lines look like: "     out_of_buffer: 42"
                m = re.search(r":\s*(\d+)", stripped)
                if m:
                    result[counter] = int(m.group(1))
    return result


def collect_interface_stats(iface: str) -> dict:
    """
    Collect all stats for a single interface.

    Returns:
        {
          "rx": {"errors": int, "dropped": int, "crc_errors": int, "missed_errors": int},
          "tx": {"errors": int, "dropped": int},
          "is_roce": bool,
          "roce_counters": {counter_name: int, ...}
        }
    """
    rx_stats = {
        "errors": read_sysfs_stat(iface, "rx_errors"),
        "dropped": read_sysfs_stat(iface, "rx_dropped"),
        "crc_errors": read_sysfs_stat(iface, "rx_crc_errors"),
        "missed_errors": read_sysfs_stat(iface, "rx_missed_errors"),
    }
    tx_stats = {
        "errors": read_sysfs_stat(iface, "tx_errors"),
        "dropped": read_sysfs_stat(iface, "tx_dropped"),
    }

    roce = is_roce_interface(iface)
    roce_counters = collect_ethtool_roce(iface) if roce else {}

    return {
        "rx": rx_stats,
        "tx": tx_stats,
        "is_roce": roce,
        "roce_counters": roce_counters,
    }


def emit_metrics(
    ifaces_found: bool,
    iface_stats: dict,
    now: float,
) -> str:
    """Render all NIC metrics in Prometheus text format."""
    lines = []

    lines += [
        "# HELP nic_collector_up 1 if physical NIC interfaces found and collector operational, 0 otherwise.",
        "# TYPE nic_collector_up gauge",
        f"nic_collector_up {1 if ifaces_found else 0}",
    ]

    if ifaces_found:
        lines += [
            "# HELP nic_errors_total NIC error counters from sysfs statistics.",
            "# TYPE nic_errors_total counter",
        ]
        for iface, stats in sorted(iface_stats.items()):
            rx = stats["rx"]
            tx = stats["tx"]
            # RX counters
            for error_type, val in sorted(rx.items()):
                lines.append(
                    f'nic_errors_total{{interface="{iface}",direction="rx",error_type="{error_type}"}} {val}'
                )
            # TX counters
            for error_type, val in sorted(tx.items()):
                lines.append(
                    f'nic_errors_total{{interface="{iface}",direction="tx",error_type="{error_type}"}} {val}'
                )

        # RoCE metrics (only for RoCE interfaces that had ethtool data)
        roce_entries = [
            (iface, stats["roce_counters"])
            for iface, stats in sorted(iface_stats.items())
            if stats["is_roce"] and stats["roce_counters"]
        ]
        if roce_entries:
            lines += [
                "# HELP roce_errors_total RoCE-specific error counters from ethtool.",
                "# TYPE roce_errors_total counter",
            ]
            for iface, counters in roce_entries:
                for counter_name, val in sorted(counters.items()):
                    lines.append(
                        f'roce_errors_total{{interface="{iface}",counter="{counter_name}"}} {val}'
                    )

    lines += [
        "# HELP nic_collector_last_run_timestamp Unix timestamp of the last NIC collector run.",
        "# TYPE nic_collector_last_run_timestamp gauge",
        f"nic_collector_last_run_timestamp {now:.3f}",
    ]

    return "\n".join(lines) + "\n"


def main():  # pragma: no cover
    parser = argparse.ArgumentParser(
        description="Collect NIC sysfs error metrics and emit Prometheus textfile output."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output .prom file (- for stdout)",
    )
    args = parser.parse_args()

    now = time.time()
    interfaces = list_physical_interfaces()

    if not interfaces:
        sys.stderr.write("nic-collector: no physical NIC interfaces found, emitting collector_up=0\n")
        output = emit_metrics(False, {}, now)
    else:
        sys.stderr.write(f"nic-collector: collecting stats for interfaces: {interfaces}\n")
        iface_stats = {}
        for iface in interfaces:
            iface_stats[iface] = collect_interface_stats(iface)
        output = emit_metrics(True, iface_stats, now)

    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        sys.stderr.write(f"nic-collector: wrote to {args.output}\n")


if __name__ == "__main__":  # pragma: no cover
    main()
