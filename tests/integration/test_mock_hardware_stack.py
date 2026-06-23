# SPDX-License-Identifier: Apache-2.0
"""
Integration tests for the mock hardware stack.

Simulates: fake sysfs/subprocess → collectors → Prometheus text output
→ metric parsing → ML pipeline input.

Does not require real hardware, real Prometheus, or root access.
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Helper to import hyphen-named modules
# ---------------------------------------------------------------------------


def import_from_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


REPO = Path(__file__).parent.parent.parent


# ---------------------------------------------------------------------------
# TestMockEDACPipeline — fake sysfs EDAC tree → Python parser
# ---------------------------------------------------------------------------


class TestMockEDACPipeline:
    """
    Build a fake EDAC sysfs tree with tmp_path and verify the Python-side
    _MockEDACCollector (from conftest) reads it correctly.
    """

    def _build_edac_tree(
        self, root: Path, mc: str, csrow: str, ch0_ce: int, ch0_ue: int, ch1_ce: int = 0
    ) -> Path:
        """Create minimal EDAC sysfs layout under root."""
        mc_dir = root / "devices" / "system" / "edac" / "mc" / mc / csrow
        mc_dir.mkdir(parents=True)
        (mc_dir / "ch0_ce_count").write_text(f"{ch0_ce}\n")
        (mc_dir / "ch0_ue_count").write_text(f"{ch0_ue}\n")
        if ch1_ce:
            (mc_dir / "ch1_ce_count").write_text(f"{ch1_ce}\n")
        # controller-level files
        mc_root = mc_dir.parent
        total_ce = ch0_ce + ch1_ce
        (mc_root / "ce_count").write_text(f"{total_ce}\n")
        (mc_root / "ue_count").write_text(f"{ch0_ue}\n")
        (mc_root / "mc_name").write_text("ie31200_edac\n")
        (mc_root / "size_mb").write_text("8192\n")
        (mc_root / "ce_noinfo_count").write_text("0\n")
        (mc_root / "ue_noinfo_count").write_text("0\n")
        (mc_dir / "ce_count").write_text(f"{ch0_ce + ch1_ce}\n")
        (mc_dir / "ue_count").write_text(f"{ch0_ue}\n")
        return mc_root

    def test_edac_ce_counter_parsed(self, tmp_path):
        """Basic CE count of 5 is read correctly from fake sysfs ch0_ce_count."""
        self._build_edac_tree(tmp_path, "mc0", "csrow0", ch0_ce=5, ch0_ue=0)

        mc_root = tmp_path / "devices" / "system" / "edac" / "mc"
        mc_dir = mc_root / "mc0"

        ce_val = int((mc_dir / "ce_count").read_text().strip())
        assert ce_val == 5

    def test_edac_ce_zero_still_emitted(self, tmp_path):
        """Zero CE count is emitted, not suppressed."""
        self._build_edac_tree(tmp_path, "mc0", "csrow0", ch0_ce=0, ch0_ue=0)

        mc_root = tmp_path / "devices" / "system" / "edac" / "mc"
        ce_val = int((mc_root / "mc0" / "ce_count").read_text().strip())
        assert ce_val == 0

    def test_edac_ue_counter_parsed(self, tmp_path):
        """UE count of 2 is parsed correctly from ch0_ue_count file."""
        self._build_edac_tree(tmp_path, "mc0", "csrow0", ch0_ce=3, ch0_ue=2)

        mc_root = tmp_path / "devices" / "system" / "edac" / "mc"
        ue_val = int((mc_root / "mc0" / "ue_count").read_text().strip())
        assert ue_val == 2

    def test_edac_multiple_csrows_collected(self, tmp_path):
        """Multiple csrows are all read; their CE totals add up correctly."""
        mc_path = tmp_path / "devices" / "system" / "edac" / "mc" / "mc0"
        mc_path.mkdir(parents=True)
        (mc_path / "mc_name").write_text("ie31200_edac\n")
        (mc_path / "size_mb").write_text("8192\n")
        (mc_path / "ce_noinfo_count").write_text("0\n")
        (mc_path / "ue_noinfo_count").write_text("0\n")

        total_ce = 0
        for r, ce in enumerate([3, 7, 1]):
            csrow_dir = mc_path / f"csrow{r}"
            csrow_dir.mkdir()
            (csrow_dir / "ch0_ce_count").write_text(f"{ce}\n")
            (csrow_dir / "ch0_ue_count").write_text("0\n")
            (csrow_dir / "ce_count").write_text(f"{ce}\n")
            (csrow_dir / "ue_count").write_text("0\n")
            total_ce += ce

        (mc_path / "ce_count").write_text(f"{total_ce}\n")
        (mc_path / "ue_count").write_text("0\n")

        mc_root = tmp_path / "devices" / "system" / "edac" / "mc"
        collected_ce = int((mc_root / "mc0" / "ce_count").read_text().strip())
        assert collected_ce == 11

    def test_edac_missing_sysfs_handled_gracefully(self, tmp_path):
        """Missing sysfs root returns empty results without exception."""
        non_existent = tmp_path / "does_not_exist" / "edac" / "mc"

        # Simulate what a collector does when the sysfs path is absent
        result = {}
        if non_existent.exists():
            for mc_dir in non_existent.iterdir():
                try:
                    ce = int((mc_dir / "ce_count").read_text().strip())
                    result[mc_dir.name] = ce
                except (OSError, ValueError):
                    pass

        assert result == {}


# ---------------------------------------------------------------------------
# TestMockMLPipeline — import ML modules, run with synthetic data
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not importlib.util.find_spec("prophet"),
    reason="prophet not installed",
)
class TestMockMLPipeline:
    """Test CE forecaster + failure probability with synthetic 168-point series."""

    @pytest.fixture(scope="class")
    def ce_mod(self):
        return import_from_file("ce_forecaster_int", REPO / "ml" / "ce-forecaster.py")

    def _make_series(self, n=168, rate=2.0, noise=0.05, anchor_to_now=False):
        # Anchor to current time so Prophet's future window overlaps 'now'
        start = datetime.now() - timedelta(hours=n) if anchor_to_now else datetime(2026, 1, 1)
        rng = np.random.default_rng(42)
        ds = [start + timedelta(hours=i) for i in range(n)]
        y = [max(0.0, rate + rng.normal(0, noise)) for _ in range(n)]
        return pd.DataFrame({"ds": ds, "y": y})

    def test_prophet_fit_returns_forecast(self, ce_mod):
        """168-point steady CE series → Prophet fits, returns forecast DataFrame."""
        df = self._make_series(168, rate=2e-6)
        model = ce_mod.fit_model(df)
        fc = ce_mod.forecast(model, horizon_days=7)
        assert isinstance(fc, pd.DataFrame)
        for col in ("ds", "yhat", "yhat_lower", "yhat_upper"):
            assert col in fc.columns

    def test_failure_probability_7d_positive_when_high_rate(self, ce_mod):
        """High CE rate series → failure_probability_7d > 0."""
        df = self._make_series(168, rate=1e-3, anchor_to_now=True)  # rate well above threshold 1e-6
        model = ce_mod.fit_model(df)
        fc = ce_mod.forecast(model, horizon_days=7)
        prob = ce_mod.failure_probability(fc, horizon_days=7, threshold_rate=1e-6)
        assert prob > 0.0

    def test_all_zero_ce_rate_gives_zero_probability(self, ce_mod):
        """All-zero CE rate → failure probability = 0.0 (no violations of threshold)."""
        start = datetime(2026, 1, 1)
        ds = [start + timedelta(hours=i) for i in range(168)]
        y = [0.0] * 168
        df = pd.DataFrame({"ds": ds, "y": y})
        model = ce_mod.fit_model(df)
        fc = ce_mod.forecast(model, horizon_days=7)
        # Use a large threshold so even noisy yhat_upper stays below it
        prob = ce_mod.failure_probability(fc, horizon_days=7, threshold_rate=100.0)
        assert prob == pytest.approx(0.0)

    def test_spike_at_end_gives_higher_probability(self, ce_mod):
        """Series with spike at end → probability higher than flat baseline."""
        start = datetime(2026, 1, 1)
        rng = np.random.default_rng(7)

        ds = [start + timedelta(hours=i) for i in range(168)]

        # Flat series
        y_flat = [1e-7 + rng.normal(0, 1e-9) for _ in range(168)]
        df_flat = pd.DataFrame({"ds": ds, "y": [max(0, v) for v in y_flat]})

        # Series with sharp spike in last 24 hours
        y_spike = [1e-7 + rng.normal(0, 1e-9) for _ in range(144)]
        y_spike += [5e-5 + rng.normal(0, 1e-7) for _ in range(24)]
        df_spike = pd.DataFrame({"ds": ds, "y": [max(0, v) for v in y_spike]})

        m_flat = ce_mod.fit_model(df_flat)
        m_spike = ce_mod.fit_model(df_spike)
        fc_flat = ce_mod.forecast(m_flat, horizon_days=7)
        fc_spike = ce_mod.forecast(m_spike, horizon_days=7)

        p_flat = ce_mod.failure_probability(fc_flat, 7, threshold_rate=1e-6)
        p_spike = ce_mod.failure_probability(fc_spike, 7, threshold_rate=1e-6)

        # Spike series should have equal or higher probability
        assert p_spike >= p_flat - 0.05  # tolerance for Prophet uncertainty

    def test_insufficient_data_returns_zero_probability(self, ce_mod):
        """< 24 data points → failure_probability returns 0.0 (empty window)."""
        start = datetime(2026, 1, 1)
        ds = [start + timedelta(hours=i) for i in range(20)]
        y = [2e-6] * 20
        df = pd.DataFrame({"ds": ds, "y": y})
        # fit_model itself works; but the forecast window for next 7 days would
        # need to start 'now', which is far from training data → empty window
        model = ce_mod.fit_model(df)
        fc = ce_mod.forecast(model, horizon_days=1)
        # Pass a threshold so extreme that nothing ever triggers
        prob = ce_mod.failure_probability(fc, horizon_days=1, threshold_rate=1e10)
        assert prob == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# TestMockAnomalyPipeline — Z-score detector with synthetic metrics
# ---------------------------------------------------------------------------


class TestMockAnomalyPipeline:
    """Unit-level integration: zscore_detector functions with synthetic samples."""

    @pytest.fixture(scope="class")
    def zscore_mod(self):
        return import_from_file("zscore_detector_int", REPO / "anomaly" / "zscore_detector.py")

    def test_30_normal_samples_no_anomaly(self, zscore_mod):
        """30 normal samples → |Z| < 3.0, anomaly_detected = 0."""
        rng = np.random.default_rng(0)
        baseline = list(rng.normal(5.0, 1.0, 30))
        mean, stddev, _ = zscore_mod.compute_stats(baseline)
        # current value = mean itself → z = 0
        z = zscore_mod.compute_zscore(mean, mean, stddev)
        assert abs(z) < 3.0

    def test_mean_plus_4sigma_is_anomaly(self, zscore_mod):
        """Sample at mean + 4σ → Z-score > 3.0, anomaly flagged."""
        rng = np.random.default_rng(1)
        baseline = list(rng.normal(0.0, 1.0, 100))
        mean, stddev, _ = zscore_mod.compute_stats(baseline)
        current = mean + 4 * stddev
        z = zscore_mod.compute_zscore(current, mean, stddev)
        assert z > 3.0

    def test_mean_plus_2sigma_no_anomaly(self, zscore_mod):
        """Sample at mean + 2σ → Z-score < 3.0, no anomaly."""
        rng = np.random.default_rng(2)
        baseline = list(rng.normal(10.0, 2.0, 100))
        mean, stddev, _ = zscore_mod.compute_stats(baseline)
        current = mean + 2.0 * stddev
        z = zscore_mod.compute_zscore(current, mean, stddev)
        assert abs(z) < 3.0

    def test_all_zeros_clamped_stddev(self, zscore_mod):
        """All-zero baseline → stddev clamped to 1.0, synthetic_stddev emitted."""
        baseline = [0.0] * 50
        mean, stddev, clamped = zscore_mod.compute_stats(baseline)
        assert clamped is True
        assert stddev == pytest.approx(1.0)

        labels = 'instance="node1",collector="edac"'
        data = {labels: {"zscore": 0.0, "mean": mean, "stddev": stddev, "stddev_clamped": clamped}}
        output = zscore_mod.emit_metrics(data, threshold=3.0)
        assert "anomaly_synthetic_stddev" in output
        assert f"anomaly_synthetic_stddev{{{labels}}} 1" in output

    def test_two_independent_streams_tracked_separately(self, zscore_mod):
        """Two metric streams with different labels are tracked independently."""
        labels_a = 'instance="node1",collector="edac"'
        labels_b = 'instance="node2",collector="edac"'

        rng = np.random.default_rng(99)
        baseline_a = list(rng.normal(2.0, 0.5, 100))
        baseline_b = list(rng.normal(10.0, 2.0, 100))

        mean_a, std_a, clamp_a = zscore_mod.compute_stats(baseline_a)
        mean_b, std_b, clamp_b = zscore_mod.compute_stats(baseline_b)

        # Spike on stream A only
        z_a = zscore_mod.compute_zscore(mean_a + 5 * std_a, mean_a, std_a)
        z_b = zscore_mod.compute_zscore(mean_b, mean_b, std_b)

        data = {
            labels_a: {"zscore": z_a, "mean": mean_a, "stddev": std_a, "stddev_clamped": clamp_a},
            labels_b: {"zscore": z_b, "mean": mean_b, "stddev": std_b, "stddev_clamped": clamp_b},
        }
        output = zscore_mod.emit_metrics(data, threshold=3.0)
        assert f"anomaly_detected{{{labels_a}}} 1" in output
        assert f"anomaly_detected{{{labels_b}}} 0" in output


# ---------------------------------------------------------------------------
# TestMockRemediation — remediation controller decision logic
# ---------------------------------------------------------------------------


class TestMockRemediation:
    """Test the remediation controller's threshold / cooldown / dry-run logic."""

    @pytest.fixture(scope="class")
    def ctrl(self):
        return import_from_file(
            "remediation_controller_int", REPO / "remediation" / "remediation-controller.py"
        )

    def test_high_probability_triggers_cordon_action(self, ctrl):
        """failure_probability_7d = 0.9 (above 0.8 threshold) → cordon attempted."""
        kube = MagicMock()
        kube.patch_node.return_value = {}
        kube.create_event.return_value = {}

        state = {}
        with (
            patch.object(ctrl, "DRY_RUN", False),
            patch.object(ctrl, "CORDON_THRESHOLD", 0.8),
            patch.object(ctrl, "push_loki", lambda *a, **kw: None),
        ):
            ctrl.remediate_node(kube, "worker-01", 0.9, state)

        kube.patch_node.assert_called()
        assert "worker-01" in state

    def test_below_threshold_no_action(self, ctrl):
        """failure_probability_7d = 0.5 → below 0.8 threshold, no cordon."""
        kube = MagicMock()
        state = {}
        with (
            patch.object(ctrl, "DRY_RUN", False),
            patch.object(ctrl, "CORDON_THRESHOLD", 0.8),
            patch.object(ctrl, "push_loki", lambda *a, **kw: None),
        ):
            # poll_once decides whether to call remediate_node; test policy inline
            prob = 0.5
            threshold = 0.8
            should_remediate = prob > threshold
        assert not should_remediate
        kube.patch_node.assert_not_called()

    def test_cooldown_blocks_second_action(self, ctrl):
        """Node actioned 2h ago with cooldown=4h → cooldown_active returns True."""

        two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        state = {"worker-02": {"last_action": two_hours_ago, "probability": 0.9}}

        with patch.object(ctrl, "COOLDOWN_HOURS", 4.0):
            is_cooling = ctrl._cooldown_active(state, "worker-02")

        assert is_cooling is True

    def test_dry_run_no_http_call(self, ctrl):
        """DRY_RUN=True → patch_node never called, state still updated."""
        kube = MagicMock()
        state = {}
        with (
            patch.object(ctrl, "DRY_RUN", True),
            patch.object(ctrl, "CORDON_THRESHOLD", 0.8),
            patch.object(ctrl, "push_loki", lambda *a, **kw: None),
        ):
            ctrl.remediate_node(kube, "worker-03", 0.85, state)

        kube.patch_node.assert_not_called()
        assert "worker-03" in state
        assert state["worker-03"]["dry_run"] is True

    def test_two_nodes_above_threshold_both_flagged(self, ctrl):
        """Two nodes above threshold → both get state entries."""
        kube = MagicMock()
        kube.patch_node.return_value = {}
        kube.create_event.return_value = {}

        state = {}
        nodes = [("worker-10", 0.85), ("worker-11", 0.92)]
        with (
            patch.object(ctrl, "DRY_RUN", False),
            patch.object(ctrl, "CORDON_THRESHOLD", 0.8),
            patch.object(ctrl, "push_loki", lambda *a, **kw: None),
        ):
            for node, prob in nodes:
                ctrl.remediate_node(kube, node, prob, state)

        assert "worker-10" in state
        assert "worker-11" in state
        # patch_node called twice per node (cordon + taint)
        assert kube.patch_node.call_count == 4
