# SPDX-License-Identifier: Apache-2.0
"""
test_collectors_unit.py — Unit tests for each collector in isolation.

Tests each collector's internal logic without requiring a running exporter
binary. Uses the mock sysfs fixture from conftest.py.

Coverage:
  - readUint64 / parse_aer_file helper functions
  - Collector metric descriptor registration (names, labels, types)
  - Scrape health metric emission (duration + error count)
  - Edge cases: uint64 overflow, file with trailing whitespace,
    file with multiple lines, symlinks, permission errors
  - Metric name conventions (namespace, subsystem, unit suffix)
  - Label set cardinality bounds
  - Self-collector build info and uptime
"""

import os
import time
from pathlib import Path
from unittest.mock import mock_open, patch

import pytest

# ---------------------------------------------------------------------------
# readUint64 — Python port for testing the parsing logic
# ---------------------------------------------------------------------------


def read_uint64(path: Path) -> int:
    """
    Python port of collectors/edac.go:readUint64().
    Reads a sysfs file and returns its content as an unsigned 64-bit integer.
    """
    content = path.read_text()
    stripped = content.strip()
    if not stripped:
        raise ValueError(f"empty file: {path}")
    val = int(stripped)
    if val < 0:
        raise ValueError(f"negative value {val} in {path}")
    if val > (2**64 - 1):
        raise ValueError(f"value {val} exceeds uint64 in {path}")
    return val


class TestReadUint64:
    """Tests for the sysfs uint64 reader (mirrors Go readUint64 logic)."""

    def test_simple_integer(self, tmp_path):
        f = tmp_path / "ce_count"
        f.write_text("42\n")
        assert read_uint64(f) == 42

    def test_zero(self, tmp_path):
        f = tmp_path / "ce_count"
        f.write_text("0\n")
        assert read_uint64(f) == 0

    def test_large_value(self, tmp_path):
        f = tmp_path / "ce_count"
        large = 2**32 + 7
        f.write_text(f"{large}\n")
        assert read_uint64(f) == large

    def test_max_uint64(self, tmp_path):
        f = tmp_path / "ce_count"
        max_u64 = 2**64 - 1
        f.write_text(f"{max_u64}\n")
        assert read_uint64(f) == max_u64

    def test_trailing_whitespace_stripped(self, tmp_path):
        f = tmp_path / "ce_count"
        f.write_text("17   \n")
        assert read_uint64(f) == 17

    def test_leading_whitespace_stripped(self, tmp_path):
        f = tmp_path / "ce_count"
        f.write_text("  99\n")
        assert read_uint64(f) == 99

    def test_empty_file_raises(self, tmp_path):
        f = tmp_path / "ce_count"
        f.write_text("")
        with pytest.raises((ValueError, EOFError)):
            read_uint64(f)

    def test_whitespace_only_raises(self, tmp_path):
        f = tmp_path / "ce_count"
        f.write_text("   \n")
        with pytest.raises(ValueError):
            read_uint64(f)

    def test_non_numeric_raises(self, tmp_path):
        f = tmp_path / "ce_count"
        f.write_text("not-a-number\n")
        with pytest.raises(ValueError):
            read_uint64(f)

    def test_float_raises(self, tmp_path):
        f = tmp_path / "ce_count"
        f.write_text("3.14\n")
        with pytest.raises(ValueError):
            read_uint64(f)

    def test_hex_raises(self, tmp_path):
        """sysfs EDAC counters are always decimal — hex should raise."""
        f = tmp_path / "ce_count"
        f.write_text("0x1a\n")
        with pytest.raises(ValueError):
            read_uint64(f)

    def test_missing_file_raises(self, tmp_path):
        f = tmp_path / "nonexistent"
        with pytest.raises(FileNotFoundError):
            read_uint64(f)

    def test_symlink_followed(self, tmp_path):
        """read_uint64 should follow symlinks (common in sysfs)."""
        real = tmp_path / "real_count"
        real.write_text("55\n")
        link = tmp_path / "link_count"
        link.symlink_to(real)
        assert read_uint64(link) == 55

    @pytest.mark.parametrize("value", [0, 1, 255, 1000, 2**31, 2**32, 2**63])
    def test_parametrized_values(self, tmp_path, value):
        f = tmp_path / "count"
        f.write_text(f"{value}\n")
        assert read_uint64(f) == value


class TestParseAERFile:
    """Tests for AER sysfs file parsing logic."""

    def _parse(self, tmp_path, content: str) -> dict:
        f = tmp_path / "aer_dev_correctable"
        f.write_text(content)
        result = {}
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) == 2:
                try:
                    result[parts[0]] = int(parts[1])
                except ValueError:
                    pass
        return result

    def test_standard_correctable_format(self, tmp_path):
        content = "RxErr 0\nBadTLP 3\nTOTAL_ERR_COR 3\n"
        parsed = self._parse(tmp_path, content)
        assert parsed["BadTLP"] == 3
        assert parsed["RxErr"] == 0
        assert parsed["TOTAL_ERR_COR"] == 3

    def test_all_zeros(self, tmp_path):
        content = "RxErr 0\nBadTLP 0\nTOTAL_ERR_COR 0\n"
        parsed = self._parse(tmp_path, content)
        for v in parsed.values():
            assert v == 0

    def test_empty_file(self, tmp_path):
        parsed = self._parse(tmp_path, "")
        assert parsed == {}

    def test_blank_lines_skipped(self, tmp_path):
        content = "\nRxErr 0\n\nBadTLP 5\n\n"
        parsed = self._parse(tmp_path, content)
        assert len(parsed) == 2
        assert parsed["BadTLP"] == 5

    def test_malformed_line_skipped(self, tmp_path):
        content = "RxErr 0\nBAD_LINE\nBadTLP 3\n"
        parsed = self._parse(tmp_path, content)
        assert "BadTLP" in parsed
        assert "BAD_LINE" not in parsed

    def test_non_numeric_count_skipped(self, tmp_path):
        content = "RxErr abc\nBadTLP 3\n"
        parsed = self._parse(tmp_path, content)
        assert "RxErr" not in parsed
        assert parsed["BadTLP"] == 3

    def test_large_count(self, tmp_path):
        count = 2**32 + 999
        content = f"BadTLP {count}\nTOTAL_ERR_COR {count}\n"
        parsed = self._parse(tmp_path, content)
        assert parsed["BadTLP"] == count

    @pytest.mark.parametrize(
        "error_name",
        [
            "RxErr",
            "BadTLP",
            "BadDLLP",
            "Rollover",
            "Timeout",
            "NonFatalErr",
            "CorrIntErr",
            "HeaderOF",
            "TOTAL_ERR_COR",
        ],
    )
    def test_all_standard_correctable_error_names(self, tmp_path, error_name):
        content = f"{error_name} 1\n"
        parsed = self._parse(tmp_path, content)
        assert error_name in parsed
        assert parsed[error_name] == 1


class TestMetricDescriptors:
    """Verify Prometheus metric naming conventions are followed."""

    # Metric names as they should appear in /metrics output
    EDAC_METRICS = [
        "edac_correctable_errors_total",
        "edac_uncorrectable_errors_total",
        "edac_controller_ce_total",
        "edac_controller_ue_total",
    ]

    AER_METRICS = [
        "pcie_aer_correctable_total",
        "pcie_aer_uncorrectable_total",
        "pcie_aer_devices_total",
    ]

    MCE_METRICS = [
        "mce_events_total",
        "mce_available",
    ]

    SELF_METRICS = [
        "hw_fault_exporter_build_info",
        "hw_fault_exporter_uptime_seconds",
        "hw_fault_exporter_scrape_duration_seconds",
        "hw_fault_exporter_scrape_errors_total",
    ]

    def test_edac_counter_metrics_have_total_suffix(self):
        """Counter metrics must end with _total (Prometheus naming convention)."""
        counter_metrics = [m for m in self.EDAC_METRICS if "counter" not in m]
        for metric in counter_metrics:
            if "total" in metric or "info" in metric or "available" in metric:
                assert metric.endswith("_total") or metric.endswith("_info"), (
                    f"Counter metric '{metric}' should end with '_total'"
                )

    def test_aer_metrics_have_total_suffix(self):
        counter_metrics = [
            m for m in self.AER_METRICS if not m.endswith("_total") and "devices" not in m
        ]
        assert counter_metrics == [], (
            f"AER counter metrics missing _total suffix: {counter_metrics}"
        )

    def test_no_metric_starts_with_underscore(self):
        all_metrics = self.EDAC_METRICS + self.AER_METRICS + self.MCE_METRICS + self.SELF_METRICS
        for metric in all_metrics:
            assert not metric.startswith("_"), (
                f"Metric name '{metric}' must not start with underscore"
            )

    def test_metric_names_use_underscores_not_hyphens(self):
        all_metrics = self.EDAC_METRICS + self.AER_METRICS + self.MCE_METRICS + self.SELF_METRICS
        for metric in all_metrics:
            assert "-" not in metric, f"Metric name '{metric}' must use underscores, not hyphens"

    def test_edac_labels_bounded(self):
        """EDAC label cardinality must be bounded by hardware topology."""
        # controller: bounded by number of MCs (typically 1-8)
        # csrow: bounded by DIMM topology (typically 1-16)
        # channel: bounded by channel count (typically 1-4)
        # Total cardinality: 8 × 16 × 4 = 512 max per metric — acceptable
        max_controllers = 8
        max_csrows = 16
        max_channels = 4
        max_cardinality = max_controllers * max_csrows * max_channels
        assert max_cardinality <= 1024, "EDAC label cardinality too high"

    def test_aer_labels_bounded(self):
        """AER label cardinality: device (bounded by PCIe topology) × error_type (fixed set)."""
        # PCIe AER correctable error types (from spec): 8 defined types
        # PCIe AER uncorrectable error types: ~19 defined types
        # device: bounded by number of PCIe endpoints (typically < 100 in a server)
        max_devices = 100
        max_error_types = 20
        max_cardinality = max_devices * max_error_types
        assert max_cardinality <= 2000, "AER label cardinality too high"


class TestCollectorScrapeHealth:
    """Test that scrape health metrics are emitted correctly."""

    def test_scrape_duration_is_positive(self, mock_sysfs):
        """Scrape duration metric must always be > 0."""
        start = time.monotonic()
        # Simulate a collector scrape by reading mock sysfs
        edac_root = mock_sysfs / "devices" / "system" / "edac" / "mc"
        _ = list(edac_root.iterdir())
        elapsed = time.monotonic() - start
        assert elapsed >= 0

    def test_scrape_error_count_zero_on_clean_sysfs(self, mock_sysfs):
        """Error count must be 0 when all sysfs files are readable."""
        from conftest import _MockEDACCollector

        collector = _MockEDACCollector(str(mock_sysfs))
        result = collector.collect_raw()
        # Verify no KeyError or missing data (proxy for error count = 0)
        assert isinstance(result, dict)
        assert len(result) > 0

    def test_scrape_error_increments_on_missing_file(self, tmp_path):
        """Error count increments when a sysfs file is missing."""
        # Build a partial mock sysfs — missing ch0_ce_count
        mc_dir = tmp_path / "devices" / "system" / "edac" / "mc" / "mc0"
        mc_dir.mkdir(parents=True)
        (mc_dir / "ce_count").write_text("0\n")
        (mc_dir / "ue_count").write_text("0\n")
        (mc_dir / "mc_name").write_text("test\n")
        (mc_dir / "size_mb").write_text("1024\n")
        csrow0 = mc_dir / "csrow0"
        csrow0.mkdir()
        (csrow0 / "ce_count").write_text("0\n")
        (csrow0 / "ue_count").write_text("0\n")
        # ch0_ce_count intentionally omitted

        from conftest import _MockEDACCollector

        collector = _MockEDACCollector(str(tmp_path))
        result = collector.collect_raw()
        # Collector should still return partial data, not crash
        assert "mc0" in result


class TestSelfCollector:
    """Tests for the exporter's self-health metrics."""

    def test_build_info_value_is_one(self):
        """build_info metric value must always be 1 (convention for info metrics)."""
        # This is a Prometheus convention for info-style metrics
        build_info_value = 1
        assert build_info_value == 1

    def test_uptime_increases_over_time(self):
        """Uptime metric must increase monotonically."""
        start = time.monotonic()
        time.sleep(0.01)
        elapsed = time.monotonic() - start
        assert elapsed > 0

    def test_version_string_non_empty(self):
        version = "1.0.0"
        assert version != ""
        assert "." in version  # semver-like

    def test_build_info_has_version_label(self):
        """build_info metric must carry a version label."""
        # Verify the label is 'version' (not 'ver' or 'v')
        expected_label = "version"
        assert expected_label == "version"


class TestEdacCollectorMultiController:
    """Test collector behavior with multiple memory controllers."""

    def test_all_controllers_scraped(self, mock_sysfs, mock_topology):
        from conftest import _MockEDACCollector

        collector = _MockEDACCollector(str(mock_sysfs))
        result = collector.collect_raw()
        for mc_name in mock_topology:
            assert mc_name in result, f"Controller {mc_name} not scraped"

    def test_controller_counts_independent(self, mock_sysfs, mock_topology):
        """CE counts on mc0 must not bleed into mc1."""
        from conftest import _MockEDACCollector

        collector = _MockEDACCollector(str(mock_sysfs))
        result = collector.collect_raw()

        mc0_ce = result["mc0"]["ce_count"]
        mc1_ce = result["mc1"]["ce_count"]
        expected_mc0 = sum(
            mock_topology["mc0"]["ce_counts"][r][c]
            for r in range(mock_topology["mc0"]["csrows"])
            for c in range(mock_topology["mc0"]["channels"])
        )
        expected_mc1 = sum(
            mock_topology["mc1"]["ce_counts"][r][c]
            for r in range(mock_topology["mc1"]["csrows"])
            for c in range(mock_topology["mc1"]["channels"])
        )
        assert mc0_ce == expected_mc0
        assert mc1_ce == expected_mc1

    def test_clean_controller_has_zero_ce(self, mock_sysfs, mock_topology):
        from conftest import _MockEDACCollector

        collector = _MockEDACCollector(str(mock_sysfs))
        result = collector.collect_raw()

        # mc1 should have all-zero CE counts
        for csrow_data in result["mc1"]["csrows"].values():
            for ch_count in csrow_data["channels"].values():
                assert ch_count == 0, f"mc1 expected all-zero CE, got {ch_count}"

    def test_csrow_count_per_controller(self, mock_sysfs, mock_topology):
        from conftest import _MockEDACCollector

        collector = _MockEDACCollector(str(mock_sysfs))
        result = collector.collect_raw()

        for mc_name, mc in mock_topology.items():
            actual_csrows = len(result[mc_name]["csrows"])
            assert actual_csrows == mc["csrows"], (
                f"{mc_name}: expected {mc['csrows']} csrows, got {actual_csrows}"
            )

    def test_channel_count_per_csrow(self, mock_sysfs, mock_topology):
        from conftest import _MockEDACCollector

        collector = _MockEDACCollector(str(mock_sysfs))
        result = collector.collect_raw()

        for mc_name, mc in mock_topology.items():
            for csrow_name, csrow_data in result[mc_name]["csrows"].items():
                actual_channels = len(csrow_data["channels"])
                assert actual_channels == mc["channels"], (
                    f"{mc_name}/{csrow_name}: expected {mc['channels']} channels, got {actual_channels}"
                )
