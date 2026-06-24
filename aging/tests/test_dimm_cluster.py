"""Tests for DIMM cluster analysis."""
from __future__ import annotations

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from dimm_cluster_analysis import DIMMRecord, analyze_clusters


def _make_dimm(vendor="acme", part="X100", date_code="2025W01", failure_probability=0.01, **kw):
    defaults = dict(serial="S001", node="n1", slot="A1", ce_count=5, ue_count=0)
    defaults.update(kw)
    return DIMMRecord(vendor=vendor, part_number=part, date_code=date_code,
                      failure_probability=failure_probability, **defaults)


def test_batch_with_5x_fleet_average_detected():
    # 20 normal DIMMs + 5 bad DIMMs from a different vendor with 5x rate
    normal = [_make_dimm(serial=f"N{i}", failure_probability=0.01) for i in range(20)]
    bad = [_make_dimm(serial=f"B{i}", vendor="badcorp", part="Y200",
                      date_code="2025W10", failure_probability=0.10) for i in range(5)]
    alerts = analyze_clusters(normal + bad)
    vendor_alerts = [a for a in alerts if a.cluster_type == "vendor" and "badcorp" in a.cluster_key]
    assert len(vendor_alerts) == 1
    assert vendor_alerts[0].affected_count == 5
    assert vendor_alerts[0].cluster_rate > vendor_alerts[0].fleet_average_rate * 3


def test_normal_distribution_not_flagged():
    dimms = [_make_dimm(serial=f"N{i}", failure_probability=0.01) for i in range(20)]
    alerts = analyze_clusters(dimms)
    assert alerts == []


def test_empty_input_returns_empty():
    assert analyze_clusters([]) == []
