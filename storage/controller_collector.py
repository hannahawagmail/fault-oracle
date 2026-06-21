#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
storage/controller_collector.py — Python-importable shim for controller-collector.py.

Identical implementation; exists so tests can do `import controller_collector`
(Python identifiers cannot contain hyphens).

See controller-collector.py for full documentation.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_OUTPUT    = Path("/var/lib/node_exporter/textfile_collector/controller.prom")
DEFAULT_SYS_PCI  = Path("/sys/bus/pci/drivers")
STORCLI           = "storcli"

DRIVER_MEGARAID = "megaraid_sas"
DRIVER_HPSA     = "hpsa"
DRIVER_AACRAID  = "aacraid"

ALL_DRIVERS = [DRIVER_MEGARAID, DRIVER_HPSA, DRIVER_AACRAID]


def detect_drivers(sys_pci_path: Path) -> list:
    """
    Detect which RAID/HBA drivers are loaded by checking sysfs driver directories.

    Returns a list of detected driver names (subset of ALL_DRIVERS).
    A driver is considered present if its sysfs directory exists and is non-empty
    (contains at least one PCI device symlink).
    """
    found = []
    for driver in ALL_DRIVERS:
        driver_path = sys_pci_path / driver
        try:
            if not driver_path.exists():
                continue
            entries = list(driver_path.iterdir())
            if entries:
                found.append(driver)
        except (OSError, PermissionError):
            continue
    return found


def run_command(cmd: list) -> str:  # pragma: no cover
    """Run a shell command and return stdout. Returns empty string on failure."""
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            shell=False,
        )
        return result.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def collect_megaraid() -> list:  # pragma: no cover
    """
    Collect MegaRAID controller metrics via storcli.

    Returns a list of dicts, one per controller:
        {
          "controller": str,
          "temperature": int|None,
          "critical_disks": int|None,
          "failed_disks": int|None,
          "ecc_correctable": int|None,
          "ecc_uncorrectable": int|None,
        }
    Returns empty list if storcli is absent or returns no data.
    """
    raw = run_command([STORCLI, "/cALL", "show", "J"])
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    controllers_top = data.get("Controllers")
    if not isinstance(controllers_top, list):
        return []

    results = []
    for idx, ctrl_entry in enumerate(controllers_top):
        response_data = ctrl_entry.get("Response Data", {})
        ctrl_list = response_data.get("Controllers", [])
        if not isinstance(ctrl_list, list):
            continue

        for ctrl_info in ctrl_list:
            info = ctrl_info.get("Controller Information", {})
            ctrl_id = str(info.get("Controller", idx))

            def _int(key, _info=info):
                val = _info.get(key)
                if val is None:
                    return None
                try:
                    return int(val)
                except (TypeError, ValueError):
                    return None

            results.append({
                "controller": ctrl_id,
                "temperature": _int("ROC temperature(Degree Celsius)"),
                "critical_disks": _int("Critical Disks"),
                "failed_disks": _int("Failed Disks"),
                "ecc_correctable": _int("Memory Correctable Errors"),
                "ecc_uncorrectable": _int("Memory Uncorrectable Errors"),
            })

    return results


def emit_metrics(
    detected_drivers: list,
    megaraid_results: list,
) -> str:
    """Render all collected data as Prometheus textfile format."""
    lines = []
    now = time.time()

    # --- controller_up per driver ---
    lines.append("# HELP storage_controller_up 1 if RAID/HBA driver is loaded and devices present.")
    lines.append("# TYPE storage_controller_up gauge")

    if not detected_drivers:
        lines.append('storage_controller_up{driver="none"} 0')
    else:
        for driver in detected_drivers:
            lines.append(f'storage_controller_up{{driver="{driver}"}} 1')

    # --- MegaRAID per-controller metrics ---
    if megaraid_results:
        lines.append(
            "# HELP storage_controller_temperature_celsius RAID controller temperature in Celsius."
        )
        lines.append("# TYPE storage_controller_temperature_celsius gauge")
        for r in megaraid_results:
            if r["temperature"] is not None:
                lines.append(
                    f'storage_controller_temperature_celsius{{'
                    f'controller="{r["controller"]}",driver="megaraid"}} {r["temperature"]}'
                )

        lines.append(
            "# HELP storage_controller_critical_disks Number of critical disks reported by controller."
        )
        lines.append("# TYPE storage_controller_critical_disks gauge")
        for r in megaraid_results:
            if r["critical_disks"] is not None:
                lines.append(
                    f'storage_controller_critical_disks{{'
                    f'controller="{r["controller"]}",driver="megaraid"}} {r["critical_disks"]}'
                )

        lines.append(
            "# HELP storage_controller_failed_disks Number of failed disks reported by controller."
        )
        lines.append("# TYPE storage_controller_failed_disks gauge")
        for r in megaraid_results:
            if r["failed_disks"] is not None:
                lines.append(
                    f'storage_controller_failed_disks{{'
                    f'controller="{r["controller"]}",driver="megaraid"}} {r["failed_disks"]}'
                )

        lines.append(
            "# HELP storage_controller_ecc_errors_total Cumulative ECC memory errors on controller."
        )
        lines.append("# TYPE storage_controller_ecc_errors_total counter")
        for r in megaraid_results:
            if r["ecc_correctable"] is not None:
                lines.append(
                    f'storage_controller_ecc_errors_total{{'
                    f'controller="{r["controller"]}",driver="megaraid",'
                    f'error_type="correctable"}} {r["ecc_correctable"]}'
                )
            if r["ecc_uncorrectable"] is not None:
                lines.append(
                    f'storage_controller_ecc_errors_total{{'
                    f'controller="{r["controller"]}",driver="megaraid",'
                    f'error_type="uncorrectable"}} {r["ecc_uncorrectable"]}'
                )

    # --- last run timestamp ---
    lines.append(
        "# HELP storage_controller_last_run_timestamp Unix timestamp of last collector run."
    )
    lines.append("# TYPE storage_controller_last_run_timestamp gauge")
    lines.append(f"storage_controller_last_run_timestamp {now:.3f}")

    return "\n".join(lines) + "\n"


def main():  # pragma: no cover
    parser = argparse.ArgumentParser(
        description="Collect RAID/HBA controller metrics and emit Prometheus textfile format."
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help="Output .prom file path (- for stdout)"
    )
    parser.add_argument(
        "--sys-pci-path", type=Path, default=DEFAULT_SYS_PCI,
        help="Path to /sys/bus/pci/drivers (for testing)"
    )
    args = parser.parse_args()

    detected_drivers = detect_drivers(args.sys_pci_path)

    megaraid_results = []
    if DRIVER_MEGARAID in detected_drivers:
        try:
            megaraid_results = collect_megaraid()
        except Exception as exc:
            sys.stderr.write(f"controller-collector: megaraid collection error: {exc}\n")

    if not detected_drivers:
        sys.stderr.write(
            'controller-collector: no RAID/HBA controllers detected, '
            'emitting controller_up{driver="none"} 0\n'
        )

    output = emit_metrics(detected_drivers, megaraid_results)

    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        sys.stderr.write(f"controller-collector: wrote {len(output)} bytes to {args.output}\n")


if __name__ == "__main__":  # pragma: no cover
    main()
