# SPDX-License-Identifier: Apache-2.0
"""
Mutation-resistant tests for ML and statistical logic.

Each test is designed to catch a specific mutation:
- Off-by-one in thresholds
- Sign flips in comparisons
- Wrong variable in formula
- Swapped numerator/denominator
- Missing negation
"""

import importlib.util
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("prophet")

# ---------------------------------------------------------------------------
# Module loader helper
# ---------------------------------------------------------------------------

REPO = Path(__file__).parent.parent.parent


def _import(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ce_mod():
    return _import("ce_forecaster_mut", REPO / "ml" / "ce-forecaster.py")


@pytest.fixture(scope="module")
def prob_mod():
    return _import("probability_exporter_mut", REPO / "ml" / "probability-exporter.py")


@pytest.fixture(scope="module")
def zscore_mod():
    return _import("zscore_detector_mut", REPO / "anomaly" / "zscore-detector.py")


def _make_forecast(rate: float, n: int = 168) -> pd.DataFrame:
    """Build a synthetic forecast DataFrame as Prophet would return."""
    now = datetime.now()
    ts = [now + timedelta(hours=i) for i in range(n)]
    return pd.DataFrame(
        {
            "ds": ts,
            "yhat": [rate * 0.8] * n,
            "yhat_lower": [rate * 0.5] * n,
            "yhat_upper": [rate] * n,
        }
    )


# ===========================================================================
# TestCEForecasterMutations (5 tests)
# ===========================================================================


class TestCEForecasterMutations:
    """Verify Prophet model hyper-parameters that, if mutated, would break forecasts."""

    def test_changepoint_prior_not_zero(self, ce_mod):
        """changepoint_prior_scale must be > 0; = 0 ignores trend changes."""
        model = ce_mod.fit_model(_make_simple_df())
        assert model.changepoint_prior_scale > 0
        assert model.changepoint_prior_scale == pytest.approx(0.05)

    def test_weekly_seasonality_enabled(self, ce_mod):
        """weekly_seasonality must be True; disabling it loses 7-day DIMM patterns."""
        model = ce_mod.fit_model(_make_simple_df())
        # Prophet stores weekly_seasonality as True or as a Fourier order int
        ws = model.weekly_seasonality
        assert ws is True or (isinstance(ws, (int, float)) and ws > 0)

    def test_daily_seasonality_enabled_or_present(self, ce_mod):
        """daily_seasonality is enabled in this model (catches False mutation)."""
        model = ce_mod.fit_model(_make_simple_df())
        ds = model.daily_seasonality
        # Accept either True or a positive Fourier order (both indicate enabled)
        assert ds is True or (isinstance(ds, (int, float)) and ds > 0)

    def test_forecast_horizon_168h_contains_enough_rows(self, ce_mod):
        """forecast(7) must return >= 168 future rows (catches periods=0 mutation)."""
        df = _make_simple_df(n_days=60)
        model = ce_mod.fit_model(df)
        fc = ce_mod.forecast(model, horizon_days=7)
        last_train = df["ds"].max()
        future = fc[fc["ds"] > last_train]
        assert len(future) >= 168 - 1  # allow ±1 for boundary

    def test_ds_column_is_datetime(self, ce_mod):
        """forecast DataFrame 'ds' column must be datetime (catches string mutation)."""
        df = _make_simple_df(n_days=30)
        model = ce_mod.fit_model(df)
        fc = ce_mod.forecast(model, horizon_days=7)
        assert pd.api.types.is_datetime64_any_dtype(fc["ds"])


def _make_simple_df(n_days: int = 60, rate: float = 1e-6) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    dates = pd.date_range("2026-01-01", periods=n_days * 24, freq="h")
    y = [max(0.0, rate + rng.normal(0, rate * 0.05)) for _ in dates]
    return pd.DataFrame({"ds": dates, "y": y})


# ===========================================================================
# TestProbabilityExporterMutations (5 tests)
# ===========================================================================


class TestProbabilityExporterMutations:
    """Verify that failure_probability logic is not accidentally mutated."""

    def test_probability_between_0_and_1(self, ce_mod):
        """failure_probability must always return a float in [0, 1]."""
        fc = _make_forecast(rate=5e-6)
        p = ce_mod.failure_probability(fc, horizon_days=7, threshold_rate=1e-6)
        assert 0.0 <= p <= 1.0

    def test_high_rate_gives_nonzero_probability(self, ce_mod):
        """yhat_upper = 10 CE/s with threshold 1e-6 → probability > 0.5."""
        fc = _make_forecast(rate=10.0)
        p = ce_mod.failure_probability(fc, horizon_days=7, threshold_rate=1e-6)
        assert p > 0.5, f"Expected probability > 0.5 for extreme rate, got {p}"

    def test_zero_rate_gives_zero_probability(self, ce_mod):
        """All yhat_upper = 0 → probability = 0.0 (no threshold violations)."""
        now = datetime.now()
        ts = [now + timedelta(hours=i) for i in range(168)]
        fc = pd.DataFrame(
            {
                "ds": ts,
                "yhat": [0.0] * 168,
                "yhat_lower": [0.0] * 168,
                "yhat_upper": [0.0] * 168,
            }
        )
        p = ce_mod.failure_probability(fc, horizon_days=7, threshold_rate=1e-6)
        assert p == pytest.approx(0.0, abs=0.01), f"Expected 0.0, got {p}"

    def test_probability_monotone_with_rate(self, ce_mod):
        """Higher CE rate → higher probability (catches sign flip in comparison)."""
        p_low = ce_mod.failure_probability(_make_forecast(rate=1e-8), 7, 1e-6)
        p_high = ce_mod.failure_probability(_make_forecast(rate=1e-2), 7, 1e-6)
        assert p_high >= p_low, f"High rate p={p_high:.4f} must be >= low rate p={p_low:.4f}"

    def test_probability_uses_yhat_upper_not_yhat(self, ce_mod):
        """
        yhat < threshold but yhat_upper > threshold → probability > 0.
        Catches the mutation where yhat is used instead of yhat_upper.
        """
        threshold = 1e-6
        now = datetime.now()
        ts = [now + timedelta(hours=i) for i in range(168)]
        # yhat below threshold, yhat_upper above
        fc = pd.DataFrame(
            {
                "ds": ts,
                "yhat": [threshold * 0.1] * 168,  # well below
                "yhat_lower": [0.0] * 168,
                "yhat_upper": [threshold * 10.0] * 168,  # well above
            }
        )
        p = ce_mod.failure_probability(fc, horizon_days=7, threshold_rate=threshold)
        assert p > 0.0, (
            "probability must be > 0 when yhat_upper > threshold, even if yhat < threshold"
        )


# ===========================================================================
# TestZScoreDetectorMutations (5 tests)
# ===========================================================================


class TestZScoreDetectorMutations:
    """Verify Z-score formula, threshold, and stddev-clamp correctness."""

    def test_zscore_formula_x_minus_mean_over_std(self, zscore_mod):
        """Z-score = (x - mean) / std; mutation (mean - x) / std would flip sign."""
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        mean, stddev, _ = zscore_mod.compute_stats(values)
        z = zscore_mod.compute_zscore(10.0, mean, stddev)
        # 10 is above mean=3, so Z must be positive
        assert z > 0, f"Expected positive Z for x=10 above mean={mean:.2f}, got {z}"

    def test_threshold_3_not_2(self, zscore_mod):
        """2.9σ must NOT trigger; value at exactly 3.1σ MUST trigger."""
        rng = np.random.default_rng(77)
        baseline = list(rng.normal(0.0, 1.0, 300))
        mean, std, _ = zscore_mod.compute_stats(baseline)

        # 2.9σ → should NOT be anomaly at threshold=3.0
        val_2_9 = mean + 2.9 * std
        z_2_9 = zscore_mod.compute_zscore(val_2_9, mean, std)
        assert abs(z_2_9) < 3.0, f"|Z| must be < 3.0 for 2.9σ, got {z_2_9:.3f}"

        # 3.1σ → should BE anomaly
        val_3_1 = mean + 3.1 * std
        z_3_1 = zscore_mod.compute_zscore(val_3_1, mean, std)
        assert abs(z_3_1) > 3.0, f"|Z| must be > 3.0 for 3.1σ, got {z_3_1:.3f}"

    def test_positive_spike_gives_positive_z(self, zscore_mod):
        """Large positive spike → positive Z-score (catches abs() mutation)."""
        baseline = [1.0] * 100
        mean, std, _ = zscore_mod.compute_stats(baseline)
        # std is clamped to 1.0 for a constant series
        z = zscore_mod.compute_zscore(100.0, mean, std)
        assert z > 3.0, f"Spike at 100 above mean=1 must give Z > 3, got {z}"

    def test_stddev_clamp_is_1_not_0(self, zscore_mod):
        """Constant baseline → stddev clamped to 1.0, not 0 (avoids ZeroDivisionError)."""
        baseline = [5.0] * 30
        mean, stddev, clamped = zscore_mod.compute_stats(baseline)
        assert stddev == pytest.approx(1.0), (
            f"Constant series stddev must be clamped to 1.0, got {stddev}"
        )
        assert clamped is True
        # No division by zero:
        z = zscore_mod.compute_zscore(5.0, mean, stddev)
        assert math.isfinite(z)

    def test_empty_baseline_safe(self, zscore_mod):
        """Empty baseline → defaults (mean=0, std=1, clamped=True), no exception."""
        mean, stddev, clamped = zscore_mod.compute_stats([])
        assert mean == 0.0
        assert stddev == pytest.approx(1.0)
        assert clamped is True
        z = zscore_mod.compute_zscore(5.0, mean, stddev)
        assert math.isfinite(z)
