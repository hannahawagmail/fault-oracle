#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
storage/sata-collector.py — Collect SATA/SAS SMART health data via smartctl.

Enumerates /sys/class/block/ for sd* devices, reads rotational flag, runs
`smartctl -A -j` and `smartctl -H -j` for each device, and emits Prometheus
textfile metrics.

Degrades gracefully when smartctl is absent.

Usage:
    python3 storage/sata-collector.py [--output PATH] [--sys-block-path PATH]

Writes to: /var/lib/node_exporter/textfile_collector/sata.prom (default)

Metrics:
    sata_smart_attribute{device, attribute_name}  — raw SMART attribute value
    sata_smart_passed{device}                     — 1=PASSED, 0=FAILED/unknown
    sata_rotational{device}                       — 1=HDD, 0=SSD
    sata_collector_up                             — 1 if devices found + smartctl present
    sata_collector_last_run_timestamp             — Unix timestamp of last run
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_OUTPUT    = Path("/var/lib/node_exporter/textfile_collector/sata.prom")
DEFAULT_SYS_BLOCK = Path("/sys/class/block")
SMARTCTL          = "smartctl"

# SMART attribute IDs we care about
SMART_ATTR_IDS = {
    5:   "reallocated_sector_ct",
    9:   "power_on_hours",
    187: "reported_uncorrect",
    190: "airflow_temperature_cel",
    194: "temperature_celsius",
    197: "current_pending_sector",
    198: "offline_uncorrectable",
    231: "ssd_life_left",
    233: "media_wearout_indicator",
}

# Attribute IDs that only apply to SSDs
SSD_ONLY_ATTR_IDS = {231, 233}


def find_sata_devices(sys_block_path: Path) -> list:
    """Return sorted list of sd* block device names."""
    try:
        entries = sorted(
            e.name for e in sys_block_path.iterdir()
            if e.name.startswith("sd")
        )
        return entries
    except (OSError, PermissionError):
        return []


def read_rotational(sys_block_path: Path, device: str) -> int:
    """
    Return 1 if the device is rotational (HDD), 0 if not (SSD).
    Returns 1 as default on read error (assume HDD).
    """
    rotational_path = sys_block_path / device / "queue" / "rotational"
    try:
        return int(rotational_path.read_text().strip())
    except (OSError, ValueError):
        return 1  # default to HDD


def run_command(cmd: list) -> str:  # pragma: no cover
    """Run a command and return stdout. Returns empty string on failure."""
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        return result.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def parse_smart_attributes(raw_json: str, is_rotational: bool) -> dict:
    """
    Parse `smartctl -A -j` JSON output.

    Returns a dict mapping attribute_name -> raw value (int).
    Only includes attributes present in the SMART_ATTR_IDS mapping.
    SSD-only attributes (231, 233) are skipped for HDDs.
    """
    attributes = {}
    if not raw_json:
        return attributes

    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return attributes

    ata_smart = data.get("ata_smart_attributes", {})
    attr_list = ata_smart.get("table", [])

    for attr in attr_list:
        attr_id = attr.get("id")
        if attr_id not in SMART_ATTR_IDS:
            continue
        # Skip SSD-only attributes for spinning disks
        if is_rotational is not False and attr_id in SSD_ONLY_ATTR_IDS and is_rotational:
            continue
        attr_name = SMART_ATTR_IDS[attr_id]
        raw_val = attr.get("raw", {})
        if isinstance(raw_val, dict):
            value = raw_val.get("value")
        else:
            value = raw_val
        try:
            attributes[attr_name] = int(value)
        except (TypeError, ValueError):
            pass

    return attributes


def parse_smart_health(raw_json: str) -> int:
    """
    Parse `smartctl -H -j` JSON output.

    Returns 1 if overall SMART assessment is PASSED, 0 otherwise.
    """
    if not raw_json:
        return 0
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return 0

    smart_status = data.get("smart_status", {})
    passed = smart_status.get("passed")
    return 1 if passed is True else 0


def collect_device(device: str, sys_block_path: Path) -> dict:  # pragma: no cover
    """Collect all SMART data for a single SATA/SAS device."""
    dev_path     = f"/dev/{device}"
    is_rotational = read_rotational(sys_block_path, device)

    attrs_raw  = run_command([SMARTCTL, "-A", "-j", dev_path])
    health_raw = run_command([SMARTCTL, "-H", "-j", dev_path])

    attributes = parse_smart_attributes(attrs_raw, bool(is_rotational))
    health     = parse_smart_health(health_raw)

    return {
        "device":       device,
        "attributes":   attributes,
        "smart_passed": health,
        "rotational":   is_rotational,
    }


def emit_metrics(device_results: list, collector_up: int) -> str:
    """Render all collected data as Prometheus textfile format."""
    lines = []
    now = time.time()

    # --- SMART attributes ---
    lines.append("# HELP sata_smart_attribute SATA/SAS SMART attribute raw value.")
    lines.append("# TYPE sata_smart_attribute gauge")
    for r in device_results:
        for attr_name, value in sorted(r["attributes"].items()):
            lines.append(
                f'sata_smart_attribute{{device="{r["device"]}",attribute_name="{attr_name}"}} {value}'
            )

    # --- overall SMART health pass/fail ---
    lines.append("# HELP sata_smart_passed SATA/SAS SMART overall health (1=PASSED, 0=FAILED).")
    lines.append("# TYPE sata_smart_passed gauge")
    for r in device_results:
        lines.append(f'sata_smart_passed{{device="{r["device"]}"}} {r["smart_passed"]}')

    # --- rotational flag ---
    lines.append("# HELP sata_rotational 1 if the device is a spinning disk (HDD), 0 for SSD.")
    lines.append("# TYPE sata_rotational gauge")
    for r in device_results:
        lines.append(f'sata_rotational{{device="{r["device"]}"}} {r["rotational"]}')

    # --- collector up ---
    lines.append("# HELP sata_collector_up 1 if SATA devices found and smartctl is present.")
    lines.append("# TYPE sata_collector_up gauge")
    lines.append(f"sata_collector_up {collector_up}")

    # --- last run timestamp ---
    lines.append("# HELP sata_collector_last_run_timestamp Unix timestamp of last collector run.")
    lines.append("# TYPE sata_collector_last_run_timestamp gauge")
    lines.append(f"sata_collector_last_run_timestamp {now:.3f}")

    return "\n".join(lines) + "\n"


def main():  # pragma: no cover
    parser = argparse.ArgumentParser(
        description="Collect SATA/SAS SMART metrics and emit Prometheus textfile format."
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help="Output .prom file path (- for stdout)"
    )
    parser.add_argument(
        "--sys-block-path", type=Path, default=DEFAULT_SYS_BLOCK,
        help="Path to /sys/class/block (for testing)"
    )
    args = parser.parse_args()

    # Check smartctl availability
    smartctl_present = shutil.which(SMARTCTL) is not None

    devices = find_sata_devices(args.sys_block_path)

    if not smartctl_present or not devices:
        if not smartctl_present:
            sys.stderr.write("sata-collector: smartctl not found, emitting collector_up=0\n")
        else:
            sys.stderr.write("sata-collector: no SATA/SAS devices found, emitting collector_up=0\n")
        output = emit_metrics([], collector_up=0)
    else:
        device_results = []
        for device in devices:
            try:
                result = collect_device(device, args.sys_block_path)
                device_results.append(result)
            except Exception as exc:
                sys.stderr.write(f"sata-collector: error collecting {device}: {exc}\n")

        collector_up = 1 if device_results else 0
        output = emit_metrics(device_results, collector_up=collector_up)

    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        sys.stderr.write(f"sata-collector: wrote {len(output)} bytes to {args.output}\n")


if __name__ == "__main__":  # pragma: no cover
    main()
