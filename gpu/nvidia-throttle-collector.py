#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
gpu/nvidia-throttle-collector.py — Collect NVIDIA GPU throttle reasons, clock speeds,
power draw, and temperature.

Emits Prometheus textfile metrics to stdout or --output file. Degrades
gracefully when no NVIDIA GPU or nvidia-smi is present.

Metrics emitted:
    gpu_throttle_active{gpu_index, reason}          — gauge, 1=Active 0=Not Active
    gpu_clock_mhz{gpu_index, domain="graphics"}     — gauge, current graphics clock in MHz
    gpu_power_watts{gpu_index}                       — gauge, current power draw in watts
    gpu_temperature_celsius{gpu_index}               — gauge, current GPU die temperature
    gpu_throttle_collector_last_run_timestamp        — Unix timestamp of last run
    gpu_throttle_collector_up{collector="nvidia_throttle"} — 1 if GPU present, 0 otherwise

Throttle reasons queried:
    hw_thermal    → clocks_throttle_reasons.hw_thermal_slowdown
    sw_thermal    → clocks_throttle_reasons.sw_thermal_slowdown
    hw_power_brake → clocks_throttle_reasons.hw_power_brake_slowdown

Usage:
    python3 gpu/nvidia-throttle-collector.py [--output PATH]
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_OUTPUT = Path("/var/lib/node_exporter/textfile_collector/nvidia_throttle.prom")

THROTTLE_FIELDS = [
    ("hw_thermal",    "clocks_throttle_reasons.hw_thermal_slowdown"),
    ("sw_thermal",    "clocks_throttle_reasons.sw_thermal_slowdown"),
    ("hw_power_brake","clocks_throttle_reasons.hw_power_brake_slowdown"),
]

QUERY_FIELDS = (
    "index,"
    + ",".join(f for _, f in THROTTLE_FIELDS)
    + ",clocks.current.graphics,power.draw,temperature.gpu"
)


def _run(cmd: list, timeout: int = 15) -> tuple:  # pragma: no cover
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


def detect_gpus() -> bool:  # pragma: no cover
    """Return True if at least one NVIDIA GPU is present."""
    stdout, rc = _run(
        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
    )
    if rc != 0:
        return False
    names = [line.strip() for line in stdout.splitlines() if line.strip()]
    return len(names) > 0


def parse_active(value: str) -> int:
    """Convert 'Active' / 'Not Active' / 'N/A' to 0 or 1."""
    v = value.strip()
    if v == "Active":
        return 1
    return 0


def collect_throttle_data() -> list:  # pragma: no cover
    """
    Query throttle reasons and metrics from nvidia-smi.
    Returns list of dicts per GPU:
        {gpu_index, throttle:{hw_thermal,sw_thermal,hw_power_brake},
         clock_graphics, power_watts, temperature}
    """
    stdout, rc = _run([
        "nvidia-smi",
        f"--query-gpu={QUERY_FIELDS}",
        "--format=csv,noheader,nounits",
    ])
    if rc != 0:
        return []

    results = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        # Expect: index + 3 throttle reasons + clock + power + temp = 7 fields
        if len(parts) < 7:
            continue

        try:
            gpu_idx = parts[0]
            throttle = {}
            for col_idx, (reason_name, _) in enumerate(THROTTLE_FIELDS):
                throttle[reason_name] = parse_active(parts[1 + col_idx])

            def _float_or_zero(val: str) -> float:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    return 0.0

            clock_graphics = _float_or_zero(parts[4])
            power_watts    = _float_or_zero(parts[5])
            temperature    = _float_or_zero(parts[6])

            results.append({
                "gpu_index": gpu_idx,
                "throttle": throttle,
                "clock_graphics": clock_graphics,
                "power_watts": power_watts,
                "temperature": temperature,
            })
        except (IndexError, ValueError):
            continue

    return results


def emit_metrics(
    gpu_present: bool,
    throttle_data: list,
    now: float,
) -> str:
    lines = []

    lines += [
        "# HELP gpu_throttle_collector_up 1 if NVIDIA GPU detected and collector operational.",
        "# TYPE gpu_throttle_collector_up gauge",
        f'gpu_throttle_collector_up{{collector="nvidia_throttle"}} {1 if gpu_present else 0}',
    ]

    if gpu_present:
        lines += [
            "# HELP gpu_throttle_active 1 if throttle reason is active on the GPU.",
            "# TYPE gpu_throttle_active gauge",
        ]
        for gpu in throttle_data:
            idx = gpu["gpu_index"]
            for reason, active in gpu["throttle"].items():
                lines.append(
                    f'gpu_throttle_active{{gpu_index="{idx}",reason="{reason}"}} {active}'
                )

        lines += [
            "# HELP gpu_clock_mhz Current GPU clock frequency in MHz.",
            "# TYPE gpu_clock_mhz gauge",
        ]
        for gpu in throttle_data:
            lines.append(
                f'gpu_clock_mhz{{gpu_index="{gpu["gpu_index"]}",domain="graphics"}}'
                f' {gpu["clock_graphics"]:.1f}'
            )

        lines += [
            "# HELP gpu_power_watts Current GPU power draw in watts.",
            "# TYPE gpu_power_watts gauge",
        ]
        for gpu in throttle_data:
            lines.append(
                f'gpu_power_watts{{gpu_index="{gpu["gpu_index"]}"}} {gpu["power_watts"]:.2f}'
            )

        lines += [
            "# HELP gpu_temperature_celsius Current GPU die temperature in degrees Celsius.",
            "# TYPE gpu_temperature_celsius gauge",
        ]
        for gpu in throttle_data:
            lines.append(
                f'gpu_temperature_celsius{{gpu_index="{gpu["gpu_index"]}"}} {gpu["temperature"]:.1f}'
            )

    lines += [
        "# HELP gpu_throttle_collector_last_run_timestamp Unix timestamp of the last throttle collector run.",
        "# TYPE gpu_throttle_collector_last_run_timestamp gauge",
        f"gpu_throttle_collector_last_run_timestamp {now:.3f}",
    ]

    return "\n".join(lines) + "\n"


def main():  # pragma: no cover
    parser = argparse.ArgumentParser(
        description="Collect NVIDIA GPU throttle/clock/power/temperature metrics."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output .prom file (- for stdout)",
    )
    args = parser.parse_args()

    now = time.time()
    gpu_present = detect_gpus()

    if not gpu_present:
        sys.stderr.write(
            "nvidia-throttle-collector: no NVIDIA GPU detected, emitting collector_up=0\n"
        )
        output = emit_metrics(False, [], now)
    else:
        throttle_data = collect_throttle_data()
        sys.stderr.write(
            f"nvidia-throttle-collector: collected data for {len(throttle_data)} GPU(s)\n"
        )
        output = emit_metrics(True, throttle_data, now)

    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        sys.stderr.write(f"nvidia-throttle-collector: wrote to {args.output}\n")


if __name__ == "__main__":  # pragma: no cover
    main()
