#!/usr/bin/env python3
"""Generate SVG visualization of DIMM slot topology with health coloring."""
from __future__ import annotations

import argparse
import json
import sys


def get_color(ce_count: int, ue_count: int) -> str:
    """Return fill color based on error counts."""
    if ue_count > 0:
        return "#e74c3c"  # red
    if ce_count > 100:
        return "#e67e22"  # orange
    if ce_count > 0:
        return "#f1c40f"  # yellow
    return "#2ecc71"  # green


def generate_svg(dimms: list[dict]) -> str:
    """Generate SVG string from DIMM records."""
    if not dimms:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"></svg>'

    cell_w, cell_h, pad = 150, 80, 10
    nodes = sorted(set(d["node"] for d in dimms))
    channels = sorted(set(d["channel"] for d in dimms))
    node_idx = {n: i for i, n in enumerate(nodes)}
    chan_idx = {c: i for i, c in enumerate(channels)}

    width = len(channels) * (cell_w + pad) + pad
    height = len(nodes) * (cell_h + pad) + pad

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
    ]

    for d in dimms:
        row, col = node_idx[d["node"]], chan_idx[d["channel"]]
        x = pad + col * (cell_w + pad)
        y = pad + row * (cell_h + pad)
        color = get_color(d.get("ce_count", 0), d.get("ue_count", 0))
        slot = d.get("slot", "")
        ce = d.get("ce_count", 0)
        ue = d.get("ue_count", 0)
        parts.append(
            f'<rect x="{x}" y="{y}" width="{cell_w}" height="{cell_h}" '
            f'fill="{color}" stroke="#333" rx="4"/>'
        )
        parts.append(
            f'<text x="{x + cell_w // 2}" y="{y + 30}" text-anchor="middle" '
            f'font-size="12" font-family="monospace">{slot}</text>'
        )
        parts.append(
            f'<text x="{x + cell_w // 2}" y="{y + 55}" text-anchor="middle" '
            f'font-size="11" font-family="monospace">CE:{ce} UE:{ue}</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="DIMM topology SVG visualizer")
    parser.add_argument(
        "--input", default="-", help="JSON input file (default: stdin)"
    )
    parser.add_argument(
        "--output", default="-", help="SVG output file (default: stdout)"
    )
    args = parser.parse_args()

    if args.input == "-":
        data = json.load(sys.stdin)
    else:
        with open(args.input) as f:
            data = json.load(f)

    svg = generate_svg(data)

    if args.output == "-":
        sys.stdout.write(svg)
    else:
        with open(args.output, "w") as f:
            f.write(svg)


if __name__ == "__main__":
    main()
