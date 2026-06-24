from __future__ import annotations

import argparse
import json
import sys


def color_for_node(ce_rate: float, ue_count: int) -> str:
    if ue_count > 0:
        return "#8B0000"
    if ce_rate >= 50:
        return "#FF0000"
    if ce_rate >= 10:
        return "#FF8C00"
    if ce_rate >= 1:
        return "#FFD700"
    return "#00AA00"


def generate_svg(nodes: list[dict]) -> str:
    cell_w, cell_h = 18, 22
    margin_left, margin_top = 60, 30
    cols, rows = 42, 10
    width = margin_left + cols * cell_w + 10
    height = margin_top + rows * cell_h + 10

    lookup: dict[tuple[str, int], dict] = {}
    for n in nodes:
        lookup[(n["rack"], n["position"])] = n

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        '<style>text{font:10px sans-serif}rect.cell:hover{stroke:#000;stroke-width:2}</style>',
    ]

    for col in range(1, cols + 1):
        x = margin_left + (col - 1) * cell_w
        lines.append(f'<text x="{x + 2}" y="{margin_top - 5}" font-size="7">U{col}</text>')

    for row_idx in range(rows):
        rack = f"rack{row_idx + 1:02d}"
        y = margin_top + row_idx * cell_h
        lines.append(f'<text x="2" y="{y + 15}" font-size="9">{rack}</text>')
        for col in range(1, cols + 1):
            x = margin_left + (col - 1) * cell_w
            node = lookup.get((rack, col))
            if node:
                fill = color_for_node(node["ce_rate"], node["ue_count"])
                title = f'{node["node"]} CE:{node["ce_rate"]}/hr'
            else:
                fill = "#EEEEEE"
                title = f"{rack}-u{col:02d} (empty)"
            lines.append(
                f'<rect class="cell" x="{x}" y="{y}" width="{cell_w - 1}" '
                f'height="{cell_h - 1}" fill="{fill}">'
                f"<title>{title}</title></rect>"
            )

    lines.append("</svg>")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fleet error heatmap SVG generator")
    parser.add_argument("--input", required=True, help="JSON file with node array")
    parser.add_argument("--output", help="Output SVG file (default: stdout)")
    args = parser.parse_args()

    with open(args.input) as f:
        nodes = json.load(f)

    svg = generate_svg(nodes)

    if args.output:
        with open(args.output, "w") as f:
            f.write(svg)
    else:
        sys.stdout.write(svg)


if __name__ == "__main__":
    main()
