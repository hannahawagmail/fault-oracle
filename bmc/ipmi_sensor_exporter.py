#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
bmc/ipmi-sensor-exporter.py — Out-of-band IPMI SDR sensor value exporter.

Runs from a management-plane Deployment targeting each node's BMC IP.
Reads sensor readings (fans, temperatures, voltages, PSU power) via:
    ipmitool -H $IP -U $USER -P $PASS -I lanplus sdr list full

SDR output line format (pipe-separated):
    Sensor Name  | 0xhh | Status | <value> <unit>

Example lines:
    Fan1 RPM     | 0x01 | ok | 1200 RPM
    CPU Temp     | 0x02 | ok | 45 degrees C
    VCore        | 0x03 | ok | 0.88 Volts
    PS1 Input    | 0x04 | ok | 120 Watts

Threshold breach is detected when Status is "Upper Critical" or "Lower Critical".

Emits Prometheus textfile format to --output (default stdout).

Metrics:
    ipmi_fan_rpm{node, sensor_name}               gauge
    ipmi_temperature_celsius{node, sensor_name}   gauge
    ipmi_voltage_volts{node, sensor_name}         gauge
    ipmi_power_watts{node, sensor_name}           gauge
    ipmi_sensor_threshold_breach{node, sensor_name} gauge (0 normal, 1 breach)
    ipmi_sensor_collector_last_run_timestamp      gauge
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT = Path("-")  # stdout

# Status values that indicate a threshold breach.
BREACH_STATUSES = {"upper critical", "lower critical"}

# Maps a unit substring → (metric_name_suffix, canonical_unit_label)
UNIT_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bRPM\b",       re.IGNORECASE), "fan_rpm"),
    (re.compile(r"\bdegrees\s+C\b", re.IGNORECASE), "temperature_celsius"),
    (re.compile(r"\bVolts?\b",    re.IGNORECASE), "voltage_volts"),
    (re.compile(r"\bWatts?\b",    re.IGNORECASE), "power_watts"),
]


# ---------------------------------------------------------------------------
# Label helper
# ---------------------------------------------------------------------------

def sanitize_label(val: str) -> str:
    """Remove characters that are invalid in Prometheus label values."""
    return val.replace('"', "'").replace('\\', '/').replace('\n', ' ').strip() or "unknown"


# ---------------------------------------------------------------------------
# SDR parsing
# ---------------------------------------------------------------------------

def parse_sdr_output(raw: str) -> list[dict[str, Any]]:
    """
    Parse ipmitool sdr list full output.

    Each line has the form:
        Sensor Name  | 0xhh | Status | <value> <unit>

    Fields beyond the 4th pipe segment are ignored (ipmitool may append extra
    columns on some firmware versions).

    Returns a list of dicts with keys:
        sensor_name (str), status (str), value (float | None),
        unit_type (str | None),  breach (bool)

    Lines that cannot be parsed (wrong column count, non-numeric value) are
    skipped with a warning to stderr.
    """
    sensors: list[dict[str, Any]] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            sys.stderr.write(
                f"ipmi-sensor-exporter: skipping line {lineno} (too few fields): {line!r}\n"
            )
            continue

        sensor_name = parts[0]
        status_raw  = parts[2].lower()
        reading_str = parts[3]   # e.g. "1200 RPM" or "45 degrees C" or "No Reading"

        breach = status_raw in BREACH_STATUSES

        # Determine unit type and parse numeric value from the reading field.
        value: float | None    = None
        unit_type: str | None  = None

        # Try to extract a leading number from the reading string.
        m = re.match(r"^\s*([\d.]+)\s*(.*)", reading_str)
        if m:
            try:
                value = float(m.group(1))
            except ValueError:
                value = None
            unit_str = m.group(2).strip()
            for pattern, utype in UNIT_MAP:
                if pattern.search(unit_str):
                    unit_type = utype
                    break

        if value is None or unit_type is None:
            # Sensor has no numeric reading or unknown unit — skip silently.
            continue

        sensors.append({
            "sensor_name": sensor_name,
            "status":      status_raw,
            "value":       value,
            "unit_type":   unit_type,
            "breach":      breach,
        })
    return sensors


# ---------------------------------------------------------------------------
# ipmitool invocation
# ---------------------------------------------------------------------------

def run_ipmitool_sdr(ip: str, user: str, password: str,  # pragma: no cover
                     timeout: int = 60) -> tuple[str | None, str | None]:
    """
    Run ipmitool sdr list full for a single BMC target.

    Returns (stdout_text, None) on success, (None, error_string) on failure.
    """
    cmd = [
        "ipmitool",
        "-H", ip,
        "-U", user,
        "-P", password,
        "-I", "lanplus",
        "sdr", "list", "full",
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

def collect_node_sensors(
    target: dict,
    run_ipmitool_fn=run_ipmitool_sdr,
) -> dict[str, Any]:
    """
    Collect SDR sensor readings for one node.

    Returns:
        node      str
        up        bool
        error     str | None
        sensors   list of parsed sensor dicts
    """
    node = target["node"]
    raw, error = run_ipmitool_fn(target["ip"], target["user"], target["pass"])
    if error:
        return {"node": node, "up": False, "error": error, "sensors": []}
    sensors = parse_sdr_output(raw or "")
    return {"node": node, "up": True, "error": None, "sensors": sensors}


# ---------------------------------------------------------------------------
# Prometheus output
# ---------------------------------------------------------------------------

# Mapping from unit_type value to (metric_name, help_text)
METRIC_DEFS: dict[str, tuple[str, str]] = {
    "fan_rpm": (
        "ipmi_fan_rpm",
        "Fan speed in RPM reported by IPMI SDR.",
    ),
    "temperature_celsius": (
        "ipmi_temperature_celsius",
        "Temperature in degrees Celsius reported by IPMI SDR.",
    ),
    "voltage_volts": (
        "ipmi_voltage_volts",
        "Voltage in Volts reported by IPMI SDR.",
    ),
    "power_watts": (
        "ipmi_power_watts",
        "Power in Watts reported by IPMI SDR.",
    ),
}


def emit_metrics(results: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    now = time.time()

    # Group sensor readings by unit_type for clean Prometheus blocks.
    for unit_type, (metric_name, help_text) in METRIC_DEFS.items():
        lines.append(f"# HELP {metric_name} {help_text}")
        lines.append(f"# TYPE {metric_name} gauge")
        for r in results:
            node = sanitize_label(r["node"])
            for s in r["sensors"]:
                if s["unit_type"] != unit_type:
                    continue
                sname = sanitize_label(s["sensor_name"])
                lines.append(
                    f'{metric_name}{{node="{node}",sensor_name="{sname}"}} {s["value"]}'
                )

    # ---- ipmi_sensor_threshold_breach ----------------------------------------
    lines.append("# HELP ipmi_sensor_threshold_breach 1 if sensor is at Upper/Lower Critical threshold.")
    lines.append("# TYPE ipmi_sensor_threshold_breach gauge")
    for r in results:
        node = sanitize_label(r["node"])
        for s in r["sensors"]:
            sname = sanitize_label(s["sensor_name"])
            val   = 1 if s["breach"] else 0
            lines.append(
                f'ipmi_sensor_threshold_breach{{node="{node}",sensor_name="{sname}"}} {val}'
            )

    # ---- ipmi_sensor_collector_last_run_timestamp ----------------------------
    lines.append("# HELP ipmi_sensor_collector_last_run_timestamp Unix timestamp of last sensor collector run.")
    lines.append("# TYPE ipmi_sensor_collector_last_run_timestamp gauge")
    lines.append(f"ipmi_sensor_collector_last_run_timestamp {now:.3f}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv=None):  # pragma: no cover
    parser = argparse.ArgumentParser(
        description="IPMI SDR sensor exporter — emits Prometheus textfile metrics."
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help="Output .prom file path, or '-' for stdout (default: stdout)",
    )
    parser.add_argument(
        "--timeout", type=int, default=60,
        help="ipmitool per-target timeout in seconds (default: 60)",
    )
    args = parser.parse_args(argv)

    raw_targets = os.environ.get("BMC_TARGETS", "[]")
    try:
        targets: list[dict] = json.loads(raw_targets)
    except Exception as exc:
        sys.stderr.write(f"ipmi-sensor-exporter: invalid BMC_TARGETS JSON: {exc}\n")
        targets = []

    ipmitool_present = shutil.which("ipmitool") is not None
    results: list[dict[str, Any]] = []

    for target in targets:
        node = target.get("node", "unknown")
        if not ipmitool_present:
            results.append({"node": node, "up": False, "error": "ipmitool not found", "sensors": []})
            continue
        results.append(collect_node_sensors(target))

    output = emit_metrics(results)
    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        sys.stderr.write(
            f"ipmi-sensor-exporter: wrote {len(output)} bytes to {args.output}\n"
        )


if __name__ == "__main__":  # pragma: no cover
    main()
