#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
storage/nvme-collector.py — Collect NVMe SMART health data via nvme-cli.

Enumerates /sys/class/nvme/, runs `nvme smart-log` and `nvme error-log` for
each device, and emits Prometheus textfile metrics.

Degrades gracefully when nvme-cli is absent or no NVMe devices are present.

Usage:
    python3 storage/nvme-collector.py [--output PATH] [--sys-nvme-path PATH]

Writes to: /var/lib/node_exporter/textfile_collector/nvme.prom (default)

Metrics:
    nvme_smart_critical_warning{device}              — critical warning bitmask
    nvme_smart_temperature_celsius{device}           — temperature in Celsius
    nvme_smart_available_spare_percent{device}       — available spare (%)
    nvme_smart_available_spare_threshold_percent{device} — spare threshold (%)
    nvme_smart_percentage_used{device}               — wear percentage
    nvme_smart_data_units_written_total{device}      — data units written
    nvme_smart_power_on_hours_total{device}          — power-on hours
    nvme_smart_unsafe_shutdowns_total{device}        — unsafe shutdowns
    nvme_smart_media_errors_total{device}            — media errors
    nvme_error_log_entries_total{device}             — error log entry count
    nvme_collector_up                                — 1 if any NVMe found
    nvme_collector_last_run_timestamp                — Unix timestamp of last run
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_OUTPUT       = Path("/var/lib/node_exporter/textfile_collector/nvme.prom")
DEFAULT_SYS_NVME     = Path("/sys/class/nvme")
NVME_CLI             = "nvme"

# Kelvin to Celsius offset
KELVIN_OFFSET = 273


def find_nvme_devices(sys_nvme_path: Path) -> list:
    """Return sorted list of NVMe device names (e.g. ['nvme0', 'nvme1'])."""
    try:
        entries = sorted(
            e.name for e in sys_nvme_path.iterdir()
            if e.name.startswith("nvme")
        )
        return entries
    except (OSError, PermissionError):
        return []


def run_command(cmd: list) -> str:
    """Run a shell command and return stdout. Returns empty string on failure."""
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return result.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def parse_smart_log(raw_json: str) -> dict:
    """
    Parse nvme smart-log JSON output.

    Returns a dict with the following keys (all may be None if absent):
        critical_warning, temperature_celsius, available_spare,
        available_spare_threshold, percentage_used, data_units_written,
        power_on_hours, unsafe_shutdowns, media_errors, num_err_log_entries
    """
    defaults = {
        "critical_warning": None,
        "temperature_celsius": None,
        "available_spare": None,
        "available_spare_threshold": None,
        "percentage_used": None,
        "data_units_written": None,
        "power_on_hours": None,
        "unsafe_shutdowns": None,
        "media_errors": None,
        "num_err_log_entries": None,
    }
    if not raw_json:
        return defaults

    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return defaults

    def _get(key):
        val = data.get(key)
        if val is None:
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    result = dict(defaults)
    result["critical_warning"]          = _get("critical_warning")
    result["available_spare"]           = _get("avail_spare")
    result["available_spare_threshold"] = _get("spare_thresh")
    result["percentage_used"]           = _get("percent_used")
    result["data_units_written"]        = _get("data_units_written")
    result["power_on_hours"]            = _get("power_on_hours")
    result["unsafe_shutdowns"]          = _get("unsafe_shutdowns")
    result["media_errors"]              = _get("media_errors")
    result["num_err_log_entries"]       = _get("num_err_log_entries")

    # Temperature: nvme-cli reports in Kelvin; convert to Celsius
    temp_k = _get("temperature")
    if temp_k is not None:
        result["temperature_celsius"] = temp_k - KELVIN_OFFSET

    return result


def count_error_log_entries(raw_json: str) -> int:
    """
    Parse nvme error-log JSON output and return the number of entries.

    nvme-cli returns either a list or {"errors": [...]} depending on version.
    Returns 0 on parse failure or empty log.
    """
    if not raw_json:
        return 0
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return 0

    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        entries = data.get("errors") or data.get("error_log") or []
        if isinstance(entries, list):
            return len(entries)
    return 0


def collect_device(device: str) -> dict:
    """
    Collect SMART data for a single NVMe device.

    Returns a dict with 'smart' (parsed fields) and 'error_log_count' (int).
    """
    dev_path = f"/dev/{device}"
    smart_raw  = run_command([NVME_CLI, "smart-log", dev_path, "--output-format=json"])
    errlog_raw = run_command([NVME_CLI, "error-log", dev_path, "--output-format=json"])

    smart  = parse_smart_log(smart_raw)
    errlog = count_error_log_entries(errlog_raw)

    return {"device": device, "smart": smart, "error_log_count": errlog}


def emit_metrics(device_results: list, collector_up: int) -> str:
    """Render all collected data as Prometheus textfile format."""
    lines = []
    now = time.time()

    # --- critical_warning ---
    lines.append("# HELP nvme_smart_critical_warning NVMe SMART critical warning bitmask.")
    lines.append("# TYPE nvme_smart_critical_warning gauge")
    for r in device_results:
        val = r["smart"]["critical_warning"]
        if val is not None:
            lines.append(f'nvme_smart_critical_warning{{device="{r["device"]}"}} {val}')

    # --- temperature ---
    lines.append("# HELP nvme_smart_temperature_celsius NVMe drive temperature in Celsius.")
    lines.append("# TYPE nvme_smart_temperature_celsius gauge")
    for r in device_results:
        val = r["smart"]["temperature_celsius"]
        if val is not None:
            lines.append(f'nvme_smart_temperature_celsius{{device="{r["device"]}"}} {val}')

    # --- available spare ---
    lines.append("# HELP nvme_smart_available_spare_percent NVMe available spare capacity (%).")
    lines.append("# TYPE nvme_smart_available_spare_percent gauge")
    for r in device_results:
        val = r["smart"]["available_spare"]
        if val is not None:
            lines.append(f'nvme_smart_available_spare_percent{{device="{r["device"]}"}} {val}')

    # --- available spare threshold ---
    lines.append("# HELP nvme_smart_available_spare_threshold_percent NVMe available spare threshold (%).")
    lines.append("# TYPE nvme_smart_available_spare_threshold_percent gauge")
    for r in device_results:
        val = r["smart"]["available_spare_threshold"]
        if val is not None:
            lines.append(f'nvme_smart_available_spare_threshold_percent{{device="{r["device"]}"}} {val}')

    # --- percentage used (wear) ---
    lines.append("# HELP nvme_smart_percentage_used NVMe drive wear percentage (100 = fully worn).")
    lines.append("# TYPE nvme_smart_percentage_used gauge")
    for r in device_results:
        val = r["smart"]["percentage_used"]
        if val is not None:
            lines.append(f'nvme_smart_percentage_used{{device="{r["device"]}"}} {val}')

    # --- data units written ---
    lines.append("# HELP nvme_smart_data_units_written_total NVMe cumulative data units written.")
    lines.append("# TYPE nvme_smart_data_units_written_total counter")
    for r in device_results:
        val = r["smart"]["data_units_written"]
        if val is not None:
            lines.append(f'nvme_smart_data_units_written_total{{device="{r["device"]}"}} {val}')

    # --- power on hours ---
    lines.append("# HELP nvme_smart_power_on_hours_total NVMe cumulative power-on hours.")
    lines.append("# TYPE nvme_smart_power_on_hours_total counter")
    for r in device_results:
        val = r["smart"]["power_on_hours"]
        if val is not None:
            lines.append(f'nvme_smart_power_on_hours_total{{device="{r["device"]}"}} {val}')

    # --- unsafe shutdowns ---
    lines.append("# HELP nvme_smart_unsafe_shutdowns_total NVMe cumulative unsafe shutdowns.")
    lines.append("# TYPE nvme_smart_unsafe_shutdowns_total counter")
    for r in device_results:
        val = r["smart"]["unsafe_shutdowns"]
        if val is not None:
            lines.append(f'nvme_smart_unsafe_shutdowns_total{{device="{r["device"]}"}} {val}')

    # --- media errors ---
    lines.append("# HELP nvme_smart_media_errors_total NVMe cumulative media errors.")
    lines.append("# TYPE nvme_smart_media_errors_total counter")
    for r in device_results:
        val = r["smart"]["media_errors"]
        if val is not None:
            lines.append(f'nvme_smart_media_errors_total{{device="{r["device"]}"}} {val}')

    # --- error log entries ---
    lines.append("# HELP nvme_error_log_entries_total NVMe error log entry count.")
    lines.append("# TYPE nvme_error_log_entries_total counter")
    for r in device_results:
        lines.append(
            f'nvme_error_log_entries_total{{device="{r["device"]}"}} {r["error_log_count"]}'
        )

    # --- collector up ---
    lines.append("# HELP nvme_collector_up 1 if NVMe devices were found and nvme-cli is present.")
    lines.append("# TYPE nvme_collector_up gauge")
    lines.append(f"nvme_collector_up {collector_up}")

    # --- last run timestamp ---
    lines.append("# HELP nvme_collector_last_run_timestamp Unix timestamp of last collector run.")
    lines.append("# TYPE nvme_collector_last_run_timestamp gauge")
    lines.append(f"nvme_collector_last_run_timestamp {now:.3f}")

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="Collect NVMe SMART metrics and emit Prometheus textfile format."
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help="Output .prom file path (- for stdout)"
    )
    parser.add_argument(
        "--sys-nvme-path", type=Path, default=DEFAULT_SYS_NVME,
        help="Path to /sys/class/nvme (for testing)"
    )
    args = parser.parse_args()

    # Check nvme-cli availability
    nvme_cli_present = shutil.which(NVME_CLI) is not None

    devices = find_nvme_devices(args.sys_nvme_path)

    if not nvme_cli_present or not devices:
        if not nvme_cli_present:
            sys.stderr.write("nvme-collector: nvme-cli not found, emitting collector_up=0\n")
        else:
            sys.stderr.write("nvme-collector: no NVMe devices found, emitting collector_up=0\n")
        output = emit_metrics([], collector_up=0)
    else:
        device_results = []
        for device in devices:
            try:
                result = collect_device(device)
                device_results.append(result)
            except Exception as exc:
                sys.stderr.write(f"nvme-collector: error collecting {device}: {exc}\n")

        collector_up = 1 if device_results else 0
        output = emit_metrics(device_results, collector_up=collector_up)

    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        sys.stderr.write(f"nvme-collector: wrote {len(output)} bytes to {args.output}\n")


if __name__ == "__main__":
    main()
