# SPDX-License-Identifier: Apache-2.0
"""Tests for the ML prediction pipeline (D1–D4)."""
import json
import math
import os
import pickle
import sys
import time
from pathlib import Path
import pytest
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from ce_forecaster import (  # type: ignore[attr-defined]
    fit_model, forecast, failure_probability, build_synthetic_df
)
from probability_exporter import (
    risk_tier, emit_metrics, RISK_TIERS
)
from survival_analysis import (
    build_synthetic_events, fit_km, median_lifetime, emit_metrics as emit_survival
)
from ai_runbook import fingerprint, build_context

# ── Helpers ──────────────────────────────────────────────────────────────────

def make_df(n_days=60, base_rate=1e-6, slope=0.0, noise=0.05):
    """Generate synthetic CE rate time series."""
    dates = pd.date_range("2026-01-01", periods=n_days * 24, freq="h")
    vals  = [max(0, base_rate + slope * i / 24 + np.random.normal(0, noise * base_rate))
             for i in range(len(dates))]
    return pd.DataFrame({"ds": dates, "y": vals})


# ── ce-forecaster tests ───────────────────────────────────────────────────────

class TestFitModel:
    def test_returns_prophet_model(self):
        from prophet import Prophet
        df = make_df()
        m  = fit_model(df)
        assert isinstance(m, Prophet)

    def test_model_has_trend(self):
        df = make_df(slope=1e-7)
        m  = fit_model(df)
        fc = forecast(m, 7)
        assert "trend" in fc.columns

    def test_forecast_has_required_columns(self):
        df = make_df()
        m  = fit_model(df)
        fc = forecast(m, 30)
        for col in ["ds", "yhat", "yhat_lower", "yhat_upper"]:
            assert col in fc.columns

    def test_forecast_horizon(self):
        df = make_df()
        m  = fit_model(df)
        fc = forecast(m, 14)
        # Should have future rows (at least 14*24 beyond last training point)
        last_train = df["ds"].max()
        future_rows = fc[fc["ds"] > last_train]
        assert len(future_rows) >= 14 * 24 - 1

    def test_worsening_trend_increases_probability(self):
        df_flat  = make_df(n_days=60, slope=0.0)
        df_worse = make_df(n_days=60, slope=5e-8)
        m_flat  = fit_model(df_flat)
        m_worse = fit_model(df_worse)
        p_flat  = failure_probability(forecast(m_flat, 7),  7)
        p_worse = failure_probability(forecast(m_worse, 7), 7)
        # Worsening trend should have equal or higher probability
        assert p_worse >= p_flat - 0.05   # small tolerance for noise


class TestFailureProbability:
    def test_returns_value_between_0_and_1(self):
        df = make_df()
        m  = fit_model(df)
        fc = forecast(m, 7)
        p  = failure_probability(fc, 7)
        assert 0.0 <= p <= 1.0

    def test_7d_le_30d(self):
        df = make_df(slope=2e-8)
        m  = fit_model(df)
        fc = forecast(m, 30)
        p7  = failure_probability(fc, 7)
        p30 = failure_probability(fc, 30)
        assert p7 <= p30 + 0.05   # small tolerance


# ── probability-exporter tests ───────────────────────────────────────────────

class TestRiskTier:
    def test_critical_above_0_8(self):
        assert risk_tier(0.9) == "critical"
    def test_high_above_0_5(self):
        assert risk_tier(0.6) == "high"
    def test_medium_above_0_2(self):
        assert risk_tier(0.3) == "medium"
    def test_low_below_0_2(self):
        assert risk_tier(0.1) == "low"
    def test_zero_is_low(self):
        assert risk_tier(0.0) == "low"
    def test_one_is_critical(self):
        assert risk_tier(1.0) == "critical"


class TestEmitProbabilityMetrics:
    ENTRY = {
        "label_key":              'instance="n1",mc="0",csrow="0"',
        "failure_probability_7d":  0.72,
        "failure_probability_30d": 0.91,
        "model_path":              "/nonexistent/model.pkl",
        "trained_at":              time.time() - 3600,
    }

    def test_7d_metric_present(self):
        out = emit_metrics([self.ENTRY], time.time())
        assert "failure_probability_7d" in out

    def test_30d_metric_present(self):
        out = emit_metrics([self.ENTRY], time.time())
        assert "failure_probability_30d" in out

    def test_risk_tier_all_four_emitted(self):
        out = emit_metrics([self.ENTRY], time.time())
        for tier in ["low", "medium", "high", "critical"]:
            assert f'tier="{tier}"' in out

    def test_model_age_in_output(self):
        out = emit_metrics([self.ENTRY], time.time())
        assert "ml_model_age_hours" in out

    def test_empty_entries_still_valid(self):
        out = emit_metrics([], time.time())
        assert "ml_models_loaded_total 0" in out


# ── survival analysis tests ───────────────────────────────────────────────────

class TestKaplanMeier:
    @pytest.fixture(scope="class")
    def km_df(self):
        return build_synthetic_events()

    def test_synthetic_df_has_required_columns(self, km_df):
        assert "cohort" in km_df.columns
        assert "duration_days" in km_df.columns
        assert "observed" in km_df.columns

    def test_km_fits_without_error(self, km_df):
        cohort = km_df["cohort"].iloc[0]
        kmf    = fit_km(km_df, cohort)
        assert kmf is not None

    def test_survival_prob_decreasing(self, km_df):
        cohort = km_df["cohort"].unique()[0]
        kmf    = fit_km(km_df, cohort)
        p30  = float(kmf.survival_function_at_times([30]).iloc[0])
        p180 = float(kmf.survival_function_at_times([180]).iloc[0])
        p365 = float(kmf.survival_function_at_times([365]).iloc[0])
        assert p30 >= p180 >= p365

    def test_median_lifetime_positive(self, km_df):
        cohort = km_df["cohort"].unique()[0]
        kmf    = fit_km(km_df, cohort)
        m      = median_lifetime(kmf)
        assert m > 0 or m == -1.0   # -1 = not reached (acceptable)

    def test_emit_survival_valid_prometheus(self, km_df):
        out = emit_survival(km_df, time.time())
        assert "# HELP dimm_survival_probability" in out
        assert "# TYPE dimm_survival_probability gauge" in out
        assert "dimm_median_lifetime_days" in out


# ── AI runbook tests ──────────────────────────────────────────────────────────

class TestAIRunbook:
    def test_fingerprint_deterministic(self):
        f1 = fingerprint("EDACStorm", "node1", "0")
        f2 = fingerprint("EDACStorm", "node1", "0")
        assert f1 == f2

    def test_fingerprint_unique(self):
        f1 = fingerprint("EDACStorm", "node1", "0")
        f2 = fingerprint("EDACStorm", "node2", "0")
        assert f1 != f2

    def test_fingerprint_length(self):
        fp = fingerprint("Alert", "host", "0")
        assert len(fp) == 12

    def test_build_context_contains_alert_name(self):
        ctx = build_context("EDACCorrectableStorm", "node1", "0", "0", {})
        assert "EDACCorrectableStorm" in ctx

    def test_build_context_contains_instance(self):
        # Instance is anonymized before sending to external API (FIX 2 — data residency).
        # build_context replaces the real hostname with SHA256[:8] pseudonym.
        import hashlib
        anon = "node-" + hashlib.sha256("worker-42".encode()).hexdigest()[:8]
        ctx = build_context("Alert", "worker-42", "0", "0", {})
        assert anon in ctx

    def test_build_context_contains_mc(self):
        ctx = build_context("Alert", "node", "3", "1", {})
        assert "mc3" in ctx

    def test_build_context_contains_extra_labels(self):
        ctx = build_context("Alert", "node", "0", "0", {"datacenter": "us-east-1"})
        assert "us-east-1" in ctx
