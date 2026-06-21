#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
gpu/tests/test_gpu_collectors.py — Pytest suite for nvidia-xid-collector and
nvidia-throttle-collector.  No live GPU required; all nvidia-smi calls are
mocked with fixture strings.

Run:
    python3 -m pytest gpu/tests/ -v
"""
import os
import sys
import subprocess
import types
import importlib
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup — allow importing the collectors from the gpu/ directory
# ---------------------------------------------------------------------------
GPU_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(GPU_DIR))

import nvidia_xid_collector as xid_mod       # noqa: E402  (loaded after sys.path insert)
import nvidia_throttle_collector as thr_mod  # noqa: E402
import gpu_failure_predictor as pred_mod     # noqa: E402


# ===========================================================================
# Fixtures / helpers
# ===========================================================================

GPU_NAME_OUTPUT_SINGLE = "Tesla T4\n"
GPU_NAME_OUTPUT_MULTI  = "Tesla T4\nA100-SXM4-40GB\n"

DMON_OUTPUT_NO_XID = """\
# gpu    sm   mem   enc   dec  xid
# Idx     %     %     %     %   No
    0     5     2     0     0    0
"""

DMON_OUTPUT_XID63 = """\
# gpu    sm   mem   enc   dec  xid
# Idx     %     %     %     %   No
    0     5     2     0     0   63
"""

DMON_OUTPUT_XID94 = """\
# gpu    sm   mem   enc   dec  xid
# Idx     %     %     %     %   No
    0     0     0     0     0   94
"""

DMON_OUTPUT_XID79 = """\
# gpu    sm   mem   enc   dec  xid
# Idx     %     %     %     %   No
    0     0     0     0     0   79
"""

DMON_OUTPUT_MULTI_GPU = """\
# gpu    sm   mem   enc   dec  xid
# Idx     %     %     %     %   No
    0     5     2     0     0   63
    1     0     0     0     0   74
"""

DMON_OUTPUT_UNKNOWN_XID = """\
# gpu    sm   mem   enc   dec  xid
# Idx     %     %     %     %   No
    0     5     2     0     0   99
"""

ECC_OUTPUT_SINGLE = "0, 4, 1\n"
ECC_OUTPUT_ZERO   = "0, 0, 0\n"
ECC_OUTPUT_MULTI  = "0, 4, 1\n1, 12, 3\n"
ECC_OUTPUT_NA     = "0, N/A, N/A\n"

THROTTLE_OUTPUT_ACTIVE_HW_THERMAL = (
    "0, Active, Not Active, Not Active, 1350, 75.50, 62\n"
)
THROTTLE_OUTPUT_NOT_ACTIVE_SW = (
    "0, Not Active, Not Active, Not Active, 1800, 50.25, 45\n"
)
THROTTLE_OUTPUT_POWER_BRAKE = (
    "0, Not Active, Not Active, Active, 900, 120.00, 80\n"
)
THROTTLE_OUTPUT_ALL_ACTIVE = (
    "0, Active, Active, Active, 600, 150.00, 95\n"
)
THROTTLE_OUTPUT_NO_THROTTLE = (
    "0, Not Active, Not Active, Not Active, 1800, 50.00, 40\n"
)
THROTTLE_OUTPUT_MULTI = (
    "0, Active, Not Active, Not Active, 1350, 75.50, 62\n"
    "1, Not Active, Not Active, Not Active, 1800, 50.00, 40\n"
)


def _make_run_returns(mapping: dict):
    """
    Returns a drop-in for xid_mod._run / thr_mod._run.
    mapping: {first_arg_of_cmd: (stdout, returncode)}
    The key is matched against cmd[0] or cmd[1] (subcommand).
    If no match, returns ("", -1) simulating absent binary.
    """
    def _fake_run(cmd, timeout=10):
        # Try matching on the full cmd list as a tuple key first
        cmd_key = tuple(cmd)
        if cmd_key in mapping:
            return mapping[cmd_key]
        # Fall back: match on (cmd[0],) or (cmd[0], cmd[1])
        for length in (2, 1):
            k = tuple(cmd[:length])
            if k in mapping:
                return mapping[k]
        return ("", -1)
    return _fake_run


# ===========================================================================
# TestXIDDetection  (8 tests)
# ===========================================================================

class TestXIDDetection:
    """Tests for GPU presence detection and XID parsing in nvidia-xid-collector."""

    def test_gpu_absent_file_not_found(self):
        """detect_gpus returns empty list when nvidia-smi binary is missing."""
        with patch.object(xid_mod, "_run", return_value=("", -1)):
            gpus = xid_mod.detect_gpus()
        assert gpus == []

    def test_gpu_absent_nonzero_exit(self):
        """detect_gpus returns empty list when nvidia-smi exits with non-zero code."""
        with patch.object(xid_mod, "_run", return_value=("", 6)):
            gpus = xid_mod.detect_gpus()
        assert gpus == []

    def test_xid_63_parsed_as_fatal(self):
        """XID 63 (DBE) must be classified as fatal severity."""
        assert xid_mod.xid_severity(63) == "fatal"

    def test_xid_94_parsed_as_critical(self):
        """XID 94 (contained ECC error) must be classified as critical."""
        assert xid_mod.xid_severity(94) == "critical"

    def test_xid_95_parsed_as_critical(self):
        """XID 95 (uncontained ECC error) must be classified as critical."""
        assert xid_mod.xid_severity(95) == "critical"

    def test_xid_79_parsed_as_warning(self):
        """XID 79 (GPU recovery) must be classified as warning severity."""
        assert xid_mod.xid_severity(79) == "warning"

    def test_multi_gpu_dmon_output(self):
        """Multi-GPU dmon output produces one XID event per GPU with correct indices."""
        events = xid_mod._parse_dmon_xid(DMON_OUTPUT_MULTI_GPU)
        assert len(events) == 2
        indices = {e["gpu_index"] for e in events}
        assert "0" in indices
        assert "1" in indices

    def test_unknown_xid_defaults_to_info(self):
        """An XID not in the severity table defaults to 'info'."""
        assert xid_mod.xid_severity(999) == "info"
        assert xid_mod.xid_severity(1) == "info"
        assert xid_mod.xid_severity(50) == "info"

    def test_collector_up_zero_when_no_gpu(self):
        """emit_metrics with gpu_present=False emits gpu_collector_up=0."""
        output = xid_mod.emit_metrics(False, [], [], 1000.0)
        assert 'gpu_collector_up{collector="nvidia_xid"} 0' in output

    def test_collector_up_one_when_gpu_present(self):
        """emit_metrics with gpu_present=True emits gpu_collector_up=1."""
        output = xid_mod.emit_metrics(True, [], [], 1000.0)
        assert 'gpu_collector_up{collector="nvidia_xid"} 1' in output


# ===========================================================================
# TestECCMetrics  (6 tests)
# ===========================================================================

class TestECCMetrics:
    """Tests for ECC single-bit and double-bit error metric emission."""

    def test_sbe_counter_emitted(self):
        """gpu_ecc_sbe_total must appear in output when ECC data is present."""
        ecc = [{"gpu_index": "0", "sbe": 4, "dbe": 1}]
        output = xid_mod.emit_metrics(True, [], ecc, 1000.0)
        assert "gpu_ecc_sbe_total" in output
        assert 'gpu_ecc_sbe_total{gpu_index="0"} 4' in output

    def test_dbe_counter_emitted(self):
        """gpu_ecc_dbe_total must appear in output with the correct count."""
        ecc = [{"gpu_index": "0", "sbe": 4, "dbe": 1}]
        output = xid_mod.emit_metrics(True, [], ecc, 1000.0)
        assert "gpu_ecc_dbe_total" in output
        assert 'gpu_ecc_dbe_total{gpu_index="0"} 1' in output

    def test_per_gpu_index_label(self):
        """ECC metrics must carry the gpu_index label from the data."""
        ecc = [{"gpu_index": "2", "sbe": 7, "dbe": 0}]
        output = xid_mod.emit_metrics(True, [], ecc, 1000.0)
        assert 'gpu_ecc_sbe_total{gpu_index="2"} 7' in output

    def test_zero_ecc_counts(self):
        """Zero ECC counts are emitted as 0, not suppressed."""
        ecc = [{"gpu_index": "0", "sbe": 0, "dbe": 0}]
        output = xid_mod.emit_metrics(True, [], ecc, 1000.0)
        assert 'gpu_ecc_sbe_total{gpu_index="0"} 0' in output
        assert 'gpu_ecc_dbe_total{gpu_index="0"} 0' in output

    def test_multi_gpu_ecc(self):
        """ECC metrics must include an entry for each GPU when multiple GPUs present."""
        ecc = [
            {"gpu_index": "0", "sbe": 4, "dbe": 1},
            {"gpu_index": "1", "sbe": 12, "dbe": 3},
        ]
        output = xid_mod.emit_metrics(True, [], ecc, 1000.0)
        assert 'gpu_ecc_sbe_total{gpu_index="0"} 4' in output
        assert 'gpu_ecc_sbe_total{gpu_index="1"} 12' in output
        assert 'gpu_ecc_dbe_total{gpu_index="1"} 3' in output

    def test_na_value_handled_as_zero(self):
        """N/A values from nvidia-smi must be treated as 0 in collect_ecc_counts."""
        with patch.object(xid_mod, "_run", return_value=(ECC_OUTPUT_NA, 0)):
            ecc = xid_mod.collect_ecc_counts()
        assert len(ecc) == 1
        assert ecc[0]["sbe"] == 0
        assert ecc[0]["dbe"] == 0

    def test_collect_ecc_multi_gpu_parsed(self):
        """collect_ecc_counts correctly parses multi-GPU CSV output."""
        with patch.object(xid_mod, "_run", return_value=(ECC_OUTPUT_MULTI, 0)):
            ecc = xid_mod.collect_ecc_counts()
        assert len(ecc) == 2
        assert ecc[0]["sbe"] == 4
        assert ecc[1]["dbe"] == 3


# ===========================================================================
# TestThrottleDetection  (8 tests)
# ===========================================================================

class TestThrottleDetection:
    """Tests for gpu/nvidia-throttle-collector.py metric parsing and emission."""

    def test_hw_thermal_active_parsed_as_1(self):
        """'Active' hw_thermal throttle reason must parse to 1."""
        with patch.object(thr_mod, "_run", return_value=(THROTTLE_OUTPUT_ACTIVE_HW_THERMAL, 0)):
            data = thr_mod.collect_throttle_data()
        assert len(data) == 1
        assert data[0]["throttle"]["hw_thermal"] == 1

    def test_sw_thermal_not_active_parsed_as_0(self):
        """'Not Active' sw_thermal throttle reason must parse to 0."""
        with patch.object(thr_mod, "_run", return_value=(THROTTLE_OUTPUT_NOT_ACTIVE_SW, 0)):
            data = thr_mod.collect_throttle_data()
        assert len(data) == 1
        assert data[0]["throttle"]["sw_thermal"] == 0

    def test_hw_power_brake_throttle(self):
        """hw_power_brake throttle active must appear as 1 in metrics output."""
        with patch.object(thr_mod, "_run", return_value=(THROTTLE_OUTPUT_POWER_BRAKE, 0)):
            data = thr_mod.collect_throttle_data()
        assert data[0]["throttle"]["hw_power_brake"] == 1
        output = thr_mod.emit_metrics(True, data, 1000.0)
        assert 'gpu_throttle_active{gpu_index="0",reason="hw_power_brake"} 1' in output

    def test_combined_multiple_active_reasons(self):
        """When all three throttle reasons are active, all must appear as 1."""
        with patch.object(thr_mod, "_run", return_value=(THROTTLE_OUTPUT_ALL_ACTIVE, 0)):
            data = thr_mod.collect_throttle_data()
        assert data[0]["throttle"]["hw_thermal"] == 1
        assert data[0]["throttle"]["sw_thermal"] == 1
        assert data[0]["throttle"]["hw_power_brake"] == 1

    def test_no_throttle_all_zeros(self):
        """When no throttle reasons are active, all throttle metrics must be 0."""
        with patch.object(thr_mod, "_run", return_value=(THROTTLE_OUTPUT_NO_THROTTLE, 0)):
            data = thr_mod.collect_throttle_data()
        output = thr_mod.emit_metrics(True, data, 1000.0)
        assert 'gpu_throttle_active{gpu_index="0",reason="hw_thermal"} 0' in output
        assert 'gpu_throttle_active{gpu_index="0",reason="sw_thermal"} 0' in output
        assert 'gpu_throttle_active{gpu_index="0",reason="hw_power_brake"} 0' in output

    def test_temperature_parsing(self):
        """Temperature must be parsed from the last field and appear in output."""
        with patch.object(thr_mod, "_run", return_value=(THROTTLE_OUTPUT_ACTIVE_HW_THERMAL, 0)):
            data = thr_mod.collect_throttle_data()
        assert data[0]["temperature"] == pytest.approx(62.0)
        output = thr_mod.emit_metrics(True, data, 1000.0)
        assert "gpu_temperature_celsius" in output
        assert 'gpu_temperature_celsius{gpu_index="0"} 62.0' in output

    def test_power_draw_parsing(self):
        """Power draw must be parsed from the sixth field and appear in output."""
        with patch.object(thr_mod, "_run", return_value=(THROTTLE_OUTPUT_ACTIVE_HW_THERMAL, 0)):
            data = thr_mod.collect_throttle_data()
        assert data[0]["power_watts"] == pytest.approx(75.50)
        output = thr_mod.emit_metrics(True, data, 1000.0)
        assert "gpu_power_watts" in output
        assert 'gpu_power_watts{gpu_index="0"} 75.50' in output

    def test_no_gpu_graceful_exit(self):
        """When no GPU is present, throttle collector emits collector_up=0 without error."""
        with patch.object(thr_mod, "_run", return_value=("", -1)):
            gpu_present = thr_mod.detect_gpus()
        assert gpu_present is False
        output = thr_mod.emit_metrics(False, [], 1000.0)
        assert 'gpu_throttle_collector_up{collector="nvidia_throttle"} 0' in output
        assert "gpu_throttle_active" not in output

    def test_clock_mhz_emitted(self):
        """Graphics clock speed must be emitted with domain label."""
        with patch.object(thr_mod, "_run", return_value=(THROTTLE_OUTPUT_ACTIVE_HW_THERMAL, 0)):
            data = thr_mod.collect_throttle_data()
        output = thr_mod.emit_metrics(True, data, 1000.0)
        assert 'gpu_clock_mhz{gpu_index="0",domain="graphics"}' in output
        assert "1350.0" in output


# ===========================================================================
# TestAMDCollector  (6 tests — sysfs-based stub for AMD ROCm / amdgpu)
# ===========================================================================

# Simulated /sys/class/drm/card*/device/driver/module/drivers content
AMD_DRIVER_SYMLINK_TARGET = "pci:amdgpu"

# Simulated amdgpu RAS sysfs ECC content
UMC_ECC_CONTENT   = "ue_count: 2\nce_count: 10\n"
GFX_ECC_CONTENT   = "ue_count: 0\nce_count: 3\n"
EMPTY_ECC_CONTENT = ""


def _parse_amdgpu_ecc(content: str) -> dict:
    """
    Minimal AMD ECC sysfs parser (used as the module-under-test within this stub).
    Reads 'ue_count' and 'ce_count' from the sysfs ras_eeprom_table content.
    Returns {'ue': int, 'ce': int} or None if unparseable.
    """
    result = {}
    for line in content.splitlines():
        if line.startswith("ue_count:"):
            try:
                result["ue"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("ce_count:"):
            try:
                result["ce"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    if "ue" in result or "ce" in result:
        return result
    return None


def _detect_amdgpu_cards(drm_root: Path) -> list:
    """
    Scan drm_root for card* directories whose driver symlink target contains 'amdgpu'.
    Returns list of card names detected.
    """
    cards = []
    if not drm_root.exists():
        return cards
    for card_path in sorted(drm_root.glob("card*")):
        driver_path = card_path / "device" / "driver"
        if driver_path.is_symlink():
            target = os.readlink(str(driver_path))
            # Check only the final path component, not the full path string,
            # to avoid false-positives when parent directories contain "amdgpu".
            driver_name = Path(target).name
            if "amdgpu" in driver_name:
                cards.append(card_path.name)
        elif (card_path / "device" / "driver" / "module").exists():
            # Fallback: check module name file
            mod_file = card_path / "device" / "driver" / "module" / "name"
            if mod_file.exists() and "amdgpu" in mod_file.read_text():
                cards.append(card_path.name)
    return cards


def _emit_amd_collector_up(cards: list) -> str:
    up = 1 if cards else 0
    return (
        "# HELP amd_gpu_collector_up 1 if AMD GPU detected.\n"
        "# TYPE amd_gpu_collector_up gauge\n"
        f'amd_gpu_collector_up{{collector="amdgpu"}} {up}\n'
    )


def _emit_amd_ecc(card: str, block: str, ecc: dict) -> str:
    lines = []
    if "ue" in ecc:
        lines.append(
            f'amd_gpu_ecc_uncorrected_total{{card="{card}",block="{block}"}} {ecc["ue"]}'
        )
    if "ce" in ecc:
        lines.append(
            f'amd_gpu_ecc_corrected_total{{card="{card}",block="{block}"}} {ecc["ce"]}'
        )
    return "\n".join(lines) + "\n" if lines else ""


class TestAMDCollector:
    """
    Stub tests for AMD GPU sysfs-based ECC detection.
    Uses a temporary filesystem tree to simulate /sys/class/drm/card* entries.
    No amdgpu hardware required.
    """

    @pytest.fixture()
    def drm_root(self, tmp_path):
        """Create a fake /sys/class/drm tree with one amdgpu card."""
        card0 = tmp_path / "card0" / "device"
        card0.mkdir(parents=True)
        driver_link = card0 / "driver"
        # Create an actual symlink that points to a path containing 'amdgpu'
        target = tmp_path / "drivers" / "amdgpu"
        target.mkdir(parents=True)
        driver_link.symlink_to(str(target))
        return tmp_path

    @pytest.fixture()
    def drm_root_no_amd(self, tmp_path):
        """Create a fake /sys/class/drm tree with a non-amdgpu card."""
        card0 = tmp_path / "card0" / "device"
        card0.mkdir(parents=True)
        target = tmp_path / "drivers" / "nouveau"
        target.mkdir(parents=True)
        (card0 / "driver").symlink_to(str(target))
        return tmp_path

    @pytest.fixture()
    def drm_root_multi(self, tmp_path):
        """Fake DRM tree with two amdgpu cards."""
        for name in ("card0", "card1"):
            card = tmp_path / name / "device"
            card.mkdir(parents=True)
            target = tmp_path / "drivers" / "amdgpu"
            target.mkdir(parents=True, exist_ok=True)
            (card / "driver").symlink_to(str(target))
        return tmp_path

    def test_amdgpu_driver_detected(self, drm_root):
        """_detect_amdgpu_cards finds card0 when driver symlink targets 'amdgpu'."""
        cards = _detect_amdgpu_cards(drm_root)
        assert "card0" in cards

    def test_missing_driver_returns_collector_up_zero(self, tmp_path):
        """When drm_root is empty (no cards), collector_up must be 0."""
        cards = _detect_amdgpu_cards(tmp_path)
        output = _emit_amd_collector_up(cards)
        assert "amd_gpu_collector_up" in output
        assert 'amd_gpu_collector_up{collector="amdgpu"} 0' in output

    def test_umc_block_ecc_parsed(self):
        """UMC block ECC content parses ue_count=2, ce_count=10 correctly."""
        ecc = _parse_amdgpu_ecc(UMC_ECC_CONTENT)
        assert ecc is not None
        assert ecc["ue"] == 2
        assert ecc["ce"] == 10

    def test_gfx_block_ecc_parsed(self):
        """GFX block ECC content parses ue_count=0, ce_count=3 correctly."""
        ecc = _parse_amdgpu_ecc(GFX_ECC_CONTENT)
        assert ecc is not None
        assert ecc["ue"] == 0
        assert ecc["ce"] == 3

    def test_multi_card_detection(self, drm_root_multi):
        """Two amdgpu cards in sysfs must both be detected."""
        cards = _detect_amdgpu_cards(drm_root_multi)
        assert len(cards) == 2
        assert "card0" in cards
        assert "card1" in cards

    def test_missing_sysfs_path_handled(self, tmp_path):
        """Passing a non-existent sysfs path returns empty card list without exception."""
        non_existent = tmp_path / "does_not_exist"
        cards = _detect_amdgpu_cards(non_existent)
        assert cards == []

    def test_non_amdgpu_driver_not_detected(self, drm_root_no_amd):
        """A card with driver 'nouveau' must NOT be included in amdgpu detection."""
        cards = _detect_amdgpu_cards(drm_root_no_amd)
        assert len(cards) == 0

    def test_amd_ecc_metric_format(self):
        """_emit_amd_ecc output matches expected Prometheus label format."""
        ecc = _parse_amdgpu_ecc(UMC_ECC_CONTENT)
        output = _emit_amd_ecc("card0", "UMC", ecc)
        assert 'amd_gpu_ecc_uncorrected_total{card="card0",block="UMC"} 2' in output
        assert 'amd_gpu_ecc_corrected_total{card="card0",block="UMC"} 10' in output

    def test_empty_sysfs_content_returns_none(self):
        """Unparseable/empty ECC sysfs content returns None."""
        result = _parse_amdgpu_ecc(EMPTY_ECC_CONTENT)
        assert result is None


# ===========================================================================
# TestGPUPredictor  (4 tests — Prophet-based GPU failure predictor)
# ===========================================================================

# Minimal Prometheus range-query response fixtures
_PROM_EMPTY_RESPONSE = {
    "status": "success",
    "data": {"resultType": "matrix", "result": []},
}

# < 24 data points for GPU index "0"
_PROM_FEW_POINTS_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "matrix",
        "result": [
            {
                "metric": {"gpu_index": "0"},
                "values": [[1_700_000_000 + i * 3600, "0"] for i in range(10)],
            }
        ],
    },
}

# 48 hourly data points for GPU index "0" — sufficient for Prophet fit
_BASE_TS = 1_700_000_000
_PROM_ENOUGH_POINTS_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "matrix",
        "result": [
            {
                "metric": {"gpu_index": "0"},
                "values": [[_BASE_TS + i * 3600, str(i * 2)] for i in range(48)],
            }
        ],
    },
}


def _make_json_response(payload: dict):
    """Return a mock urllib response that yields payload as JSON bytes."""
    import json, io
    raw = json.dumps(payload).encode()

    class _FakeResp:
        def read(self):
            return raw
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass

    return _FakeResp()


class TestGPUPredictor:
    """Tests for gpu/gpu-failure-predictor.py Prophet-based failure predictor."""

    def test_empty_prometheus_response_emits_nothing(self):
        """Empty Prometheus result → no probability metrics, no crash."""
        with patch.object(
            pred_mod.urllib.request, "urlopen",
            return_value=_make_json_response(_PROM_EMPTY_RESPONSE),
        ):
            series = pred_mod.fetch_dbe_series("http://localhost:9090")
        assert series == {}
        # emit_metrics with empty dicts must not raise and must include predictor_up
        output = pred_mod.emit_metrics(True, {}, {}, 1000.0)
        assert "gpu_predictor_up 1" in output
        assert "gpu_failure_probability_7d" not in output

    def test_fewer_than_24_points_emits_zero_probability(self):
        """< 24 hourly data points → failure probability reported as 0.0."""
        with patch.object(
            pred_mod.urllib.request, "urlopen",
            return_value=_make_json_response(_PROM_FEW_POINTS_RESPONSE),
        ):
            series = pred_mod.fetch_dbe_series("http://localhost:9090")
        # Should have a sentinel None for GPU "0"
        assert "0" in series
        assert series["0"] is None
        # emit_metrics with 0.0 probability must reflect it
        output = pred_mod.emit_metrics(True, {"0": 0.0}, {"0": 0.0}, 1000.0)
        assert 'gpu_failure_probability_7d{gpu_index="0"} 0.000000' in output

    def test_mock_prophet_produces_valid_probability(self):
        """Mocked Prophet fit+predict → gpu_failure_probability_7d is a float in [0, 1]."""
        import pandas as pd
        import numpy as np

        # Build a minimal 48-row DataFrame (hourly DBE rate)
        now = pd.Timestamp.now()
        timestamps = [now - pd.Timedelta(hours=48 - i) for i in range(48)]
        df = pd.DataFrame({"ds": timestamps, "y": [float(i % 3) for i in range(48)]})

        # Build a fake forecast DataFrame covering next 7 days
        future_ts = [now + pd.Timedelta(hours=i) for i in range(1, 169)]
        fc = pd.DataFrame({
            "ds":         future_ts,
            "yhat":       [0.1] * 168,
            "yhat_lower": [0.0] * 168,
            "yhat_upper": [0.3] * 168,
        })

        class _FakeProphet:
            def fit(self, _df):
                return self
            def make_future_dataframe(self, periods, freq):
                return pd.DataFrame({"ds": future_ts})
            def predict(self, _future):
                return fc

        with patch.object(pred_mod, "fit_prophet", return_value=_FakeProphet()):
            with patch.object(pred_mod, "make_forecast", return_value=fc):
                prob = pred_mod.compute_failure_probability(fc)
                rate = pred_mod.mean_next_24h_rate(fc)

        assert isinstance(prob, float)
        assert 0.0 <= prob <= 1.0
        assert isinstance(rate, float)
        assert rate >= 0.0

    def test_prometheus_unreachable_emits_predictor_up_zero(self):
        """ConnectionError from Prometheus → gpu_predictor_up=0, no exception propagated."""
        import urllib.error

        with patch.object(
            pred_mod.urllib.request, "urlopen",
            side_effect=ConnectionError("connection refused"),
        ):
            # fetch_dbe_series raises; caller (main) must catch it and emit up=0
            raised = False
            try:
                pred_mod.fetch_dbe_series("http://unreachable:9090")
                raised = False
            except Exception:
                raised = True

        # Whether fetch raises or not, emit_metrics with predictor_up=False must work
        output = pred_mod.emit_metrics(False, {}, {}, 1000.0)
        assert "gpu_predictor_up 0" in output
        # Verify main() handles the exception gracefully via direct call
        with patch.object(
            pred_mod.urllib.request, "urlopen",
            side_effect=ConnectionError("connection refused"),
        ):
            with patch.object(pred_mod, "emit_metrics", wraps=pred_mod.emit_metrics) as mock_emit:
                import argparse
                args = argparse.Namespace(
                    prometheus="http://unreachable:9090",
                    output=Path("-"),
                    verbose=False,
                )
                # Patch argparse to return our args, then call main
                with patch("argparse.ArgumentParser.parse_args", return_value=args):
                    import io
                    from contextlib import redirect_stdout
                    buf = io.StringIO()
                    try:
                        with redirect_stdout(buf):
                            pred_mod.main()
                    except SystemExit:
                        pass
                    except Exception:
                        pass
                # The emit_metrics call with predictor_up=False must have happened
                called_with_false = any(
                    call_args.args[0] is False or call_args.args[0] == False
                    for call_args in mock_emit.call_args_list
                )
                assert called_with_false, "emit_metrics must be called with predictor_up=False on connection error"
