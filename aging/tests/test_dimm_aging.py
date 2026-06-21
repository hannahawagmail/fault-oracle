# SPDX-License-Identifier: Apache-2.0
import math
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from dimm_aging_report import (
    linear_regression, days_to_threshold, health_score,
    analyze_series, emit_prometheus
)

LABELS = 'instance="node1",mc="0",csrow="0"'


class TestLinearRegression:
    def test_perfect_fit(self):
        # y = 2x + 1
        xs = [0, 1, 2, 3, 4]
        ys = [1, 3, 5, 7, 9]
        slope, intercept, r_sq = linear_regression(xs, ys)
        assert math.isclose(slope, 2.0, rel_tol=1e-9)
        assert math.isclose(intercept, 1.0, rel_tol=1e-9)
        assert math.isclose(r_sq, 1.0, rel_tol=1e-9)

    def test_flat_series(self):
        xs = [0, 1, 2, 3]
        ys = [5.0, 5.0, 5.0, 5.0]
        slope, intercept, r_sq = linear_regression(xs, ys)
        assert slope == 0.0
        assert math.isclose(intercept, 5.0, rel_tol=1e-9)

    def test_single_point_returns_zero_slope(self):
        slope, intercept, r_sq = linear_regression([0], [3.0])
        assert slope == 0.0
        assert intercept == 3.0

    def test_empty_returns_zeros(self):
        slope, intercept, r_sq = linear_regression([], [])
        assert slope == 0.0
        assert intercept == 0.0


class TestDaysToThreshold:
    def test_reaches_threshold(self):
        # rate=0, slope=1/day, threshold=10 → 10 days
        assert math.isclose(days_to_threshold(0.0, 1.0, 10.0), 10.0)

    def test_already_exceeded(self):
        assert days_to_threshold(15.0, 1.0, 10.0) == 0.0

    def test_zero_slope_returns_inf(self):
        assert days_to_threshold(5.0, 0.0, 10.0) == float("inf")

    def test_negative_slope_returns_inf(self):
        assert days_to_threshold(5.0, -0.5, 10.0) == float("inf")


class TestHealthScore:
    def test_zero_rate_is_perfect(self):
        assert health_score(0.0, 10.0, 100.0) == 1.0

    def test_at_critical_is_zero(self):
        assert health_score(100.0, 10.0, 100.0) == 0.0

    def test_above_critical_is_zero(self):
        assert health_score(200.0, 10.0, 100.0) == 0.0

    def test_midpoint_half(self):
        assert math.isclose(health_score(50.0, 10.0, 100.0), 0.5)


class TestAnalyzeSeries:
    def _make_points(self, rates, days_apart=1):
        t0 = 0.0
        return [(t0 + i * 86400 * days_apart, r) for i, r in enumerate(rates)]

    def test_slope_positive_for_worsening(self):
        points = self._make_points([0, 1, 2, 3, 4])
        a = analyze_series(LABELS, points, 10.0, 100.0)
        assert a["slope"] > 0

    def test_health_decreases_as_rate_rises(self):
        a_low  = analyze_series(LABELS, self._make_points([0, 1, 2, 3, 4]),  10.0, 100.0)
        a_high = analyze_series(LABELS, self._make_points([0, 10, 20, 30, 40]), 10.0, 100.0)
        assert a_high["health"] < a_low["health"]

    def test_empty_points_returns_healthy(self):
        a = analyze_series(LABELS, [], 10.0, 100.0)
        assert a["health"] == 1.0
        assert a["slope"] == 0.0


class TestEmitPrometheus:
    def test_has_help_lines(self):
        out = emit_prometheus([], timestamp=1234567890.0)
        assert "# HELP dimm_aging_ce_rate_slope" in out
        assert "# HELP dimm_aging_health_score" in out

    def test_slope_in_output(self):
        analyses = [{"labels": LABELS, "slope": 2.5, "days_to_threshold": 10.0,
                     "health": 0.8, "r_squared": 0.95}]
        out = emit_prometheus(analyses, timestamp=0)
        assert f"dimm_aging_ce_rate_slope{{{LABELS}}} 2.500000" in out

    def test_timestamp_in_output(self):
        out = emit_prometheus([], timestamp=9999999.123)
        assert "dimm_aging_report_timestamp 9999999.123" in out
