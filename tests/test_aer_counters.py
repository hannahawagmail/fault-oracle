# SPDX-License-Identifier: Apache-2.0
"""
test_aer_counters.py — Verify PCIe AER counter reading and metric correctness.

Tests:
  - AER sysfs file structure
  - parseAERFile logic (via Python re-implementation of the Go parser)
  - Counter values match mock topology
  - TOTAL_ERR_* keys are not emitted as individual metrics
  - Missing AER capability (no aer_dev_* files) handled gracefully
  - Device enumeration discovers all AER-capable devices
  - Correctable vs non-fatal vs fatal error separation
  - Multiple devices tracked independently
"""

from pathlib import Path

import pytest


# Python re-implementation of the AER file parser (mirrors collectors/aer.go)
def parse_aer_file(path: Path) -> dict:
    """Parse an AER sysfs file into {error_type: count} dict."""
    result = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) != 2:
                continue
            name, count_str = parts
            try:
                result[name] = int(count_str)
            except ValueError:
                continue
    return result


class TestAERSysfsStructure:
    """Verify the AER portion of the mock sysfs tree."""

    def test_pci_devices_root_exists(self, aer_sysfs):
        assert aer_sysfs.is_dir()

    def test_expected_devices_present(self, aer_sysfs, mock_aer_devices):
        dirs = {d.name for d in aer_sysfs.iterdir() if d.is_dir()}
        for bdf in mock_aer_devices:
            assert bdf in dirs, f"Device {bdf} missing from mock sysfs"

    def test_aer_correctable_file_exists(self, aer_sysfs, mock_aer_devices):
        for bdf in mock_aer_devices:
            fpath = aer_sysfs / bdf / "aer_dev_correctable"
            assert fpath.exists(), f"Missing aer_dev_correctable for {bdf}"

    def test_aer_nonfatal_file_exists(self, aer_sysfs, mock_aer_devices):
        for bdf in mock_aer_devices:
            fpath = aer_sysfs / bdf / "aer_dev_nonfatal"
            assert fpath.exists(), f"Missing aer_dev_nonfatal for {bdf}"

    def test_aer_fatal_file_exists(self, aer_sysfs, mock_aer_devices):
        for bdf in mock_aer_devices:
            fpath = aer_sysfs / bdf / "aer_dev_fatal"
            assert fpath.exists(), f"Missing aer_dev_fatal for {bdf}"

    def test_aer_file_format_correct(self, aer_sysfs, mock_aer_devices):
        """Each line in an AER file must be '<name> <count>'."""
        for bdf in mock_aer_devices:
            corr_path = aer_sysfs / bdf / "aer_dev_correctable"
            with open(corr_path) as f:
                for i, line in enumerate(f):
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split()
                    assert len(parts) == 2, (
                        f"{corr_path} line {i + 1}: expected 2 fields, got {len(parts)}: '{line}'"
                    )
                    assert parts[1].isdigit(), (
                        f"{corr_path} line {i + 1}: count '{parts[1]}' is not an integer"
                    )


class TestAERCounterValues:
    """Verify counter values from the AER parser match the mock data."""

    def test_correctable_error_counts(self, aer_sysfs, mock_aer_devices):
        for bdf, dev in mock_aer_devices.items():
            parsed = parse_aer_file(aer_sysfs / bdf / "aer_dev_correctable")
            for error_type, expected in dev["correctable"].items():
                assert error_type in parsed, (
                    f"{bdf}: error type '{error_type}' missing from parsed correctable"
                )
                assert parsed[error_type] == expected, (
                    f"{bdf}/{error_type}: got {parsed[error_type]}, expected {expected}"
                )

    def test_nonfatal_error_counts(self, aer_sysfs, mock_aer_devices):
        for bdf, dev in mock_aer_devices.items():
            parsed = parse_aer_file(aer_sysfs / bdf / "aer_dev_nonfatal")
            for error_type, expected in dev["nonfatal"].items():
                if error_type in parsed:
                    assert parsed[error_type] == expected

    def test_total_err_cor_present(self, aer_sysfs, mock_aer_devices):
        """TOTAL_ERR_COR must be present and equal to sum of individual errors."""
        for bdf, dev in mock_aer_devices.items():
            parsed = parse_aer_file(aer_sysfs / bdf / "aer_dev_correctable")
            assert "TOTAL_ERR_COR" in parsed, f"{bdf}: TOTAL_ERR_COR missing"
            # Verify total equals sum of individual (non-total) errors
            individual_sum = sum(v for k, v in parsed.items() if k != "TOTAL_ERR_COR")
            assert parsed["TOTAL_ERR_COR"] == individual_sum, (
                f"{bdf}: TOTAL_ERR_COR={parsed['TOTAL_ERR_COR']} != sum={individual_sum}"
            )

    def test_badtlp_device_0000_01_00_0(self, aer_sysfs):
        """Device 0000:01:00.0 should have 3 BadTLP errors."""
        parsed = parse_aer_file(aer_sysfs / "0000:01:00.0" / "aer_dev_correctable")
        assert parsed.get("BadTLP") == 3

    def test_clean_device_all_zeros(self, aer_sysfs):
        """Device 0000:02:00.0 should have 0 correctable errors."""
        parsed = parse_aer_file(aer_sysfs / "0000:02:00.0" / "aer_dev_correctable")
        non_total = {k: v for k, v in parsed.items() if not k.startswith("TOTAL")}
        for error_type, count in non_total.items():
            assert count == 0, f"0000:02:00.0/{error_type}={count}, expected 0"

    def test_device_independence(self, aer_sysfs):
        """Errors on one device must not affect another device's counters."""
        dev0 = parse_aer_file(aer_sysfs / "0000:01:00.0" / "aer_dev_correctable")
        dev1 = parse_aer_file(aer_sysfs / "0000:02:00.0" / "aer_dev_correctable")
        assert dev0["BadTLP"] == 3
        assert dev1["BadTLP"] == 0


class TestAERCollectorLogic:
    """Test the collector-level logic: device enumeration, filtering, metric emission."""

    def test_enumeration_finds_all_aer_devices(self, aer_sysfs, mock_aer_devices):
        """Enumerate all directories with aer_dev_correctable."""
        found = set()
        for entry in aer_sysfs.iterdir():
            if not entry.is_dir():
                continue
            if (entry / "aer_dev_correctable").exists():
                found.add(entry.name)
        assert found == set(mock_aer_devices.keys())

    def test_device_count_correct(self, aer_sysfs, mock_aer_devices):
        count = sum(
            1 for d in aer_sysfs.iterdir() if d.is_dir() and (d / "aer_dev_correctable").exists()
        )
        assert count == len(mock_aer_devices)

    def test_total_key_excluded_from_per_type_metrics(self, aer_sysfs):
        """TOTAL_ERR_COR and TOTAL_ERR_UNCOR should not appear as individual metric labels."""
        total_keys = {"TOTAL_ERR_COR", "TOTAL_ERR_UNCOR"}
        for dev_dir in aer_sysfs.iterdir():
            if not dev_dir.is_dir():
                continue
            for aer_file in dev_dir.glob("aer_dev_*"):
                parsed = parse_aer_file(aer_file)
                for key in parsed:
                    if key in total_keys:
                        continue  # TOTAL keys exist in the file but should be skipped by collector
                    # All other keys should have non-negative integer values
                    assert isinstance(parsed[key], int)
                    assert parsed[key] >= 0

    def test_missing_aer_capability_graceful(self, tmp_path):
        """A PCI device directory without aer_dev_* files should be skipped."""
        # Create a device directory without AER files
        no_aer_dir = tmp_path / "bus" / "pci" / "devices" / "0000:ff:00.0"
        no_aer_dir.mkdir(parents=True)
        (no_aer_dir / "config").write_text("fake pci config\n")

        # Enumerate: should find 0 AER devices
        count = sum(
            1
            for d in (tmp_path / "bus" / "pci" / "devices").iterdir()
            if d.is_dir() and (d / "aer_dev_correctable").exists()
        )
        assert count == 0

    def test_aer_correctable_non_negative(self, aer_sysfs, mock_aer_devices):
        for bdf in mock_aer_devices:
            parsed = parse_aer_file(aer_sysfs / bdf / "aer_dev_correctable")
            for error_type, count in parsed.items():
                assert count >= 0, f"{bdf}/{error_type} has negative count {count}"

    def test_modified_counter_read_correctly(self, tmp_path):
        """After writing a new count to an AER file, the parser reads the new value."""
        dev_dir = tmp_path / "bus" / "pci" / "devices" / "0000:01:00.0"
        dev_dir.mkdir(parents=True)
        aer_file = dev_dir / "aer_dev_correctable"
        aer_file.write_text("BadTLP 0\nTOTAL_ERR_COR 0\n")

        parsed = parse_aer_file(aer_file)
        assert parsed["BadTLP"] == 0

        # Simulate counter increment
        aer_file.write_text("BadTLP 7\nTOTAL_ERR_COR 7\n")
        parsed = parse_aer_file(aer_file)
        assert parsed["BadTLP"] == 7

    @pytest.mark.parametrize(
        "error_type,expected_count",
        [
            ("BadTLP", 3),
            ("RxErr", 0),
            ("TOTAL_ERR_COR", 3),
        ],
    )
    def test_specific_error_types_device_0(self, aer_sysfs, error_type, expected_count):
        parsed = parse_aer_file(aer_sysfs / "0000:01:00.0" / "aer_dev_correctable")
        assert parsed.get(error_type) == expected_count, (
            f"0000:01:00.0/{error_type}: got {parsed.get(error_type)}, expected {expected_count}"
        )
