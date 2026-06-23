#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
power_cxl/rapl-collector.py — Collect CPU power and frequency throttle metrics.

Reads Intel RAPL energy counters via /sys/class/powercap/intel-rapl/ and
CPU frequency throttle ratios from /sys/devices/system/cpu/cpu*/cpufreq/.
Emits Prometheus textfile metrics.

Compatible with x86 (RAPL) and ARM (cpufreq only). Degrades gracefully when
powercap or cpufreq sysfs paths are absent.

Usage:
    python3 power_cxl/rapl-collector.py [--output PATH]
                                         [--powercap-path PATH]
                                         [--cpu-path PATH]

Writes to: /var/lib/node_exporter/textfile_collector/rapl.prom (default)

Metrics:
    rapl_package_power_watts{package}   — instantaneous package power (W)
    rapl_power_limit_watts{package}     — constraint_0 power limit (W)
    rapl_throttled{package}             — 1 if power > 95% of limit
    cpu_freq_throttle_ratio{cpu}        — scaling_cur_freq / scaling_max_freq
    rapl_collector_up                   — 1 if any data was collected
    rapl_collector_last_run_timestamp   — Unix timestamp of last run
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

DEFAULT_OUTPUT      = Path("/var/lib/node_exporter/textfile_collector/rapl.prom")
DEFAULT_POWERCAP    = Path("/sys/class/powercap/intel-rapl")
DEFAULT_CPU_PATH    = Path("/sys/devices/system/cpu")

THROTTLE_THRESHOLD  = 0.95  # flag throttled when power > 95% of limit


def _read_int(path: Path) -> int | None:
    """Read a sysfs file and return its integer content, or None on failure."""
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError, PermissionError):
        return None


def collect_rapl(powercap_path: Path) -> list:
    """
    Enumerate Intel RAPL package domains under powercap_path.

    For each intel-rapl:N subdirectory:
      - Reads 'name' to get the domain label (e.g. "package-0").
      - Reads 'energy_uj' twice (1 s apart) and computes watts.
      - Reads 'constraint_0_power_limit_uw' for the power limit.

    Returns a list of dicts with keys:
        package, power_watts, limit_watts, throttled
    """
    results = []
    try:
        candidates = sorted(powercap_path.iterdir())
    except (OSError, PermissionError):
        return results

    for entry in candidates:
        if not entry.name.startswith("intel-rapl:"):
            continue
        # Skip sub-domains (e.g. intel-rapl:0:0)
        if entry.name.count(":") > 1:
            continue

        name_file   = entry / "name"
        energy_file = entry / "energy_uj"
        limit_file  = entry / "constraint_0_power_limit_uw"

        package_name = None
        try:
            package_name = name_file.read_text().strip()
        except (OSError, PermissionError):
            package_name = entry.name  # fallback

        e1 = _read_int(energy_file)
        if e1 is None:
            continue
        time.sleep(1)
        e2 = _read_int(energy_file)
        if e2 is None:
            continue

        # Handle counter wrap (energy_uj wraps at max_energy_range_uj)
        delta_uj = e2 - e1 if e2 >= e1 else 0
        power_watts = delta_uj / 1_000_000

        limit_uw = _read_int(limit_file)
        limit_watts = limit_uw / 1_000_000 if limit_uw is not None else None

        throttled = 0
        if limit_watts is not None and limit_watts > 0:
            throttled = 1 if power_watts > THROTTLE_THRESHOLD * limit_watts else 0

        results.append({
            "package":     package_name,
            "power_watts": power_watts,
            "limit_watts": limit_watts,
            "throttled":   throttled,
        })

    return results


def collect_cpufreq(cpu_path: Path) -> list:
    """
    Enumerate CPU frequency scaling directories.

    For each /sys/devices/system/cpu/cpuN/cpufreq/ that exists:
      - Reads scaling_cur_freq and scaling_max_freq.
      - Computes throttle_ratio = cur / max.

    Returns a list of dicts with keys: cpu, throttle_ratio.
    Skips CPUs missing the cpufreq directory or either frequency file.
    """
    results = []
    try:
        cpu_dirs = sorted(cpu_path.iterdir())
    except (OSError, PermissionError):
        return results

    for cpu_dir in cpu_dirs:
        if not cpu_dir.name.startswith("cpu"):
            continue
        # Only real CPU dirs (cpu0, cpu1, ...) — skip cpufreq, cpuidle dirs
        suffix = cpu_dir.name[3:]
        if not suffix.isdigit():
            continue

        cpufreq_dir = cpu_dir / "cpufreq"
        if not cpufreq_dir.is_dir():
            continue

        cur  = _read_int(cpufreq_dir / "scaling_cur_freq")
        maxi = _read_int(cpufreq_dir / "scaling_max_freq")

        if cur is None or maxi is None or maxi == 0:
            continue

        results.append({
            "cpu":            cpu_dir.name,
            "throttle_ratio": cur / maxi,
        })

    return results


def emit_metrics(
    rapl_records: list,
    cpufreq_records: list,
    collector_up: int,
) -> str:
    """Render all collected data as Prometheus textfile format."""
    lines = []
    now = time.time()

    # --- RAPL package power ---
    lines.append(
        "# HELP rapl_package_power_watts Instantaneous CPU package power draw in watts (RAPL)."
    )
    lines.append("# TYPE rapl_package_power_watts gauge")
    for r in rapl_records:
        lines.append(
            f'rapl_package_power_watts{{package="{r["package"]}"}} {r["power_watts"]:.6f}'
        )

    # --- RAPL power limit ---
    lines.append(
        "# HELP rapl_power_limit_watts CPU package TDP power limit in watts."
    )
    lines.append("# TYPE rapl_power_limit_watts gauge")
    for r in rapl_records:
        if r["limit_watts"] is not None:
            lines.append(
                f'rapl_power_limit_watts{{package="{r["package"]}"}} {r["limit_watts"]:.6f}'
            )

    # --- RAPL throttled ---
    lines.append(
        "# HELP rapl_throttled 1 if package power exceeds 95% of the TDP limit."
    )
    lines.append("# TYPE rapl_throttled gauge")
    for r in rapl_records:
        lines.append(
            f'rapl_throttled{{package="{r["package"]}"}} {r["throttled"]}'
        )

    # --- CPU frequency throttle ratio ---
    lines.append(
        "# HELP cpu_freq_throttle_ratio Ratio of current to maximum CPU scaling frequency."
    )
    lines.append("# TYPE cpu_freq_throttle_ratio gauge")
    for r in cpufreq_records:
        lines.append(
            f'cpu_freq_throttle_ratio{{cpu="{r["cpu"]}"}} {r["throttle_ratio"]:.6f}'
        )

    # --- collector up ---
    lines.append(
        "# HELP rapl_collector_up 1 if RAPL or cpufreq data was collected successfully."
    )
    lines.append("# TYPE rapl_collector_up gauge")
    lines.append(f"rapl_collector_up {collector_up}")

    # --- last run timestamp ---
    lines.append(
        "# HELP rapl_collector_last_run_timestamp Unix timestamp of last collector run."
    )
    lines.append("# TYPE rapl_collector_last_run_timestamp gauge")
    lines.append(f"rapl_collector_last_run_timestamp {now:.3f}")

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="Collect RAPL power and CPU frequency throttle metrics."
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help="Output .prom file path (- for stdout)"
    )
    parser.add_argument(
        "--powercap-path", type=Path, default=DEFAULT_POWERCAP,
        help="Path to Intel RAPL powercap sysfs root"
    )
    parser.add_argument(
        "--cpu-path", type=Path, default=DEFAULT_CPU_PATH,
        help="Path to /sys/devices/system/cpu"
    )
    args = parser.parse_args()

    rapl_records    = collect_rapl(args.powercap_path)
    cpufreq_records = collect_cpufreq(args.cpu_path)

    any_data = bool(rapl_records or cpufreq_records)
    collector_up = 1 if any_data else 0

    if not any_data:
        sys.stderr.write(
            "rapl-collector: no powercap or cpufreq data found, emitting collector_up=0\n"
        )

    output = emit_metrics(rapl_records, cpufreq_records, collector_up=collector_up)

    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        sys.stderr.write(f"rapl-collector: wrote {len(output)} bytes to {args.output}\n")


if __name__ == "__main__":
    main()
