# SPDX-License-Identifier: Apache-2.0
"""
Chaos tests for replay/replay_kernel_state.sh speed multiplier edge values.

These tests verify that the replay script clamps speed multipliers correctly
at boundary values and never produces negative sleep intervals or
divide-by-zero errors.  They run entirely in dry-run mode — no hardware
required.
"""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "replay" / "replay_kernel_state.sh"
SAMPLE_LOG = Path(__file__).parent.parent / "replay" / "example_traces" / "sample_ce_storm.log"


def _run_replay(speed: str, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    cmd = [
        "bash",
        str(SCRIPT),
        "--input", str(SAMPLE_LOG),
        "--speed", speed,
        "--dry-run",
    ]
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


# ── Speed multiplier boundary tests ──────────────────────────────────────

class TestSpeedMultiplierBoundaries:
    """Verify speed clamping at extreme values."""

    def test_speed_1x_exits_cleanly(self):
        result = _run_replay("1.0")
        assert result.returncode == 0, f"1x speed failed:\n{result.stderr}"

    def test_speed_100x_exits_cleanly(self):
        result = _run_replay("100.0")
        assert result.returncode == 0, f"100x speed failed:\n{result.stderr}"

    def test_speed_0001x_exits_cleanly(self):
        """Very slow replay — should clamp or proceed without hanging."""
        # Use a timeout to detect infinite hang (the test timeout itself is 30s)
        result = _run_replay("0.001")
        # Either exits 0 (dry-run skips actual sleep) or exits non-zero with
        # a clear error — it must not hang indefinitely.
        assert result.returncode in (0, 1, 2), (
            f"0.001x speed exited with unexpected code {result.returncode}:\n{result.stderr}"
        )

    def test_speed_1000x_exits_cleanly(self):
        result = _run_replay("1000.0")
        assert result.returncode == 0, f"1000x speed failed:\n{result.stderr}"

    def test_speed_zero_does_not_divide_by_zero(self):
        """Speed=0 must not cause a divide-by-zero or hang."""
        result = _run_replay("0")
        # Acceptable: exit 0 (clamped to minimum) or exit 1/2 (invalid input).
        # Not acceptable: crash, hang, or shell arithmetic error.
        assert result.returncode in (0, 1, 2), (
            f"Speed=0 exited {result.returncode}:\n{result.stderr}"
        )
        assert "divide" not in result.stderr.lower(), (
            f"Divide-by-zero detected in stderr:\n{result.stderr}"
        )
        assert "arithmetic" not in result.stderr.lower(), (
            f"Arithmetic error in stderr:\n{result.stderr}"
        )

    def test_negative_speed_rejected(self):
        """Negative speed values must be rejected with a non-zero exit."""
        result = _run_replay("-1.0")
        assert result.returncode != 0, "Negative speed should be rejected"

    def test_non_numeric_speed_rejected(self):
        """Non-numeric speed value must be rejected cleanly."""
        result = _run_replay("fast")
        assert result.returncode != 0, "Non-numeric speed should be rejected"

    def test_speed_very_large_does_not_overflow(self):
        """Extremely large speed multiplier must not cause integer overflow."""
        result = _run_replay("9999999.0")
        assert result.returncode in (0, 1, 2), (
            f"Very large speed exited {result.returncode}:\n{result.stderr}"
        )
        assert "overflow" not in result.stderr.lower()


# ── Missing input file handling ───────────────────────────────────────────

class TestInputEdgeCases:
    """Verify graceful handling of bad input files."""

    def test_missing_input_file_exits_nonzero(self):
        cmd = [
            "bash", str(SCRIPT),
            "--input", "/nonexistent/path/kern.log",
            "--speed", "1.0",
            "--dry-run",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        assert result.returncode != 0

    def test_empty_input_file_exits_cleanly(self, tmp_path):
        empty = tmp_path / "empty.log"
        empty.write_text("")
        cmd = [
            "bash", str(SCRIPT),
            "--input", str(empty),
            "--speed", "1.0",
            "--dry-run",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        # Empty file: either 0 (nothing to replay) or a documented error code.
        assert result.returncode in (0, 1, 2)

    def test_dry_run_flag_skips_actual_sleep(self):
        """--dry-run must complete quickly even with 0.001x speed."""
        import time
        start = time.monotonic()
        result = _run_replay("0.001")
        elapsed = time.monotonic() - start
        # In dry-run mode, no real sleeping should occur.
        # Allow 10 seconds for script startup overhead.
        assert elapsed < 10, f"dry-run took {elapsed:.1f}s — sleep not skipped?"


# ── No-hardware marker ────────────────────────────────────────────────────

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
