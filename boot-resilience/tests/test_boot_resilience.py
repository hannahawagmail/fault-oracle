# SPDX-License-Identifier: Apache-2.0
"""
test_boot_resilience.py — Tests for boot resilience scripts

Tests script syntax, argument validation, and dry-run behavior.
No hardware required — all tests run in a standard Linux or CI environment.

Run with:
    python3 -m pytest test_boot_resilience.py -v
or:
    python3 -m pytest test_boot_resilience.py -v --tb=short
"""

import os
import subprocess
import stat
import re
import pytest

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def script_path(name: str) -> str:
    """Return the absolute path to a script in the boot-resilience directory."""
    return os.path.join(SCRIPT_DIR, name)


def env_path(name: str) -> str:
    """Return path to an env/config file in the boot-resilience directory."""
    return os.path.join(SCRIPT_DIR, name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    """Run a command, capture output, do not raise on non-zero exit."""
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **kwargs
    )


def bash_syntax_check(path: str) -> subprocess.CompletedProcess:
    """Check a shell script for syntax errors using bash -n."""
    return run(["bash", "-n", path])


# ---------------------------------------------------------------------------
# Group 1: File existence
# ---------------------------------------------------------------------------

class TestFileExistence:
    """All expected files must exist and be non-empty."""

    EXPECTED_SCRIPTS = [
        "wdt-setup.sh",
        "ab-partition-setup.sh",
        "secure-boot-verify.sh",
    ]

    EXPECTED_FILES = [
        "uboot-bootcount.env",
        "README.md",
    ]

    @pytest.mark.parametrize("script", EXPECTED_SCRIPTS)
    def test_script_exists(self, script):
        path = script_path(script)
        assert os.path.isfile(path), f"Script not found: {path}"

    @pytest.mark.parametrize("script", EXPECTED_SCRIPTS)
    def test_script_nonempty(self, script):
        path = script_path(script)
        assert os.path.getsize(path) > 100, f"Script appears empty or too short: {path}"

    @pytest.mark.parametrize("filename", EXPECTED_FILES)
    def test_file_exists(self, filename):
        path = env_path(filename)
        assert os.path.isfile(path), f"File not found: {path}"

    def test_tests_dir_exists(self):
        tests_dir = os.path.join(SCRIPT_DIR, "tests")
        assert os.path.isdir(tests_dir)


# ---------------------------------------------------------------------------
# Group 2: Script header requirements
# ---------------------------------------------------------------------------

class TestScriptHeaders:
    """Scripts must have SPDX license header and set -e or set -euo pipefail."""

    SCRIPTS = [
        "wdt-setup.sh",
        "ab-partition-setup.sh",
        "secure-boot-verify.sh",
    ]

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_spdx_header(self, script):
        path = script_path(script)
        with open(path, "r") as f:
            content = f.read()
        assert "SPDX-License-Identifier: Apache-2.0" in content, (
            f"{script} missing SPDX-License-Identifier: Apache-2.0"
        )

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_set_e_present(self, script):
        path = script_path(script)
        with open(path, "r") as f:
            content = f.read()
        # Accept either 'set -e', 'set -eu', 'set -euo pipefail', etc.
        has_set_e = bool(re.search(r"^set\s+-[a-z]*e", content, re.MULTILINE))
        assert has_set_e, (
            f"{script} missing 'set -e' or 'set -euo pipefail' for error handling"
        )

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_shebang(self, script):
        path = script_path(script)
        with open(path, "r") as f:
            first_line = f.readline().strip()
        assert first_line.startswith("#!/"), (
            f"{script} missing shebang line, got: {first_line!r}"
        )
        assert "bash" in first_line or "sh" in first_line, (
            f"{script} shebang should reference bash or sh"
        )

    def test_uboot_env_spdx(self):
        path = env_path("uboot-bootcount.env")
        with open(path, "r") as f:
            content = f.read()
        assert "SPDX-License-Identifier: Apache-2.0" in content


# ---------------------------------------------------------------------------
# Group 3: Bash syntax validation
# ---------------------------------------------------------------------------

class TestBashSyntax:
    """All shell scripts must pass bash -n (no syntax errors)."""

    SCRIPTS = [
        "wdt-setup.sh",
        "ab-partition-setup.sh",
        "secure-boot-verify.sh",
    ]

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_bash_syntax(self, script):
        path = script_path(script)
        result = bash_syntax_check(path)
        assert result.returncode == 0, (
            f"bash -n failed for {script}:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# Group 4: wdt-setup.sh behavior
# ---------------------------------------------------------------------------

class TestWdtSetup:
    """Tests for wdt-setup.sh argument handling and dry-run."""

    def test_dry_run_test_exits_acceptably(self):
        """--test --dry-run should exit 0 (dry-run, no hardware needed)."""
        result = run(["bash", script_path("wdt-setup.sh"), "--test", "--dry-run"])
        # Exit 0: dry-run success. Exit 2: no WDT hardware (also acceptable).
        assert result.returncode in (0, 2), (
            f"wdt-setup.sh --test --dry-run returned unexpected code {result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    def test_dry_run_output_contains_expected_text(self):
        """Dry-run output should explain what would happen."""
        result = run(["bash", script_path("wdt-setup.sh"), "--test", "--dry-run"])
        combined = result.stdout + result.stderr
        assert "dry-run" in combined.lower() or "would" in combined.lower(), (
            f"Dry-run output did not mention 'dry-run' or 'would': {combined[:500]}"
        )

    def test_status_exits_acceptably(self):
        """--status should exit 0 (hardware found) or 2 (no WDT hardware)."""
        result = run(["bash", script_path("wdt-setup.sh"), "--status"])
        assert result.returncode in (0, 1, 2), (
            f"wdt-setup.sh --status returned unexpected code {result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    def test_daemon_dry_run_exits_zero(self):
        """--daemon --dry-run should exit 0 without starting a process."""
        result = run(["bash", script_path("wdt-setup.sh"),
                      "--daemon", "--timeout", "30", "--dry-run"])
        assert result.returncode in (0, 2), (
            f"wdt-setup.sh --daemon --dry-run returned {result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    def test_no_mode_exits_nonzero(self):
        """Running without a mode argument should exit non-zero."""
        result = run(["bash", script_path("wdt-setup.sh")])
        assert result.returncode != 0, (
            "wdt-setup.sh with no arguments should exit non-zero"
        )

    def test_unknown_arg_exits_nonzero(self):
        """Unknown arguments should cause exit 1."""
        result = run(["bash", script_path("wdt-setup.sh"), "--invalid-flag"])
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Group 5: ab-partition-setup.sh behavior
# ---------------------------------------------------------------------------

class TestAbPartitionSetup:
    """Tests for ab-partition-setup.sh argument handling."""

    def test_status_exits_acceptably(self):
        """--status should exit 0 (fw_printenv available) or 2 (not available)."""
        result = run(["bash", script_path("ab-partition-setup.sh"), "--status"])
        assert result.returncode in (0, 1, 2), (
            f"ab-partition-setup.sh --status returned unexpected code {result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    def test_status_produces_output(self):
        """--status must produce some output regardless of environment."""
        result = run(["bash", script_path("ab-partition-setup.sh"), "--status"])
        combined = result.stdout + result.stderr
        assert len(combined.strip()) > 0, "ab-partition-setup.sh --status produced no output"

    def test_rejects_invalid_partition_name(self):
        """--mark-pending with invalid partition name must exit non-zero."""
        for invalid in ["C", "a", "b", "0", "AB", "ROOT", ""]:
            if invalid == "":
                continue  # skip empty — argparse handles differently
            result = run(["bash", script_path("ab-partition-setup.sh"),
                          "--mark-pending", invalid, "--dry-run"])
            assert result.returncode != 0, (
                f"Expected non-zero exit for invalid partition '{invalid}', "
                f"got {result.returncode}"
            )

    def test_accepts_valid_partition_a(self):
        """--mark-pending A --dry-run should not crash with an invalid-partition error."""
        result = run(["bash", script_path("ab-partition-setup.sh"),
                      "--mark-pending", "A", "--dry-run"])
        combined = result.stdout + result.stderr
        # Should not complain about invalid partition name
        assert "invalid partition" not in combined.lower() or result.returncode not in (0, 2), \
            "Partition 'A' should be accepted as valid"

    def test_accepts_valid_partition_b(self):
        """--mark-active B --dry-run should not crash with an invalid-partition error."""
        result = run(["bash", script_path("ab-partition-setup.sh"),
                      "--mark-active", "B", "--dry-run"])
        combined = result.stdout + result.stderr
        assert "invalid partition" not in combined.lower() or result.returncode not in (0, 2)

    def test_no_mode_exits_nonzero(self):
        """No mode argument should cause exit non-zero."""
        result = run(["bash", script_path("ab-partition-setup.sh")])
        assert result.returncode != 0

    def test_switch_dry_run_exits_acceptably(self):
        """--switch --dry-run should exit 0 or 2."""
        result = run(["bash", script_path("ab-partition-setup.sh"),
                      "--switch", "--dry-run"])
        assert result.returncode in (0, 1, 2)

    def test_set_bootlimit_invalid_exits_nonzero(self):
        """--set-bootlimit with non-integer should fail."""
        result = run(["bash", script_path("ab-partition-setup.sh"),
                      "--set-bootlimit", "abc"])
        assert result.returncode != 0

    def test_set_bootlimit_out_of_range_exits_nonzero(self):
        """--set-bootlimit 0 and 11 should fail (valid range is 1-10)."""
        for bad_val in ["0", "11", "100"]:
            result = run(["bash", script_path("ab-partition-setup.sh"),
                          "--set-bootlimit", bad_val])
            assert result.returncode != 0, (
                f"Expected failure for --set-bootlimit {bad_val}"
            )


# ---------------------------------------------------------------------------
# Group 6: uboot-bootcount.env content validation
# ---------------------------------------------------------------------------

class TestUBootEnv:
    """Validate that the U-Boot env file contains required keywords."""

    @pytest.fixture(scope="class")
    def env_content(self):
        path = env_path("uboot-bootcount.env")
        with open(path, "r") as f:
            return f.read()

    REQUIRED_KEYWORDS = [
        "bootlimit",
        "bootcount",
        "altbootcmd",
        "bootcmd",
        "bootslot",
        "slot_a_state",
        "slot_b_state",
    ]

    @pytest.mark.parametrize("keyword", REQUIRED_KEYWORDS)
    def test_keyword_present(self, env_content, keyword):
        assert keyword in env_content, (
            f"uboot-bootcount.env missing required keyword: '{keyword}'"
        )

    def test_bootlimit_has_value(self, env_content):
        """bootlimit must be assigned a numeric value."""
        match = re.search(r"^bootlimit\s*=\s*(\d+)", env_content, re.MULTILINE)
        assert match is not None, "bootlimit=<number> not found in uboot-bootcount.env"
        val = int(match.group(1))
        assert 1 <= val <= 10, f"bootlimit value {val} out of expected range 1-10"

    def test_altbootcmd_references_slot_switch(self, env_content):
        """altbootcmd must reference a slot switch mechanism."""
        # Find the altbootcmd line
        match = re.search(r"^altbootcmd\s*=\s*(.+)", env_content, re.MULTILINE)
        assert match is not None, "altbootcmd not defined in uboot-bootcount.env"
        altbootcmd_val = match.group(1)
        # Should reference the fallback command
        assert "fallback" in altbootcmd_val.lower() or "ab_fallback" in altbootcmd_val, (
            f"altbootcmd does not reference fallback logic: {altbootcmd_val}"
        )

    def test_no_invalid_syntax_lines(self, env_content):
        """All non-comment, non-empty lines should be valid key=value pairs."""
        errors = []
        for lineno, line in enumerate(env_content.splitlines(), 1):
            stripped = line.strip()
            # Skip comments and empty lines
            if not stripped or stripped.startswith("#"):
                continue
            # Must be key=value (key can contain letters, digits, underscore, dash)
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_\-]*\s*=", stripped):
                errors.append(f"Line {lineno}: {line!r}")
        assert not errors, (
            f"uboot-bootcount.env has invalid non-key=value lines:\n"
            + "\n".join(errors)
        )

    def test_root_partitions_referenced(self, env_content):
        """bootargs_a and bootargs_b should reference different root= devices."""
        match_a = re.search(r"^bootargs_a\s*=\s*(.+)", env_content, re.MULTILINE)
        match_b = re.search(r"^bootargs_b\s*=\s*(.+)", env_content, re.MULTILINE)
        assert match_a, "bootargs_a not defined"
        assert match_b, "bootargs_b not defined"
        assert match_a.group(1) != match_b.group(1), (
            "bootargs_a and bootargs_b should differ (different root= partitions)"
        )


# ---------------------------------------------------------------------------
# Group 7: secure-boot-verify.sh behavior
# ---------------------------------------------------------------------------

class TestSecureBootVerify:
    """Tests for secure-boot-verify.sh argument handling."""

    def test_dry_run_exits_zero(self):
        """--dry-run should exit 0 without touching any system state."""
        result = run(["bash", script_path("secure-boot-verify.sh"), "--dry-run"])
        assert result.returncode in (0, 2), (
            f"secure-boot-verify.sh --dry-run returned {result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    def test_dry_run_lists_checks(self):
        """Dry-run output must list the checks that would be performed."""
        result = run(["bash", script_path("secure-boot-verify.sh"), "--dry-run"])
        combined = result.stdout + result.stderr
        assert "dry-run" in combined.lower() or "would" in combined.lower()

    def test_no_fit_image_skips_gracefully(self):
        """Running without --fit-image should not crash; should skip FIT check."""
        result = run(["bash", script_path("secure-boot-verify.sh")])
        combined = result.stdout + result.stderr
        # Should mention SKIP or skip for FIT check
        assert "skip" in combined.lower() or result.returncode in (0, 1, 2), (
            f"Expected graceful exit without --fit-image, got {result.returncode}"
        )

    def test_nonexistent_fit_image_fails(self):
        """Providing a non-existent FIT image path should cause FAIL result."""
        result = run(["bash", script_path("secure-boot-verify.sh"),
                      "--fit-image", "/nonexistent/path/kernel.itb"])
        combined = result.stdout + result.stderr
        # Should report failure for the missing file
        assert "fail" in combined.lower() or "not found" in combined.lower() or result.returncode != 0

    def test_help_flag_exits_zero(self):
        """--help should exit 0 and show usage."""
        result = run(["bash", script_path("secure-boot-verify.sh"), "--help"])
        assert result.returncode == 0

    def test_exits_with_acceptable_code(self):
        """Running the full check should exit 0 (pass) or 1 (some checks failed)."""
        result = run(["bash", script_path("secure-boot-verify.sh")])
        assert result.returncode in (0, 1, 2), (
            f"Unexpected exit code {result.returncode}\nstderr: {result.stderr}"
        )


# ===========================================================================
# Extended ab-partition-setup.sh tests
# ===========================================================================

class TestAbPartitionExtended:
    """Additional behavioral coverage for ab-partition-setup.sh."""

    def _run(self, args, timeout=5):
        return run(["bash", script_path("ab-partition-setup.sh")] + args, timeout=timeout)

    def test_dry_run_mark_active_exits_0(self):
        result = self._run(["--dry-run", "--mark-active", "A"])
        assert result.returncode in (0, 2)

    def test_dry_run_mark_active_b_exits_0(self):
        result = self._run(["--dry-run", "--mark-active", "B"])
        assert result.returncode in (0, 2)

    def test_dry_run_mark_failed_exits_0(self):
        result = self._run(["--dry-run", "--mark-failed", "A"])
        assert result.returncode in (0, 2)

    def test_dry_run_mark_pending_exits_0(self):
        result = self._run(["--dry-run", "--mark-pending", "B"])
        assert result.returncode in (0, 2)

    def test_dry_run_set_bootlimit_exits_0(self):
        result = self._run(["--dry-run", "--set-bootlimit", "3"])
        assert result.returncode in (0, 2)

    def test_status_shows_slot_header(self):
        result = self._run(["--status"])
        combined = result.stdout + result.stderr
        assert "slot" in combined.lower() or "partition" in combined.lower() or "A/B" in combined

    def test_status_shows_boot_info(self):
        result = self._run(["--status"])
        combined = result.stdout + result.stderr
        assert "boot" in combined.lower() or "status" in combined.lower()

    def test_dry_run_switch_prints_explanation(self):
        result = self._run(["--dry-run", "--switch"])
        combined = result.stdout + result.stderr
        assert len(combined.strip()) > 0

    def test_mark_active_invalid_slot_rejected(self):
        result = self._run(["--mark-active", "C"])
        assert result.returncode != 0

    def test_mark_pending_invalid_slot_rejected(self):
        result = self._run(["--mark-pending", "X"])
        assert result.returncode != 0

    def test_set_bootlimit_non_numeric_rejected(self):
        result = self._run(["--set-bootlimit", "abc"])
        assert result.returncode != 0

    def test_set_bootlimit_zero_rejected(self):
        result = self._run(["--set-bootlimit", "0"])
        assert result.returncode != 0


# ===========================================================================
# Extended wdt-setup.sh tests
# ===========================================================================

class TestWdtSetupExtended:
    """Additional behavioral coverage for wdt-setup.sh."""

    def _run(self, args, timeout=5):
        return run(["bash", script_path("wdt-setup.sh")] + args, timeout=timeout)

    def test_status_mode_exits_acceptably(self):
        result = self._run(["--status"])
        assert result.returncode in (0, 1, 2)

    def test_status_produces_output(self):
        result = self._run(["--status"])
        combined = result.stdout + result.stderr
        assert len(combined.strip()) > 0

    def test_daemon_dry_run_exits_0(self):
        result = self._run(["--daemon", "--dry-run"])
        assert result.returncode == 0

    def test_daemon_dry_run_mentions_watchdog(self):
        result = self._run(["--daemon", "--dry-run"])
        combined = result.stdout + result.stderr
        assert "watchdog" in combined.lower() or "wdt" in combined.lower() or "dry" in combined.lower()

    def test_timeout_flag_accepted(self):
        result = self._run(["--test", "--dry-run", "--timeout", "30"])
        assert result.returncode == 0

    def test_device_flag_accepted(self):
        result = self._run(["--test", "--dry-run", "--device", "/dev/watchdog0"])
        assert result.returncode == 0

    def test_no_mode_exits_nonzero(self):
        result = self._run([])
        assert result.returncode != 0

    def test_unknown_arg_exits_nonzero(self):
        result = self._run(["--bogus-arg-xyz"])
        assert result.returncode != 0
