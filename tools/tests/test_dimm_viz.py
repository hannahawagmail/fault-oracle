"""Tests for dimm_topology_viz module."""
from __future__ import annotations

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from dimm_topology_viz import generate_svg, get_color


def test_svg_header():
    dimms = [{"serial": "A", "slot": "DIMM0", "node": 0, "channel": 0,
              "ce_count": 0, "ue_count": 0, "status": "ok"}]
    svg = generate_svg(dimms)
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert svg.endswith("</svg>")


def test_color_green():
    assert get_color(0, 0) == "#2ecc71"


def test_color_yellow():
    assert get_color(50, 0) == "#f1c40f"


def test_color_orange():
    assert get_color(200, 0) == "#e67e22"


def test_color_red_ue():
    assert get_color(0, 1) == "#e74c3c"
    assert get_color(200, 5) == "#e74c3c"


def test_empty_input():
    svg = generate_svg([])
    assert '<svg xmlns="http://www.w3.org/2000/svg"' in svg
    assert "</svg>" in svg
