# SPDX-License-Identifier: Apache-2.0
import math
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from zscore_detector import compute_stats, compute_zscore, emit_metrics, _window_to_seconds


class TestComputeStats:
    def test_mean(self):
        mean, _, _clamped = compute_stats([1.0, 2.0, 3.0, 4.0, 5.0])
        assert math.isclose(mean, 3.0)

    def test_stddev(self):
        _, stddev, _clamped = compute_stats([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
        assert math.isclose(stddev, 2.0, rel_tol=1e-5)

    def test_empty_returns_defaults(self):
        mean, stddev, clamped = compute_stats([])
        assert mean == 0.0
        assert stddev == 1.0
        assert clamped is True

    def test_constant_series_no_division_by_zero(self):
        mean, stddev, clamped = compute_stats([5.0, 5.0, 5.0])
        assert mean == 5.0
        assert stddev == 1.0   # clamped to avoid div-by-zero
        assert clamped is True

    def test_single_value(self):
        mean, stddev, clamped = compute_stats([42.0])
        assert mean == 42.0
        assert stddev == 1.0   # variance=0, clamped
        assert clamped is True

    def test_real_stddev_not_clamped(self):
        _, _, clamped = compute_stats([1.0, 2.0, 3.0, 4.0, 5.0])
        assert clamped is False


class TestComputeZScore:
    def test_zero_when_at_mean(self):
        assert compute_zscore(5.0, 5.0, 1.0) == 0.0

    def test_positive_above_mean(self):
        z = compute_zscore(8.0, 5.0, 1.0)
        assert z == 3.0

    def test_negative_below_mean(self):
        z = compute_zscore(2.0, 5.0, 1.0)
        assert z == -3.0

    def test_large_deviation(self):
        z = compute_zscore(100.0, 0.0, 1.0)
        assert z == 100.0


class TestWindowToSeconds:
    def test_minutes(self):
        assert _window_to_seconds("5m") == 300
    def test_hours(self):
        assert _window_to_seconds("6h") == 21600
    def test_days(self):
        assert _window_to_seconds("7d") == 604800
    def test_weeks(self):
        assert _window_to_seconds("1w") == 604800


class TestEmitMetrics:
    LABELS = 'instance="node1",collector="edac"'

    def test_zscore_in_output(self):
        data = {self.LABELS: {"zscore": 4.5, "mean": 1.0, "stddev": 0.5}}
        out = emit_metrics(data, threshold=3.0)
        assert f'anomaly_zscore{{{self.LABELS}}} 4.5000' in out

    def test_anomaly_detected_when_above_threshold(self):
        data = {self.LABELS: {"zscore": 4.5, "mean": 1.0, "stddev": 0.5}}
        out = emit_metrics(data, threshold=3.0)
        assert f'anomaly_detected{{{self.LABELS}}} 1' in out

    def test_no_anomaly_below_threshold(self):
        data = {self.LABELS: {"zscore": 1.2, "mean": 1.0, "stddev": 0.5}}
        out = emit_metrics(data, threshold=3.0)
        assert f'anomaly_detected{{{self.LABELS}}} 0' in out

    def test_negative_zscore_triggers_detection(self):
        # |Z| > threshold regardless of sign
        data = {self.LABELS: {"zscore": -3.5, "mean": 5.0, "stddev": 0.5}}
        out = emit_metrics(data, threshold=3.0)
        assert f'anomaly_detected{{{self.LABELS}}} 1' in out

    def test_baseline_mean_and_stddev_emitted(self):
        data = {self.LABELS: {"zscore": 0.0, "mean": 2.5, "stddev": 0.8}}
        out = emit_metrics(data, threshold=3.0)
        assert 'anomaly_baseline_mean' in out
        assert 'anomaly_baseline_stddev' in out

    def test_timestamp_present(self):
        out = emit_metrics({}, threshold=3.0)
        assert 'anomaly_detector_last_run_timestamp' in out

    def test_type_annotations_correct(self):
        data = {self.LABELS: {"zscore": 1.0, "mean": 1.0, "stddev": 1.0}}
        out = emit_metrics(data, threshold=3.0)
        assert '# TYPE anomaly_zscore gauge' in out
        assert '# TYPE anomaly_detected gauge' in out

    def test_multiple_nodes(self):
        data = {
            'instance="node1"': {"zscore": 0.5, "mean": 1.0, "stddev": 1.0},
            'instance="node2"': {"zscore": 5.0, "mean": 1.0, "stddev": 1.0},
        }
        out = emit_metrics(data, threshold=3.0)
        assert 'instance="node1"} 0' in out
        assert 'instance="node2"} 1' in out

    def test_empty_produces_valid_prom(self):
        out = emit_metrics({}, threshold=3.0)
        assert '# HELP anomaly_zscore' in out
        assert '# HELP anomaly_detected' in out
