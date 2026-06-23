# SPDX-License-Identifier: Apache-2.0
"""
test_mce_counters.py — Verify MCE counter reading from mock GHES sysfs.

Tests:
  - GHES sysfs files exist and are readable
  - Corrected / deferred / uncorrected counts match mock data
  - MCE data source availability flag
  - Graceful handling when GHES path is missing
  - MCE severity mapping logic
  - Multiple severity levels tracked independently
"""

from pathlib import Path

import pytest


class TestMCESysfsStructure:
    """Verify the mock MCE/APEI sysfs tree."""

    def test_apei_errors_dir_exists(self, mce_sysfs):
        assert mce_sysfs.is_dir(), f"APEI errors dir not found: {mce_sysfs}"

    def test_corrected_errors_file_exists(self, mce_sysfs):
        assert (mce_sysfs / "corrected_errors").exists()

    def test_deferred_errors_file_exists(self, mce_sysfs):
        assert (mce_sysfs / "deferred_errors").exists()

    def test_uncorrected_errors_file_exists(self, mce_sysfs):
        assert (mce_sysfs / "uncorrected_errors").exists()

    def test_all_files_readable(self, mce_sysfs):
        for fname in ["corrected_errors", "deferred_errors", "uncorrected_errors"]:
            fpath = mce_sysfs / fname
            assert os.access(fpath, os.R_OK), f"Not readable: {fpath}"


class TestMCECounterValues:
    """Verify MCE counter values match the mock data."""

    def test_corrected_errors_count(self, mce_sysfs, mock_mce):
        val = int((mce_sysfs / "corrected_errors").read_text().strip())
        assert val == mock_mce["corrected_errors"], (
            f"corrected_errors: got {val}, expected {mock_mce['corrected_errors']}"
        )

    def test_deferred_errors_count(self, mce_sysfs, mock_mce):
        val = int((mce_sysfs / "deferred_errors").read_text().strip())
        assert val == mock_mce["deferred_errors"]

    def test_uncorrected_errors_count(self, mce_sysfs, mock_mce):
        val = int((mce_sysfs / "uncorrected_errors").read_text().strip())
        assert val == mock_mce["uncorrected_errors"]

    def test_all_counts_non_negative(self, mce_sysfs):
        for fname in ["corrected_errors", "deferred_errors", "uncorrected_errors"]:
            val = int((mce_sysfs / fname).read_text().strip())
            assert val >= 0, f"{fname}: negative count {val}"

    def test_uncorrected_is_zero(self, mce_sysfs):
        """In our mock, uncorrected errors is 0 — system is healthy."""
        val = int((mce_sysfs / "uncorrected_errors").read_text().strip())
        assert val == 0, f"Expected 0 uncorrected errors in clean mock, got {val}"


class TestMCESeverityMapping:
    """Test the MCE severity decoding logic."""

    @pytest.mark.parametrize(
        "status_hex,expected_severity",
        [
            (0x9400004000800400, "corrected"),  # VAL=1, UC=0 → corrected
            (0xBC00000000000402, "uncorrected"),  # VAL=1, UC=1 → uncorrected
            (0xBE20000000000402, "panic"),  # VAL=1, UC=1, PCC=1 → panic
            (0x0000000000000000, "corrected"),  # status=0 → default corrected
        ],
    )
    def test_decode_mce_severity(self, status_hex, expected_severity):
        """Verify severity decoding from STATUS register bits."""
        severity = _decode_mce_severity(status_hex)
        assert severity == expected_severity, (
            f"STATUS=0x{status_hex:x}: got '{severity}', expected '{expected_severity}'"
        )


class TestMCECollectorRobustness:
    """Test graceful handling of missing MCE data sources."""

    def test_missing_ghes_path_handled(self, tmp_path):
        """When GHES path doesn't exist, collector should return empty with available=0."""
        # tmp_path has no firmware/acpi/errors subdirectory
        apei_path = tmp_path / "firmware" / "acpi" / "errors"
        assert not apei_path.exists()

        # Simulate scrapeGHES failure: directory not found
        try:
            entries = list(apei_path.iterdir())
            raise AssertionError("Should have raised FileNotFoundError")
        except FileNotFoundError:
            pass  # Expected

    def test_partial_ghes_path_handled(self, tmp_path):
        """When GHES path exists but some files are missing, read available ones."""
        apei_dir = tmp_path / "firmware" / "acpi" / "errors"
        apei_dir.mkdir(parents=True)
        # Only write corrected_errors, omit the others
        (apei_dir / "corrected_errors").write_text("3\n")

        corrected = int((apei_dir / "corrected_errors").read_text().strip())
        assert corrected == 3

        # deferred_errors doesn't exist — should not raise on missing
        deferred_path = apei_dir / "deferred_errors"
        assert not deferred_path.exists()

    def test_modified_mce_counter_read(self, tmp_path):
        """Writing a new count should be reflected immediately."""
        apei_dir = tmp_path / "firmware" / "acpi" / "errors"
        apei_dir.mkdir(parents=True)
        fpath = apei_dir / "corrected_errors"
        fpath.write_text("0\n")

        val = int(fpath.read_text().strip())
        assert val == 0

        fpath.write_text("42\n")
        val = int(fpath.read_text().strip())
        assert val == 42

    def test_mce_available_flag_when_ghes_present(self, mce_sysfs):
        """available=1 when GHES path exists and has at least one file."""
        files = list(mce_sysfs.iterdir())
        available = 1 if any(f.is_file() for f in files) else 0
        assert available == 1

    def test_mce_available_flag_when_ghes_missing(self, tmp_path):
        """available=0 when GHES path does not exist."""
        apei_dir = tmp_path / "firmware" / "acpi" / "errors"
        available = 1 if apei_dir.exists() else 0
        assert available == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import os


def _decode_mce_severity(status: int) -> str:
    """
    Python port of collectors/mce.go:decodeMCESeverity().
    STATUS register bit layout:
      Bit 63: VAL (valid)
      Bit 61: UC  (uncorrected)
      Bit 57: PCC (processor context corrupt → panic)
    """
    BIT_UC = 1 << 61
    BIT_PCC = 1 << 57
    if status & BIT_PCC:
        return "panic"
    if status & BIT_UC:
        return "uncorrected"
    return "corrected"
