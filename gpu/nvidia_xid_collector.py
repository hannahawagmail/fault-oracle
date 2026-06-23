#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
gpu/nvidia-xid-collector.py — Collect NVIDIA GPU XID errors and ECC counts.

Emits Prometheus textfile metrics to stdout or --output file. Degrades
gracefully when no NVIDIA GPU or nvidia-smi is present.

Metrics emitted:
    gpu_xid_error_total{gpu_index, xid, severity}          — XID error counter
    gpu_ecc_sbe_total{gpu_index}                            — single-bit (corrected) ECC errors
    gpu_ecc_dbe_total{gpu_index}                            — double-bit (uncorrected) ECC errors
    gpu_collector_up{collector="nvidia_xid"}                — 1 if GPU detected, 0 otherwise
    nvidia_gpu_collector_last_run_timestamp                  — Unix timestamp of last run

XID severity mapping:
    63  (DBE)                    → fatal
    94  (contained ECC)          → critical
    95  (uncontained ECC)        → critical
    74  (NVLINK error)           → warning
    79  (GPU recovery initiated) → warning
    all others                   → info

Usage:
    python3 gpu/nvidia-xid-collector.py [--output PATH]
"""
import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_OUTPUT = Path("/var/lib/node_exporter/textfile_collector/nvidia_xid.prom")

# XID → severity lookup table
XID_SEVERITY: dict = {
    63: "fatal",
    94: "critical",
    95: "critical",
    74: "warning",
    79: "warning",
}


def xid_severity(xid: int) -> str:
    return XID_SEVERITY.get(xid, "info")


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


def detect_gpus() -> list:  # pragma: no cover
    """
    Return a list of GPU name strings via nvidia-smi.
    Returns empty list if nvidia-smi is absent or exits non-zero.
    """
    stdout, rc = _run(
        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
    )
    if rc != 0:
        return []
    names = [line.strip() for line in stdout.splitlines() if line.strip()]
    return names


def collect_xid_errors() -> list:  # pragma: no cover
    """
    Attempt to read XID events via nvidia-smi dmon.
    Returns list of dicts: {gpu_index, xid, severity}.

    Falls back to /proc/driver/nvidia/gpus/*/information if dmon is unavailable.
    dmon -s u reports 'xid' column; we take one sample (-c 1, -d 1).
    """
    xid_events = []

    # Primary: nvidia-smi dmon
    stdout, rc = _run(
        ["nvidia-smi", "dmon", "-s", "u", "-d", "1", "-c", "1"],
        timeout=15,
    )
    if rc == 0 and stdout.strip():
        xid_events = _parse_dmon_xid(stdout)
        if xid_events is not None:
            return xid_events

    # Fallback: /proc/driver/nvidia/gpus/*/information
    proc_gpus = list(Path("/proc/driver/nvidia/gpus").glob("*/information")) if Path(
        "/proc/driver/nvidia/gpus"
    ).exists() else []
    for idx, info_path in enumerate(sorted(proc_gpus)):
        try:
            text = info_path.read_text(errors="replace")
            for m in re.finditer(r"XID\s+(\d+)", text, re.IGNORECASE):
                xid = int(m.group(1))
                xid_events.append({
                    "gpu_index": str(idx),
                    "xid": xid,
                    "severity": xid_severity(xid),
                })
        except OSError:
            continue

    return xid_events


def _parse_dmon_xid(output: str) -> list:
    """
    Parse nvidia-smi dmon -s u output.
    Header lines start with '#'. Data lines are space-separated.
    Returns list of {gpu_index, xid, severity} or empty list if no XID column.
    """
    lines = output.splitlines()
    header_line = None
    data_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            # nvidia-smi dmon emits two header comment lines:
            #   first:  column names  (e.g. "# gpu  sm  mem  enc  dec  xid")
            #   second: column units  (e.g. "# Idx   %   %   %   %  No")
            # We only want the first one (contains "xid" or "gpu").
            if header_line is None:
                header_line = stripped.lstrip("#").split()
            # Subsequent comment lines are units rows — skip them.
        else:
            data_lines.append(stripped.split())

    if header_line is None:
        return []

    # Normalise header to lowercase
    headers = [h.lower() for h in header_line]
    xid_col = None
    gpu_col = None
    for i, h in enumerate(headers):
        if h == "xid":
            xid_col = i
        if h in ("gpu", "idx", "gpu_index"):
            gpu_col = i

    if xid_col is None:
        return []

    events = []
    for row in data_lines:
        if len(row) <= xid_col:
            continue
        try:
            xid_val = int(row[xid_col])
        except ValueError:
            continue
        if xid_val == 0:
            continue  # 0 means no XID error reported

        gpu_idx = row[gpu_col] if (gpu_col is not None and len(row) > gpu_col) else "0"
        events.append({
            "gpu_index": gpu_idx,
            "xid": xid_val,
            "severity": xid_severity(xid_val),
        })
    return events


def collect_ecc_counts() -> list:  # pragma: no cover
    """
    Query ECC corrected/uncorrected volatile totals via nvidia-smi.
    Returns list of dicts: {gpu_index, sbe, dbe}.
    """
    stdout, rc = _run([
        "nvidia-smi",
        "--query-gpu=index,ecc.errors.corrected.volatile.total,"
        "ecc.errors.uncorrected.volatile.total",
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
        if len(parts) < 3:
            continue
        try:
            gpu_idx = parts[0]
            sbe = int(parts[1]) if parts[1] not in ("N/A", "[N/A]", "") else 0
            dbe = int(parts[2]) if parts[2] not in ("N/A", "[N/A]", "") else 0
            results.append({"gpu_index": gpu_idx, "sbe": sbe, "dbe": dbe})
        except (ValueError, IndexError):
            continue
    return results


def emit_metrics(
    gpu_present: bool,
    xid_events: list,
    ecc_counts: list,
    now: float,
) -> str:
    lines = []

    lines += [
        "# HELP gpu_collector_up 1 if NVIDIA GPU detected and collector operational, 0 otherwise.",
        "# TYPE gpu_collector_up gauge",
        f'gpu_collector_up{{collector="nvidia_xid"}} {1 if gpu_present else 0}',
    ]

    if gpu_present:
        lines += [
            "# HELP gpu_xid_error_total Total NVIDIA GPU XID error events observed.",
            "# TYPE gpu_xid_error_total counter",
        ]
        for ev in xid_events:
            lines.append(
                f'gpu_xid_error_total{{gpu_index="{ev["gpu_index"]}",'
                f'xid="{ev["xid"]}",severity="{ev["severity"]}"}} 1'
            )

        lines += [
            "# HELP gpu_ecc_sbe_total Total single-bit (corrected) ECC errors per GPU (volatile).",
            "# TYPE gpu_ecc_sbe_total counter",
        ]
        for ec in ecc_counts:
            lines.append(
                f'gpu_ecc_sbe_total{{gpu_index="{ec["gpu_index"]}"}} {ec["sbe"]}'
            )

        lines += [
            "# HELP gpu_ecc_dbe_total Total double-bit (uncorrected) ECC errors per GPU (volatile).",
            "# TYPE gpu_ecc_dbe_total counter",
        ]
        for ec in ecc_counts:
            lines.append(
                f'gpu_ecc_dbe_total{{gpu_index="{ec["gpu_index"]}"}} {ec["dbe"]}'
            )

    lines += [
        "# HELP nvidia_gpu_collector_last_run_timestamp Unix timestamp of the last XID collector run.",
        "# TYPE nvidia_gpu_collector_last_run_timestamp gauge",
        f"nvidia_gpu_collector_last_run_timestamp {now:.3f}",
    ]

    return "\n".join(lines) + "\n"


def main():  # pragma: no cover
    parser = argparse.ArgumentParser(
        description="Collect NVIDIA GPU XID/ECC metrics and emit Prometheus textfile output."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output .prom file (- for stdout)",
    )
    args = parser.parse_args()

    now = time.time()
    gpus = detect_gpus()
    gpu_present = len(gpus) > 0

    if not gpu_present:
        sys.stderr.write("nvidia-xid-collector: no NVIDIA GPU detected, emitting collector_up=0\n")
        output = emit_metrics(False, [], [], now)
    else:
        sys.stderr.write(f"nvidia-xid-collector: detected {len(gpus)} GPU(s)\n")
        xid_events = collect_xid_errors()
        ecc_counts = collect_ecc_counts()
        output = emit_metrics(True, xid_events, ecc_counts, now)

    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        sys.stderr.write(f"nvidia-xid-collector: wrote to {args.output}\n")


if __name__ == "__main__":  # pragma: no cover
    main()
