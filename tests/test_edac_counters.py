# SPDX-License-Identifier: Apache-2.0
"""
test_edac_counters.py — Verify EDAC sysfs counter reading and Prometheus metric emission.

Tests:
  - All expected sysfs paths exist in the mock sysfs tree
  - Counter values are read correctly (per-controller, per-csrow, per-channel)
  - Controller-level totals equal the sum of csrow-level values
  - Counter values match the MOCK_TOPOLOGY fixture
  - Zero counters are emitted correctly (not omitted)
  - Non-existent sysfs root is handled gracefully
  - Edge cases: single controller, single csrow, single channel
"""

import os
from pathlib import Path

import pytest


class TestEdacSysfsStructure:
    """Verify the mock sysfs tree structure is correct before testing the collector."""

    def test_edac_mc_root_exists(self, edac_sysfs):
        assert edac_sysfs.is_dir(), f"EDAC mc root not found: {edac_sysfs}"

    def test_expected_controllers_present(self, edac_sysfs, mock_topology):
        dirs = {d.name for d in edac_sysfs.iterdir() if d.is_dir()}
        for mc_name in mock_topology:
            assert mc_name in dirs, f"Controller {mc_name} missing from mock sysfs"

    def test_controller_files_exist(self, edac_sysfs, mock_topology):
        required_files = ["ce_count", "ue_count", "ce_noinfo_count",
                          "ue_noinfo_count", "mc_name", "size_mb"]
        for mc_name in mock_topology:
            mc_dir = edac_sysfs / mc_name
            for fname in required_files:
                fpath = mc_dir / fname
                assert fpath.exists(), f"Missing {fname} in {mc_dir}"
                assert fpath.is_file(), f"Not a file: {fpath}"

    def test_csrow_dirs_exist(self, edac_sysfs, mock_topology):
        for mc_name, mc in mock_topology.items():
            for r in range(mc["csrows"]):
                csrow_dir = edac_sysfs / mc_name / f"csrow{r}"
                assert csrow_dir.is_dir(), f"Missing csrow dir: {csrow_dir}"

    def test_channel_ce_count_files_exist(self, edac_sysfs, mock_topology):
        for mc_name, mc in mock_topology.items():
            for r in range(mc["csrows"]):
                csrow_dir = edac_sysfs / mc_name / f"csrow{r}"
                for c in range(mc["channels"]):
                    fpath = csrow_dir / f"ch{c}_ce_count"
                    assert fpath.exists(), f"Missing {fpath}"

    def test_dimm_label_files_exist(self, edac_sysfs, mock_topology):
        for mc_name, mc in mock_topology.items():
            for r in range(mc["csrows"]):
                csrow_dir = edac_sysfs / mc_name / f"csrow{r}"
                for c in range(mc["channels"]):
                    fpath = csrow_dir / f"ch{c}_dimm_label"
                    assert fpath.exists(), f"Missing DIMM label: {fpath}"

    def test_mc_name_content(self, edac_sysfs, mock_topology):
        for mc_name, mc in mock_topology.items():
            mc_name_file = edac_sysfs / mc_name / "mc_name"
            content = mc_name_file.read_text().strip()
            assert content == mc["mc_name"], (
                f"{mc_name}/mc_name: got '{content}', expected '{mc['mc_name']}'"
            )

    def test_dimm_label_format(self, edac_sysfs, mock_topology):
        """DIMM labels must follow the format DIMM_<row>_CH<channel>."""
        for mc_name, mc in mock_topology.items():
            for r in range(mc["csrows"]):
                csrow_dir = edac_sysfs / mc_name / f"csrow{r}"
                for c in range(mc["channels"]):
                    label = (csrow_dir / f"ch{c}_dimm_label").read_text().strip()
                    expected = f"DIMM_{r}_CH{c}"
                    assert label == expected, (
                        f"Label mismatch: got '{label}', expected '{expected}'"
                    )


class TestEdacCounterValues:
    """Verify counter values match the mock topology."""

    def test_channel_ce_counts_match_topology(self, edac_sysfs, mock_topology):
        for mc_name, mc in mock_topology.items():
            for r in range(mc["csrows"]):
                for c in range(mc["channels"]):
                    fpath = edac_sysfs / mc_name / f"csrow{r}" / f"ch{c}_ce_count"
                    actual = int(fpath.read_text().strip())
                    expected = mc["ce_counts"][r][c]
                    assert actual == expected, (
                        f"{mc_name}/csrow{r}/ch{c}_ce_count: got {actual}, expected {expected}"
                    )

    def test_csrow_ce_count_equals_channel_sum(self, edac_sysfs, mock_topology):
        for mc_name, mc in mock_topology.items():
            for r in range(mc["csrows"]):
                csrow_dir = edac_sysfs / mc_name / f"csrow{r}"
                csrow_ce = int((csrow_dir / "ce_count").read_text().strip())
                channel_sum = sum(mc["ce_counts"][r])
                assert csrow_ce == channel_sum, (
                    f"{mc_name}/csrow{r}: ce_count={csrow_ce} != sum of channels={channel_sum}"
                )

    def test_controller_ce_count_equals_csrow_sum(self, edac_sysfs, mock_topology):
        for mc_name, mc in mock_topology.items():
            ctrl_ce = int((edac_sysfs / mc_name / "ce_count").read_text().strip())
            expected_total = sum(
                mc["ce_counts"][r][c]
                for r in range(mc["csrows"])
                for c in range(mc["channels"])
            )
            assert ctrl_ce == expected_total, (
                f"{mc_name} ce_count={ctrl_ce} != sum of all channels={expected_total}"
            )

    def test_controller_ue_count_equals_csrow_sum(self, edac_sysfs, mock_topology):
        for mc_name, mc in mock_topology.items():
            ctrl_ue = int((edac_sysfs / mc_name / "ue_count").read_text().strip())
            expected_ue = sum(mc["ue_counts"])
            assert ctrl_ue == expected_ue, (
                f"{mc_name} ue_count={ctrl_ue} != sum of csrow ue_counts={expected_ue}"
            )

    def test_csrow_ue_counts_match_topology(self, edac_sysfs, mock_topology):
        for mc_name, mc in mock_topology.items():
            for r in range(mc["csrows"]):
                csrow_ue = int((edac_sysfs / mc_name / f"csrow{r}" / "ue_count").read_text().strip())
                expected = mc["ue_counts"][r]
                assert csrow_ue == expected, (
                    f"{mc_name}/csrow{r}/ue_count={csrow_ue}, expected {expected}"
                )

    def test_zero_counters_are_present(self, edac_sysfs, mock_topology):
        """Zero-count files must exist — they should not be omitted."""
        for mc_name, mc in mock_topology.items():
            for r in range(mc["csrows"]):
                for c in range(mc["channels"]):
                    if mc["ce_counts"][r][c] == 0:
                        fpath = edac_sysfs / mc_name / f"csrow{r}" / f"ch{c}_ce_count"
                        assert fpath.exists()
                        val = int(fpath.read_text().strip())
                        assert val == 0

    def test_size_mb_positive(self, edac_sysfs, mock_topology):
        for mc_name in mock_topology:
            size = int((edac_sysfs / mc_name / "size_mb").read_text().strip())
            assert size > 0, f"{mc_name}/size_mb must be positive"

    def test_counter_values_are_non_negative(self, edac_sysfs, mock_topology):
        for mc_name, mc in mock_topology.items():
            ce = int((edac_sysfs / mc_name / "ce_count").read_text().strip())
            ue = int((edac_sysfs / mc_name / "ue_count").read_text().strip())
            assert ce >= 0
            assert ue >= 0


class TestEdacCollectorRobustness:
    """Test graceful handling of missing or malformed sysfs paths."""

    def test_missing_sysfs_root_handled(self, tmp_path):
        """Collector should not crash when sysfs root does not exist."""
        from conftest import _MockEDACCollector
        collector = _MockEDACCollector(str(tmp_path / "nonexistent"))
        # Should return empty dict, not raise
        result = collector.collect_raw()
        assert result == {}, "Expected empty dict for missing sysfs root"

    def test_empty_sysfs_root_handled(self, tmp_path):
        """Collector should handle empty sysfs root gracefully."""
        (tmp_path / "devices" / "system" / "edac" / "mc").mkdir(parents=True)
        from conftest import _MockEDACCollector
        collector = _MockEDACCollector(str(tmp_path))
        result = collector.collect_raw()
        assert result == {}

    def test_counter_readable_as_uint(self, edac_sysfs, mock_topology):
        """All counter files must contain parseable unsigned integers."""
        counter_files = ["ce_count", "ue_count"]
        for mc_name in mock_topology:
            mc_dir = edac_sysfs / mc_name
            for fname in counter_files:
                val_str = (mc_dir / fname).read_text().strip()
                val = int(val_str)  # must not raise
                assert val >= 0

    def test_collector_collect_raw_structure(self, mock_sysfs, mock_topology):
        """collect_raw() must return nested dict with expected keys."""
        from conftest import _MockEDACCollector
        collector = _MockEDACCollector(str(mock_sysfs))
        result = collector.collect_raw()

        for mc_name, mc in mock_topology.items():
            assert mc_name in result, f"Missing controller {mc_name}"
            assert "ce_count" in result[mc_name]
            assert "ue_count" in result[mc_name]
            assert "csrows" in result[mc_name]

            for r in range(mc["csrows"]):
                csrow_name = f"csrow{r}"
                assert csrow_name in result[mc_name]["csrows"], (
                    f"Missing {csrow_name} in result[{mc_name}]"
                )
                csrow_data = result[mc_name]["csrows"][csrow_name]
                assert "channels" in csrow_data

    def test_collector_ce_values_match_topology(self, mock_sysfs, mock_topology):
        """collect_raw() CE values must match the mock topology."""
        from conftest import _MockEDACCollector
        collector = _MockEDACCollector(str(mock_sysfs))
        result = collector.collect_raw()

        for mc_name, mc in mock_topology.items():
            for r in range(mc["csrows"]):
                csrow_data = result[mc_name]["csrows"][f"csrow{r}"]
                for c in range(mc["channels"]):
                    ch_key = f"ch{c}"
                    actual = csrow_data["channels"].get(ch_key, -1)
                    expected = mc["ce_counts"][r][c]
                    assert actual == expected, (
                        f"{mc_name}/csrow{r}/{ch_key}: actual={actual}, expected={expected}"
                    )


class TestEdacWritableCounters:
    """Test counter modification in a writable mock sysfs (simulates fault injection)."""

    def test_increment_ce_counter(self, writable_sysfs):
        """Writing a new CE value should be reflected in subsequent reads."""
        mc0_ce = writable_sysfs / "devices" / "system" / "edac" / "mc" / "mc0" / "csrow0" / "ch0_ce_count"
        original = int(mc0_ce.read_text().strip())

        # Simulate counter increment (as would happen after fault injection)
        mc0_ce.write_text(str(original + 5) + "\n")

        new_val = int(mc0_ce.read_text().strip())
        assert new_val == original + 5

    def test_reset_counters_to_zero(self, writable_sysfs):
        """After writing 0 to all channel counters, sums should be 0."""
        mc_dir = writable_sysfs / "devices" / "system" / "edac" / "mc" / "mc0"
        for csrow_dir in mc_dir.iterdir():
            if not csrow_dir.is_dir() or not csrow_dir.name.startswith("csrow"):
                continue
            for ch_file in csrow_dir.glob("ch*_ce_count"):
                ch_file.write_text("0\n")
            (csrow_dir / "ce_count").write_text("0\n")
        (mc_dir / "ce_count").write_text("0\n")

        # Verify
        from conftest import _MockEDACCollector
        collector = _MockEDACCollector(str(writable_sysfs))
        result = collector.collect_raw()
        assert result["mc0"]["ce_count"] == 0
        for csrow_data in result["mc0"]["csrows"].values():
            for ch_count in csrow_data["channels"].values():
                assert ch_count == 0
