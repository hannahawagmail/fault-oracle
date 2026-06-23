#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
power_cxl/misc-hardware-collector.py — Collect miscellaneous hardware metrics.

Collects:
  - PCIe AER correctable/fatal error counters from sysfs
  - Hardware watchdog presence (/dev/watchdog)
  - Hardware RNG activity (/sys/class/misc/hw_random/rng_current)

Each collector can be disabled via environment variables:
  COLLECT_PCIE_AER=false   — skip PCIe AER collection
  COLLECT_WATCHDOG=false   — skip watchdog detection
  COLLECT_HWRNG=false      — skip hardware RNG detection

Usage:
    python3 power_cxl/misc-hardware-collector.py [--output PATH]
                                                   [--pci-path PATH]

Writes to: /var/lib/node_exporter/textfile_collector/misc_hardware.prom (default)

Metrics:
    pcie_aer_correctable_total{pci_id}   — sum of correctable AER errors
    pcie_aer_fatal_total{pci_id}         — sum of fatal AER errors
    watchdog_present                     — 1 if /dev/watchdog exists
    hwrng_active                         — 1 if hw_random/rng_current is non-empty
    misc_collector_up                    — always 1
    misc_collector_last_run_timestamp    — Unix timestamp of last run
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

DEFAULT_OUTPUT   = Path("/var/lib/node_exporter/textfile_collector/misc_hardware.prom")
DEFAULT_PCI_PATH = Path("/sys/bus/pci/devices")
WATCHDOG_PATH    = Path("/dev/watchdog")
HWRNG_PATH       = Path("/sys/class/misc/hw_random/rng_current")


def _env_enabled(var: str) -> bool:
    """Return False only if the environment variable is explicitly set to 'false'."""
    return os.environ.get(var, "true").lower() != "false"


def _parse_aer_file(path: Path) -> int:
    """
    Parse an AER sysfs counter file.

    Each line is of the form '<name> <count>'.
    Returns the sum of all non-zero integer count values.
    """
    try:
        total = 0
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                try:
                    total += int(parts[-1])
                except ValueError:
                    pass
        return total
    except (OSError, PermissionError):
        return 0


def collect_pcie_aer(pci_path: Path) -> list:
    """
    Walk pci_path and collect AER error counters.

    Returns a list of dicts with keys:
        pci_id, correctable, fatal
    Only includes devices where at least one AER file exists.
    """
    results = []
    try:
        devices = sorted(pci_path.iterdir())
    except (OSError, PermissionError):
        return results

    for dev in devices:
        if not dev.is_dir():
            continue

        corr_file  = dev / "aer_dev_correctable"
        fatal_file = dev / "aer_dev_fatal"

        has_corr  = corr_file.exists()
        has_fatal = fatal_file.exists()

        if not has_corr and not has_fatal:
            continue

        correctable = _parse_aer_file(corr_file) if has_corr else 0
        fatal       = _parse_aer_file(fatal_file) if has_fatal else 0

        results.append({
            "pci_id":      dev.name,
            "correctable": correctable,
            "fatal":       fatal,
        })

    return results


def collect_watchdog(watchdog_path: Path = WATCHDOG_PATH) -> int:
    """Return 1 if the watchdog device node exists, else 0."""
    return 1 if watchdog_path.exists() else 0


def collect_hwrng(hwrng_path: Path = HWRNG_PATH) -> int:
    """Return 1 if the hw_random rng_current file exists and is non-empty."""
    try:
        content = hwrng_path.read_text().strip()
        return 1 if content else 0
    except (OSError, PermissionError):
        return 0


def emit_metrics(
    aer_records: list,
    watchdog: int | None,
    hwrng: int | None,
) -> str:
    """Render all collected data as Prometheus textfile format."""
    lines = []
    now = time.time()

    # --- PCIe AER correctable ---
    if aer_records is not None:
        lines.append(
            "# HELP pcie_aer_correctable_total"
            " Sum of PCIe AER correctable error counters per device."
        )
        lines.append("# TYPE pcie_aer_correctable_total counter")
        for r in aer_records:
            lines.append(
                f'pcie_aer_correctable_total{{pci_id="{r["pci_id"]}"}} {r["correctable"]}'
            )

        lines.append(
            "# HELP pcie_aer_fatal_total"
            " Sum of PCIe AER fatal error counters per device."
        )
        lines.append("# TYPE pcie_aer_fatal_total counter")
        for r in aer_records:
            lines.append(
                f'pcie_aer_fatal_total{{pci_id="{r["pci_id"]}"}} {r["fatal"]}'
            )

    # --- watchdog ---
    if watchdog is not None:
        lines.append(
            "# HELP watchdog_present 1 if /dev/watchdog device node is present."
        )
        lines.append("# TYPE watchdog_present gauge")
        lines.append(f"watchdog_present {watchdog}")

    # --- hwrng ---
    if hwrng is not None:
        lines.append(
            "# HELP hwrng_active 1 if a hardware RNG is active."
        )
        lines.append("# TYPE hwrng_active gauge")
        lines.append(f"hwrng_active {hwrng}")

    # --- collector up ---
    lines.append(
        "# HELP misc_collector_up 1 when the misc hardware collector ran successfully."
    )
    lines.append("# TYPE misc_collector_up gauge")
    lines.append("misc_collector_up 1")

    # --- last run timestamp ---
    lines.append(
        "# HELP misc_collector_last_run_timestamp Unix timestamp of last collector run."
    )
    lines.append("# TYPE misc_collector_last_run_timestamp gauge")
    lines.append(f"misc_collector_last_run_timestamp {now:.3f}")

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="Collect miscellaneous hardware metrics."
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help="Output .prom file path (- for stdout)"
    )
    parser.add_argument(
        "--pci-path", type=Path, default=DEFAULT_PCI_PATH,
        help="Path to /sys/bus/pci/devices (for testing)"
    )
    args = parser.parse_args()

    # Respect enable flags
    aer_records = collect_pcie_aer(args.pci_path) if _env_enabled("COLLECT_PCIE_AER") else None
    watchdog    = collect_watchdog()               if _env_enabled("COLLECT_WATCHDOG") else None
    hwrng       = collect_hwrng()                  if _env_enabled("COLLECT_HWRNG")    else None

    output = emit_metrics(aer_records, watchdog, hwrng)

    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        sys.stderr.write(
            f"misc-hardware-collector: wrote {len(output)} bytes to {args.output}\n"
        )


if __name__ == "__main__":
    main()
