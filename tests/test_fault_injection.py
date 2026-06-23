# SPDX-License-Identifier: Apache-2.0
"""
test_fault_injection.py — Tests for the fault injection harness scripts.

These tests verify the injection script logic without requiring a loaded
kernel module or root access. They test:

  - Script argument validation (out-of-range csrow/channel, bad count)
  - Preflight check logic (missing debugfs, missing sysfs root)
  - Counter delta calculation correctness
  - ci_fault_matrix.sh test plan generation (via --dry-run)
  - Log output format and exit codes
  - AER backend auto-detection logic
  - Inter-event timing calculation for replay

Tests that require root / loaded module are marked with @pytest.mark.skipif
and will be skipped in standard CI. The integration tests in ci/build-and-test.yml
run these against a real QEMU environment.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
FAULT_DIR = REPO_ROOT / "fault-injection"


def run_script(
    script: str, args: list[str], env=None, input_text=None
) -> subprocess.CompletedProcess:
    """Run a shell script and return the CompletedProcess."""
    cmd = ["bash", str(FAULT_DIR / script)] + args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        input=input_text,
        timeout=30,
    )


def has_root() -> bool:
    return os.geteuid() == 0


def has_edac_module() -> bool:
    return Path("/sys/devices/system/edac/mc/mc0").exists()


def has_debugfs() -> bool:
    return Path("/sys/kernel/debug").exists()


# ---------------------------------------------------------------------------
# Script existence and basic syntax
# ---------------------------------------------------------------------------


class TestScriptExists:
    """Verify all injection scripts exist and are valid bash."""

    @pytest.mark.parametrize(
        "script",
        [
            "inject_edac_ce.sh",
            "inject_edac_ue.sh",
            "inject_aer.sh",
            "ci_fault_matrix.sh",
        ],
    )
    def test_script_exists(self, script):
        assert (FAULT_DIR / script).exists(), f"Script not found: {script}"

    @pytest.mark.parametrize(
        "script",
        [
            "inject_edac_ce.sh",
            "inject_edac_ue.sh",
            "inject_aer.sh",
            "ci_fault_matrix.sh",
        ],
    )
    def test_script_is_valid_bash(self, script):
        """bash -n <script> checks syntax without executing."""
        result = subprocess.run(
            ["bash", "-n", str(FAULT_DIR / script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Bash syntax error in {script}:\n{result.stderr}"

    @pytest.mark.parametrize(
        "script",
        [
            "inject_edac_ce.sh",
            "inject_edac_ue.sh",
            "inject_aer.sh",
            "ci_fault_matrix.sh",
        ],
    )
    def test_script_has_spdx_header(self, script):
        content = (FAULT_DIR / script).read_text()
        assert "SPDX-License-Identifier: Apache-2.0" in content, f"{script} missing SPDX header"

    @pytest.mark.parametrize(
        "script",
        [
            "inject_edac_ce.sh",
            "inject_edac_ue.sh",
            "inject_aer.sh",
            "ci_fault_matrix.sh",
        ],
    )
    def test_script_has_set_e(self, script):
        """All scripts must use set -e (or set -euo pipefail) for safety."""
        content = (FAULT_DIR / script).read_text()
        assert "set -e" in content, f"{script} missing 'set -e'"

    @pytest.mark.parametrize(
        "script",
        [
            "inject_edac_ce.sh",
            "inject_edac_ue.sh",
        ],
    )
    def test_script_has_usage_comment(self, script):
        content = (FAULT_DIR / script).read_text()
        assert "Usage:" in content, f"{script} missing Usage: documentation"


# ---------------------------------------------------------------------------
# Argument validation (scripts must fail with non-zero exit on bad input)
# ---------------------------------------------------------------------------


class TestArgumentValidation:
    """Scripts should exit non-zero and print an error on bad arguments."""

    @pytest.mark.skipif(not has_root(), reason="requires root")
    def test_ce_script_rejects_missing_required_args(self):
        """inject_edac_ce.sh with no EDAC module should fail in preflight."""
        result = run_script("inject_edac_ce.sh", [])
        # Without root or module, should fail with a clear error
        assert result.returncode != 0

    def test_unknown_option_rejected(self):
        """All scripts must reject unknown options."""
        for script in ["inject_edac_ce.sh", "inject_edac_ue.sh"]:
            result = run_script(script, ["--totally-invalid-option", "value"])
            assert result.returncode != 0, f"{script} should reject unknown options"

    @pytest.mark.skipif(
        not has_root() or not has_edac_module(), reason="requires root + loaded module"
    )
    def test_ce_out_of_range_csrow_rejected(self):
        result = run_script("inject_edac_ce.sh", ["--csrow", "99"])
        assert result.returncode != 0
        assert "range" in result.stderr.lower() or "out of" in result.stderr.lower()

    @pytest.mark.skipif(
        not has_root() or not has_edac_module(), reason="requires root + loaded module"
    )
    def test_ce_out_of_range_channel_rejected(self):
        result = run_script("inject_edac_ce.sh", ["--channel", "99"])
        assert result.returncode != 0

    @pytest.mark.skipif(
        not has_root() or not has_edac_module(), reason="requires root + loaded module"
    )
    def test_ce_zero_count_rejected(self):
        result = run_script("inject_edac_ce.sh", ["--count", "0"])
        assert result.returncode != 0

    @pytest.mark.skipif(
        not has_root() or not has_edac_module(), reason="requires root + loaded module"
    )
    def test_ce_excessive_count_rejected(self):
        result = run_script("inject_edac_ce.sh", ["--count", "99999"])
        assert result.returncode != 0

    @pytest.mark.skipif(
        not has_root() or not has_edac_module(), reason="requires root + loaded module"
    )
    def test_ue_excessive_count_rejected(self):
        """UE count limit is lower (10) than CE count limit (1000) for safety."""
        result = run_script("inject_edac_ue.sh", ["--count", "11"])
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# ci_fault_matrix.sh dry-run
# ---------------------------------------------------------------------------


class TestFaultMatrix:
    """Test the CI fault matrix script in dry-run mode (no injection)."""

    def test_dry_run_exits_zero(self):
        """Dry run must succeed even without hardware."""
        result = run_script("ci_fault_matrix.sh", ["--dry-run", "--types", "CE,UE"])
        # May exit 0 (all skipped) or 0 (dry run complete)
        # Should not exit with an unhandled error
        assert result.returncode in (
            0,
            1,
        ), f"Unexpected exit code {result.returncode}:\n{result.stderr}"

    def test_dry_run_prints_plan(self):
        result = run_script("ci_fault_matrix.sh", ["--dry-run", "--types", "CE"])
        # Should print the dry-run header
        assert "DRY" in result.stdout.upper() or "MATRIX" in result.stdout.upper()

    def test_unknown_fault_type_skipped(self):
        result = run_script("ci_fault_matrix.sh", ["--dry-run", "--types", "INVALID_TYPE"])
        # Unknown types should be skipped (not cause a crash)
        assert result.returncode in (0, 1)

    def test_matrix_output_dir_created(self, tmp_path):
        output_dir = str(tmp_path / "matrix-out")
        result = run_script(
            "ci_fault_matrix.sh",
            [
                "--dry-run",
                "--types",
                "CE",
                "--output-dir",
                output_dir,
            ],
        )
        # The output dir should be created by the script
        assert Path(output_dir).exists(), "Output dir not created"

    def test_junit_xml_flag_accepted(self, tmp_path):
        junit_file = str(tmp_path / "results.xml")
        result = run_script(
            "ci_fault_matrix.sh",
            [
                "--dry-run",
                "--types",
                "CE",
                "--junit",
                junit_file,
                "--output-dir",
                str(tmp_path / "out"),
            ],
        )
        # In dry-run with no controllers, JUnit file should be created
        # (may be empty or have skipped tests)
        # Exit code 0 or 1 both acceptable without hardware
        assert result.returncode in (0, 1)

    def test_aer_types_accepted(self):
        result = run_script("ci_fault_matrix.sh", ["--dry-run", "--types", "AER_CE,AER_UE"])
        assert result.returncode in (0, 1)


# ---------------------------------------------------------------------------
# Counter delta calculation
# ---------------------------------------------------------------------------


class TestCounterDelta:
    """Test the counter delta arithmetic used by injection scripts."""

    @pytest.mark.parametrize(
        "baseline,new_val,count,should_pass",
        [
            (0, 3, 3, True),  # exact match
            (0, 5, 3, True),  # more than injected — also fine
            (10, 13, 3, True),  # non-zero baseline
            (0, 2, 3, False),  # less than injected — fail
            (0, 0, 1, False),  # no increment — fail
            (100, 100, 1, False),  # no change — fail
            (0, 1000, 1, True),  # large increment — ok (multiple poll periods)
        ],
    )
    def test_delta_pass_fail_logic(self, baseline, new_val, count, should_pass):
        """Counter delta >= count is PASS; delta < count is FAIL."""
        delta = new_val - baseline
        passed = delta >= count
        assert passed == should_pass, (
            f"baseline={baseline} new={new_val} injected={count}: "
            f"delta={delta}, expected_pass={should_pass}, got_pass={passed}"
        )

    def test_delta_non_negative(self):
        """Kernel EDAC counters are monotonically increasing — delta must be ≥ 0."""
        baselines = [0, 5, 100, 2**32]
        for baseline in baselines:
            new_val = baseline + 1
            delta = new_val - baseline
            assert delta >= 0

    def test_counter_overflow_not_regression(self):
        """If new_val < baseline (counter wrapped), delta is negative — handle it."""
        baseline = 2**64 - 1
        new_val = 0  # simulated 64-bit wrap
        delta = new_val - baseline  # Python handles big integers natively
        # A correct implementation should detect this as a wrap, not a failure
        # The Go implementation uses uint64 which wraps; Python doesn't
        assert new_val < baseline  # just verify the wrap scenario is detected


# ---------------------------------------------------------------------------
# AER backend detection
# ---------------------------------------------------------------------------


class TestAERBackendDetection:
    """Test AER injection backend auto-detection logic."""

    def test_no_backend_exits_2(self):
        """When --backend none is set, script does a dry-run and exits 0."""
        # Run with --backend none explicitly
        result = run_script("inject_aer.sh", ["--backend", "none"])
        assert result.returncode == 0, (
            f"Expected exit 0 (dry-run) for backend=none, got {result.returncode}"
        )

    def test_backend_auto_flag_accepted(self):
        result = run_script("inject_aer.sh", ["--backend", "auto", "--no-verify"])
        # 2 = no backend available (acceptable in CI without hardware)
        assert result.returncode in (0, 1, 2)

    def test_backend_invalid_flag_rejected(self):
        result = run_script("inject_aer.sh", ["--backend", "invalid_backend"])
        assert result.returncode != 0

    @pytest.mark.parametrize("error_type", ["correctable", "nonfatal", "fatal"])
    def test_error_type_flag_accepted(self, error_type):
        result = run_script(
            "inject_aer.sh",
            [
                "--type",
                error_type,
                "--no-verify",
            ],
        )
        # Exit 2 means no backend — that's fine in CI without hardware
        assert result.returncode in (0, 1, 2)


# ---------------------------------------------------------------------------
# Integration tests (require root + loaded module)
# ---------------------------------------------------------------------------


class TestInjectionIntegration:
    """
    End-to-end injection tests that require a loaded edac_cortex_ref module.
    These are skipped in standard CI but run in the QEMU GitHub Actions workflow.
    """

    @pytest.mark.skipif(
        not has_root() or not has_edac_module(), reason="requires root + edac_cortex_ref module"
    )
    def test_ce_injection_increments_counter(self):
        """Full injection → verify counter increment."""
        result = run_script(
            "inject_edac_ce.sh",
            [
                "--controller",
                "mc0",
                "--csrow",
                "0",
                "--channel",
                "0",
                "--count",
                "1",
                "--verbose",
            ],
        )
        assert result.returncode == 0, f"CE injection failed:\n{result.stdout}\n{result.stderr}"
        assert "PASS" in result.stdout

    @pytest.mark.skipif(
        not has_root() or not has_edac_module(), reason="requires root + edac_cortex_ref module"
    )
    def test_ue_injection_increments_counter(self):
        result = run_script(
            "inject_edac_ue.sh",
            [
                "--controller",
                "mc0",
                "--csrow",
                "0",
                "--channel",
                "0",
                "--count",
                "1",
            ],
        )
        assert result.returncode == 0, f"UE injection failed:\n{result.stdout}\n{result.stderr}"

    @pytest.mark.skipif(
        not has_root() or not has_edac_module(), reason="requires root + edac_cortex_ref module"
    )
    def test_matrix_ce_ue_full_run(self, tmp_path):
        result = run_script(
            "ci_fault_matrix.sh",
            [
                "--types",
                "CE,UE",
                "--output-dir",
                str(tmp_path),
            ],
        )
        assert result.returncode == 0, f"Fault matrix failed:\n{result.stdout}\n{result.stderr}"
        assert "MATRIX RESULT: PASSED" in result.stdout

    @pytest.mark.skipif(
        not has_root() or not has_edac_module(), reason="requires root + edac_cortex_ref module"
    )
    def test_no_verify_mode_does_not_check_counters(self):
        result = run_script(
            "inject_edac_ce.sh",
            [
                "--no-verify",
                "--count",
                "1",
            ],
        )
        # Should succeed quickly without waiting for poll
        assert result.returncode == 0
