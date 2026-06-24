from __future__ import annotations

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from fleet_heatmap import color_for_node, generate_svg


def test_valid_svg_output():
    nodes = [{"node": "rack01-u01", "rack": "rack01", "position": 1, "ce_rate": 5.2, "ue_count": 0}]
    svg = generate_svg(nodes)
    assert svg.startswith("<svg")
    assert svg.strip().endswith("</svg>")
    assert "xmlns" in svg


def test_color_green_zero():
    assert color_for_node(0, 0) == "#00AA00"


def test_color_yellow():
    assert color_for_node(5, 0) == "#FFD700"


def test_color_orange():
    assert color_for_node(25, 0) == "#FF8C00"


def test_color_red():
    assert color_for_node(60, 0) == "#FF0000"


def test_color_dark_red_ue():
    assert color_for_node(0, 1) == "#8B0000"
    assert color_for_node(100, 3) == "#8B0000"


def test_empty_rack_list():
    svg = generate_svg([])
    assert svg.startswith("<svg")
    assert svg.strip().endswith("</svg>")
    assert "rect" in svg  # empty cells still rendered


def test_node_with_ue_in_svg():
    nodes = [{"node": "rack02-u05", "rack": "rack02", "position": 5, "ce_rate": 3.0, "ue_count": 2}]
    svg = generate_svg(nodes)
    assert "#8B0000" in svg


def test_tooltip_contains_node_info():
    nodes = [{"node": "rack01-u01", "rack": "rack01", "position": 1, "ce_rate": 7.1, "ue_count": 0}]
    svg = generate_svg(nodes)
    assert "rack01-u01" in svg
    assert "CE:7.1/hr" in svg
