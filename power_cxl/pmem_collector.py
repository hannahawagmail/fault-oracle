#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
power_cxl/pmem-collector.py — Collect NVDIMM/PMEM health data via ndctl.

Runs `ndctl list --health` and `ndctl list --media-errors` to gather
persistent-memory device health metrics and emits Prometheus textfile format.

Degrades gracefully when ndctl is absent or no PMEM devices are present.

Usage:
    python3 power_cxl/pmem-collector.py [--output PATH]

Writes to: /var/lib/node_exporter/textfile_collector/pmem.prom (default)

Metrics:
    pmem_health_state{device,state}          — 1 if device is in this state
    pmem_lifespan_used_percent{device}       — lifespan consumed (%)
    pmem_lifespan_remaining_percent{device}  — lifespan remaining (%)
    pmem_temperature_celsius{device}         — device temperature (if available)
    pmem_unsafe_shutdowns_total{device}      — cumulative unsafe shutdowns
    pmem_ars_in_progress{device}             — 1 if ARS scan is in progress
    pmem_media_errors_total{device}          — count of media error regions
    pmem_collector_up                        — 1 if ndctl present and data found
    pmem_collector_last_run_timestamp        — Unix timestamp of last run
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_OUTPUT = Path("/var/lib/node_exporter/textfile_collector/pmem.prom")

HEALTH_STATES = ("ok", "non-critical", "critical", "fatal")


def run_ndctl(args: list) -> str:
    """Run ndctl with the given args and return stdout. Returns '' on failure."""
    try:
        result = subprocess.run(
            ["ndctl"] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.decode("utf-8", errors="replace")
    except (OSError, FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def parse_health_output(raw: str) -> list:
    """
    Parse `ndctl list --health --json` output.

    Returns a list of dicts, each with keys:
        dev, health_state, lifespan_used, lifespan_remaining,
        temperature (optional), unsafe_shutdowns, ars_in_progress
    Returns empty list on parse failure or empty array.
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    if not isinstance(data, list) or len(data) == 0:
        return []

    results = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        dev = entry.get("dev")
        if not dev:
            continue
        health = entry.get("health")
        if not isinstance(health, dict):
            continue

        record = {"dev": dev}
        record["health_state"] = health.get("health_state", "ok")
        record["lifespan_used"] = health.get("lifespan_used")
        record["lifespan_remaining"] = health.get("lifespan_remaining")
        record["temperature"] = health.get("temperature")  # may be absent
        record["unsafe_shutdowns"] = health.get("unsafe_shutdowns")
        ars = health.get("ars_status", "idle")
        record["ars_in_progress"] = 1 if ars == "scanning" else 0
        results.append(record)

    return results


def count_media_errors(raw: str) -> dict:
    """
    Parse `ndctl list --media-errors --json` output.

    Returns a dict mapping device name → error count.
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    if not isinstance(data, list):
        return {}

    counts: dict = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        dev = entry.get("dev")
        if not dev:
            continue
        counts[dev] = counts.get(dev, 0) + 1
    return counts


def emit_metrics(records: list, media_errors: dict, collector_up: int) -> str:
    """Render all collected data as Prometheus textfile format."""
    lines = []
    now = time.time()

    # --- health_state (4 booleans per device) ---
    lines.append(
        "# HELP pmem_health_state 1 if the PMEM device is in the given health state."
    )
    lines.append("# TYPE pmem_health_state gauge")
    for r in records:
        dev = r["dev"]
        current_state = r["health_state"]
        for state in HEALTH_STATES:
            val = 1 if current_state == state else 0
            lines.append(f'pmem_health_state{{device="{dev}",state="{state}"}} {val}')

    # --- lifespan_used ---
    lines.append(
        "# HELP pmem_lifespan_used_percent PMEM lifespan consumed (%)."
    )
    lines.append("# TYPE pmem_lifespan_used_percent gauge")
    for r in records:
        val = r.get("lifespan_used")
        if val is not None:
            lines.append(f'pmem_lifespan_used_percent{{device="{r["dev"]}"}} {val}')

    # --- lifespan_remaining ---
    lines.append(
        "# HELP pmem_lifespan_remaining_percent PMEM lifespan remaining (%)."
    )
    lines.append("# TYPE pmem_lifespan_remaining_percent gauge")
    for r in records:
        val = r.get("lifespan_remaining")
        if val is not None:
            lines.append(
                f'pmem_lifespan_remaining_percent{{device="{r["dev"]}"}} {val}'
            )

    # --- temperature ---
    lines.append(
        "# HELP pmem_temperature_celsius PMEM device temperature in Celsius."
    )
    lines.append("# TYPE pmem_temperature_celsius gauge")
    for r in records:
        val = r.get("temperature")
        if val is not None:
            lines.append(f'pmem_temperature_celsius{{device="{r["dev"]}"}} {val}')

    # --- unsafe_shutdowns ---
    lines.append(
        "# HELP pmem_unsafe_shutdowns_total PMEM cumulative unsafe shutdown count."
    )
    lines.append("# TYPE pmem_unsafe_shutdowns_total counter")
    for r in records:
        val = r.get("unsafe_shutdowns")
        if val is not None:
            lines.append(
                f'pmem_unsafe_shutdowns_total{{device="{r["dev"]}"}} {val}'
            )

    # --- ars_in_progress ---
    lines.append(
        "# HELP pmem_ars_in_progress 1 if address-range-scrub scan is in progress."
    )
    lines.append("# TYPE pmem_ars_in_progress gauge")
    for r in records:
        lines.append(
            f'pmem_ars_in_progress{{device="{r["dev"]}"}} {r["ars_in_progress"]}'
        )

    # --- media_errors ---
    lines.append(
        "# HELP pmem_media_errors_total Number of media-error regions reported by ndctl."
    )
    lines.append("# TYPE pmem_media_errors_total counter")
    all_devs = {r["dev"] for r in records} | set(media_errors.keys())
    for dev in sorted(all_devs):
        count = media_errors.get(dev, 0)
        lines.append(f'pmem_media_errors_total{{device="{dev}"}} {count}')

    # --- collector up ---
    lines.append(
        "# HELP pmem_collector_up 1 if ndctl is present and PMEM devices were found."
    )
    lines.append("# TYPE pmem_collector_up gauge")
    lines.append(f"pmem_collector_up {collector_up}")

    # --- last run timestamp ---
    lines.append(
        "# HELP pmem_collector_last_run_timestamp Unix timestamp of last collector run."
    )
    lines.append("# TYPE pmem_collector_last_run_timestamp gauge")
    lines.append(f"pmem_collector_last_run_timestamp {now:.3f}")

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="Collect PMEM/NVDIMM health metrics via ndctl."
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help="Output .prom file path (- for stdout)"
    )
    args = parser.parse_args()

    health_raw = run_ndctl(["list", "--health", "--json"])

    # ndctl absent → FileNotFoundError swallowed, raw == ""
    # non-zero exit → raw == ""
    if not health_raw:
        output = emit_metrics([], {}, collector_up=0)
        sys.stderr.write(
            "pmem-collector: ndctl unavailable or returned no data, emitting collector_up=0\n"
        )
        _write_output(args.output, output)
        return

    records = parse_health_output(health_raw)
    if not records:
        output = emit_metrics([], {}, collector_up=0)
        sys.stderr.write(
            "pmem-collector: no PMEM devices found, emitting collector_up=0\n"
        )
        _write_output(args.output, output)
        return

    media_raw = run_ndctl(["list", "--media-errors", "--json"])
    media_errors = count_media_errors(media_raw)

    output = emit_metrics(records, media_errors, collector_up=1)
    _write_output(args.output, output)


def _write_output(path: Path, output: str) -> None:
    if str(path) == "-":
        sys.stdout.write(output)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output)
        sys.stderr.write(f"pmem-collector: wrote {len(output)} bytes to {path}\n")


if __name__ == "__main__":
    main()
