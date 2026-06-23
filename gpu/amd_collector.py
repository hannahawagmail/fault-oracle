#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
gpu/amd-collector.py — Collect AMD GPU RAS errors and utilisation via sysfs.

Emits Prometheus textfile metrics to stdout or --output file. Degrades
gracefully when no AMD GPU is present (amdgpu module absent or no vendor
0x1002 devices found).

Metrics emitted:
    amd_gpu_ras_error_total{card, block, error_type}  — counter (error_type=correctable|uncorrectable)
    amd_gpu_utilization_percent{card}                  — gauge
    amd_gpu_vram_used_bytes{card}                      — gauge
    amd_gpu_vram_total_bytes{card}                     — gauge
    amd_collector_up                                   — gauge (0 if no AMD GPU)
    amd_collector_last_run_timestamp                   — gauge

AMD vendor ID: 0x1002
Sysfs paths:
    /sys/module/amdgpu                              — module presence check
    /sys/class/drm/card*/device/vendor              — vendor ID file
    /sys/class/drm/card*/device/ras/                — RAS error counts directory
    /sys/class/drm/card*/device/gpu_busy_percent    — utilisation gauge
    /sys/class/drm/card*/device/mem_info_vram_total — VRAM total in bytes
    /sys/class/drm/card*/device/mem_info_vram_used  — VRAM used in bytes

RAS files (e.g. umc_err_count, gfx_err_count, mmhub_err_count) have format:
    ce: N
    ue: N

Usage:
    python3 gpu/amd-collector.py [--output PATH]
"""
import argparse
import sys
import time
from pathlib import Path

DEFAULT_OUTPUT = Path("/var/lib/node_exporter/textfile_collector/amd_gpu.prom")
AMD_VENDOR_ID  = "0x1002"
SYSFS_DRM      = Path("/sys/class/drm")
SYSFS_MODULE   = Path("/sys/module/amdgpu")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _amdgpu_module_present(sysfs_module: Path = SYSFS_MODULE) -> bool:
    """Return True if the amdgpu kernel module is loaded."""
    return sysfs_module.exists()


def _read_text_safe(path: Path) -> str:
    """Read a file and return its text, or '' on any error."""
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def detect_amd_cards(drm_root: Path = SYSFS_DRM) -> list:
    """
    Scan drm_root for card* entries whose device/vendor == AMD_VENDOR_ID.
    Returns sorted list of card names (e.g. ['card0', 'card1']).
    Degrades gracefully if drm_root does not exist.
    """
    if not drm_root.exists():
        return []

    cards = []
    for card_path in sorted(drm_root.glob("card*")):
        vendor_file = card_path / "device" / "vendor"
        if not vendor_file.exists():
            continue
        vendor = _read_text_safe(vendor_file).lower()
        if vendor == AMD_VENDOR_ID.lower():
            cards.append(card_path.name)

    return cards


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def _parse_ras_file(content: str) -> dict:
    """
    Parse a RAS sysfs file with lines like:
        ce: N
        ue: N
    Returns dict with keys 'correctable' and/or 'uncorrectable'.
    Returns empty dict if content is unparseable.
    """
    result = {}
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("ce:"):
            try:
                result["correctable"] = int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                pass
        elif line.startswith("ue:"):
            try:
                result["uncorrectable"] = int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                pass
    return result


def collect_ras_errors(card: str, drm_root: Path = SYSFS_DRM) -> list:
    """
    Read RAS error counts for a given card from its ras/ subdirectory.
    Returns list of dicts: {card, block, error_type, count}.
    Returns [] if ras/ directory is absent or empty.
    """
    ras_dir = drm_root / card / "device" / "ras"
    if not ras_dir.is_dir():
        return []

    events = []
    for ras_file in sorted(ras_dir.iterdir()):
        if not ras_file.is_file():
            continue
        # Derive block name from filename, e.g. umc_err_count → umc
        block = ras_file.name.replace("_err_count", "").replace("_error_count", "")
        content = _read_text_safe(ras_file)
        if not content:
            continue
        counts = _parse_ras_file(content)
        for error_type, count in counts.items():
            events.append({
                "card": card,
                "block": block,
                "error_type": error_type,
                "count": count,
            })

    return events


def collect_gpu_metrics(card: str, drm_root: Path = SYSFS_DRM) -> dict:
    """
    Read utilisation and VRAM metrics for a given card.
    Returns dict: {utilization_percent, vram_used_bytes, vram_total_bytes}.
    Missing files produce None values.
    """
    device = drm_root / card / "device"

    def _read_int(rel_path: str):
        val = _read_text_safe(device / rel_path)
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    def _read_float(rel_path: str):
        val = _read_text_safe(device / rel_path)
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    return {
        "utilization_percent": _read_float("gpu_busy_percent"),
        "vram_used_bytes":     _read_int("mem_info_vram_used"),
        "vram_total_bytes":    _read_int("mem_info_vram_total"),
    }


# ---------------------------------------------------------------------------
# Metric emission
# ---------------------------------------------------------------------------

def emit_metrics(
    amd_present: bool,
    ras_errors: list,
    gpu_metrics: dict,   # {card: {utilization_percent, vram_used_bytes, vram_total_bytes}}
    now: float,
) -> str:
    lines = []

    lines += [
        "# HELP amd_collector_up 1 if AMD GPU detected and collector operational, 0 otherwise.",
        "# TYPE amd_collector_up gauge",
        f"amd_collector_up {1 if amd_present else 0}",
    ]

    if amd_present:
        lines += [
            "# HELP amd_gpu_ras_error_total Total AMD GPU RAS errors per block and type.",
            "# TYPE amd_gpu_ras_error_total counter",
        ]
        for ev in ras_errors:
            lines.append(
                f'amd_gpu_ras_error_total{{card="{ev["card"]}",'
                f'block="{ev["block"]}",error_type="{ev["error_type"]}"}} {ev["count"]}'
            )

        lines += [
            "# HELP amd_gpu_utilization_percent Current GPU utilisation as a percentage.",
            "# TYPE amd_gpu_utilization_percent gauge",
        ]
        for card, metrics in sorted(gpu_metrics.items()):
            if metrics["utilization_percent"] is not None:
                lines.append(
                    f'amd_gpu_utilization_percent{{card="{card}"}}'
                    f' {metrics["utilization_percent"]:.1f}'
                )

        lines += [
            "# HELP amd_gpu_vram_used_bytes Current VRAM in use in bytes.",
            "# TYPE amd_gpu_vram_used_bytes gauge",
        ]
        for card, metrics in sorted(gpu_metrics.items()):
            if metrics["vram_used_bytes"] is not None:
                lines.append(
                    f'amd_gpu_vram_used_bytes{{card="{card}"}}'
                    f' {metrics["vram_used_bytes"]}'
                )

        lines += [
            "# HELP amd_gpu_vram_total_bytes Total VRAM available in bytes.",
            "# TYPE amd_gpu_vram_total_bytes gauge",
        ]
        for card, metrics in sorted(gpu_metrics.items()):
            if metrics["vram_total_bytes"] is not None:
                lines.append(
                    f'amd_gpu_vram_total_bytes{{card="{card}"}}'
                    f' {metrics["vram_total_bytes"]}'
                )

    lines += [
        "# HELP amd_collector_last_run_timestamp Unix timestamp of the last AMD collector run.",
        "# TYPE amd_collector_last_run_timestamp gauge",
        f"amd_collector_last_run_timestamp {now:.3f}",
    ]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():  # pragma: no cover
    parser = argparse.ArgumentParser(
        description="Collect AMD GPU RAS/utilisation metrics and emit Prometheus textfile output."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output .prom file (- for stdout)",
    )
    parser.add_argument(
        "--drm-root",
        type=Path,
        default=SYSFS_DRM,
        help="Override /sys/class/drm root (for testing)",
    )
    args = parser.parse_args()

    now = time.time()

    if not _amdgpu_module_present():
        sys.stderr.write("amd-collector: amdgpu module not loaded, emitting collector_up=0\n")
        output = emit_metrics(False, [], {}, now)
    else:
        cards = detect_amd_cards(args.drm_root)
        if not cards:
            sys.stderr.write("amd-collector: no AMD GPU (vendor 0x1002) found, emitting collector_up=0\n")
            output = emit_metrics(False, [], {}, now)
        else:
            sys.stderr.write(f"amd-collector: detected {len(cards)} AMD GPU(s): {', '.join(cards)}\n")
            all_ras = []
            all_metrics = {}
            for card in cards:
                all_ras.extend(collect_ras_errors(card, args.drm_root))
                all_metrics[card] = collect_gpu_metrics(card, args.drm_root)
            output = emit_metrics(True, all_ras, all_metrics, now)

    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        sys.stderr.write(f"amd-collector: wrote to {args.output}\n")


if __name__ == "__main__":  # pragma: no cover
    main()
