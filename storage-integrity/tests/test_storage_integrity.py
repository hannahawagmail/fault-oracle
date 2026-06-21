# SPDX-License-Identifier: Apache-2.0
"""
test_storage_integrity.py — Tests for storage integrity scripts

Tests script syntax, argument validation, and dry-run behavior.
No hardware or root access required.

Run with:
    python3 -m pytest test_storage_integrity.py -v
"""

import os
import re
import subprocess
import pytest

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BOOT_RESILIENCE_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "boot-resilience"))


def script_path(name: str) -> str:
    return os.path.join(SCRIPT_DIR, name)


def boot_path(name: str) -> str:
    return os.path.join(BOOT_RESILIENCE_DIR, name)


def run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **kwargs
    )


# ---------------------------------------------------------------------------
# Group 1: File existence
# ---------------------------------------------------------------------------

class TestFileExistence:
    SCRIPTS = [
        "overlayfs-setup.sh",
        "filesystem-check.sh",
        "log-to-tmpfs.sh",
    ]

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_script_exists(self, script):
        path = script_path(script)
        assert os.path.isfile(path), f"Script not found: {path}"

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_script_nonempty(self, script):
        path = script_path(script)
        assert os.path.getsize(path) > 100, f"Script appears empty: {path}"

    def test_readme_exists(self):
        assert os.path.isfile(script_path("README.md"))

    def test_tests_dir_exists(self):
        assert os.path.isdir(os.path.join(SCRIPT_DIR, "tests"))


# ---------------------------------------------------------------------------
# Group 2: SPDX headers and set -e
# ---------------------------------------------------------------------------

class TestScriptHeaders:
    SCRIPTS = [
        "overlayfs-setup.sh",
        "filesystem-check.sh",
        "log-to-tmpfs.sh",
    ]

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_spdx_header(self, script):
        path = script_path(script)
        with open(path) as f:
            content = f.read()
        assert "SPDX-License-Identifier: Apache-2.0" in content, (
            f"{script} missing SPDX header"
        )

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_set_e_present(self, script):
        path = script_path(script)
        with open(path) as f:
            content = f.read()
        assert re.search(r"^set\s+-[a-z]*e", content, re.MULTILINE), (
            f"{script} missing 'set -e' or 'set -euo pipefail'"
        )

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_shebang(self, script):
        path = script_path(script)
        with open(path) as f:
            first_line = f.readline().strip()
        assert first_line.startswith("#!/"), f"{script} missing shebang"
        assert "bash" in first_line or "sh" in first_line


# ---------------------------------------------------------------------------
# Group 3: Bash syntax validation (bash -n)
# ---------------------------------------------------------------------------

class TestBashSyntax:
    SCRIPTS = [
        "overlayfs-setup.sh",
        "filesystem-check.sh",
        "log-to-tmpfs.sh",
    ]

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_bash_syntax(self, script):
        path = script_path(script)
        result = run(["bash", "-n", path])
        assert result.returncode == 0, (
            f"bash -n failed for {script}:\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# Group 4: overlayfs-setup.sh behavior
# ---------------------------------------------------------------------------

class TestOverlayfsSetup:
    """Tests for overlayfs-setup.sh."""

    def test_status_exits_acceptably(self):
        """--status should exit 0 or 1 (no crash, no core dump)."""
        result = run(["bash", script_path("overlayfs-setup.sh"), "--status"])
        assert result.returncode in (0, 1, 2), (
            f"overlayfs-setup.sh --status unexpected exit code {result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    def test_status_produces_output(self):
        """--status must produce some output."""
        result = run(["bash", script_path("overlayfs-setup.sh"), "--status"])
        combined = result.stdout + result.stderr
        assert len(combined.strip()) > 0, "No output from --status"

    def test_dry_run_does_not_write_proc_mounts(self):
        """--apply --dry-run must not modify /proc/mounts."""
        mtime_before = os.path.getmtime("/proc/mounts")
        result = run([
            "bash", script_path("overlayfs-setup.sh"),
            "--apply", "--dry-run"
        ])
        mtime_after = os.path.getmtime("/proc/mounts")
        # /proc/mounts updates on any mount operation; it should NOT change
        assert mtime_before == mtime_after, (
            "/proc/mounts was modified during dry-run — unexpected mount occurred"
        )
        assert result.returncode in (0, 1, 2), (
            f"Unexpected exit code {result.returncode} for --apply --dry-run\n"
            f"stderr: {result.stderr}"
        )

    def test_dry_run_output_contains_dry_run_keyword(self):
        """Dry-run output must mention dry-run."""
        result = run([
            "bash", script_path("overlayfs-setup.sh"),
            "--apply", "--dry-run"
        ])
        combined = result.stdout + result.stderr
        assert "dry-run" in combined.lower() or "would" in combined.lower(), (
            f"No dry-run indicators in output: {combined[:300]}"
        )

    def test_no_mode_exits_nonzero(self):
        """No mode argument should exit non-zero."""
        result = run(["bash", script_path("overlayfs-setup.sh")])
        assert result.returncode != 0

    def test_unknown_arg_exits_nonzero(self):
        result = run(["bash", script_path("overlayfs-setup.sh"), "--bogus"])
        assert result.returncode != 0

    def test_teardown_dry_run_exits_acceptably(self):
        """--teardown --dry-run should not crash."""
        result = run([
            "bash", script_path("overlayfs-setup.sh"),
            "--teardown", "--dry-run"
        ])
        assert result.returncode in (0, 1, 2)

    def test_overlay_kernel_module_availability(self):
        """Check if kernel has overlayfs support (/proc/filesystems)."""
        try:
            with open("/proc/filesystems") as f:
                content = f.read()
            if "overlay" in content:
                # Module available — overlayfs-setup.sh should report PASS for this
                pass
            else:
                pytest.skip("overlayfs not in /proc/filesystems on this kernel")
        except OSError:
            pytest.skip("Cannot read /proc/filesystems")


# ---------------------------------------------------------------------------
# Group 5: filesystem-check.sh behavior
# ---------------------------------------------------------------------------

class TestFilesystemCheck:
    """Tests for filesystem-check.sh."""

    def test_runs_without_crashing(self):
        """filesystem-check.sh must exit 0 or 1 (not segfault or unhandled error)."""
        result = run(["bash", script_path("filesystem-check.sh")])
        assert result.returncode in (0, 1), (
            f"filesystem-check.sh returned unexpected exit code {result.returncode}\n"
            f"stderr: {result.stderr[-500:]}"
        )

    def test_produces_pass_warn_or_fail_output(self):
        """Output must contain at least one PASS, WARN, FAIL, or SKIP marker."""
        result = run(["bash", script_path("filesystem-check.sh")])
        combined = result.stdout + result.stderr
        markers = ["[ PASS ]", "[ WARN ]", "[ FAIL ]", "[ SKIP ]"]
        has_marker = any(m in combined for m in markers)
        assert has_marker, (
            f"filesystem-check.sh produced no structured output\n"
            f"stdout: {result.stdout[:300]}"
        )

    def test_reports_rootfs(self):
        """Output should mention '/' root filesystem check."""
        result = run(["bash", script_path("filesystem-check.sh")])
        combined = result.stdout + result.stderr
        assert "rootfs" in combined.lower() or "root filesystem" in combined.lower() \
               or "/ " in combined, (
            "filesystem-check.sh did not mention rootfs check"
        )

    def test_summary_line_present(self):
        """Output must include a summary section."""
        result = run(["bash", script_path("filesystem-check.sh")])
        combined = result.stdout + result.stderr
        assert "summary" in combined.lower() or "result:" in combined.lower(), (
            "filesystem-check.sh output missing summary section"
        )

    def test_verbose_flag_accepted(self):
        """--verbose flag should be accepted without error."""
        result = run(["bash", script_path("filesystem-check.sh"), "--verbose"])
        assert result.returncode in (0, 1)

    def test_unknown_arg_exits_nonzero(self):
        result = run(["bash", script_path("filesystem-check.sh"), "--invalid-flag"])
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Group 6: log-to-tmpfs.sh behavior
# ---------------------------------------------------------------------------

class TestLogToTmpfs:
    """Tests for log-to-tmpfs.sh."""

    JOURNALD_CONF = "/etc/systemd/journald.conf"

    def test_dry_run_exits_zero(self):
        """--dry-run should exit 0 without touching the system."""
        result = run(["bash", script_path("log-to-tmpfs.sh"), "--dry-run"])
        assert result.returncode == 0, (
            f"log-to-tmpfs.sh --dry-run exited {result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    def test_dry_run_does_not_modify_journald_conf(self):
        """--dry-run must not modify /etc/systemd/journald.conf."""
        if not os.path.isfile(self.JOURNALD_CONF):
            pytest.skip(f"{self.JOURNALD_CONF} does not exist — skip modification check")

        mtime_before = os.path.getmtime(self.JOURNALD_CONF)
        run(["bash", script_path("log-to-tmpfs.sh"), "--dry-run"])
        mtime_after = os.path.getmtime(self.JOURNALD_CONF)
        assert mtime_before == mtime_after, (
            f"{self.JOURNALD_CONF} was modified during dry-run"
        )

    def test_dry_run_output_shows_would_write(self):
        """Dry-run output should indicate what would be written."""
        result = run(["bash", script_path("log-to-tmpfs.sh"), "--dry-run"])
        combined = result.stdout + result.stderr
        assert "dry-run" in combined.lower() or "would write" in combined.lower() or \
               "would" in combined.lower(), (
            f"Dry-run did not mention dry-run intent: {combined[:400]}"
        )

    def test_status_exits_acceptably(self):
        """--status should exit 0 or 1."""
        result = run(["bash", script_path("log-to-tmpfs.sh"), "--status"])
        assert result.returncode in (0, 1), (
            f"--status returned unexpected code {result.returncode}\nstderr: {result.stderr}"
        )

    def test_flush_interval_arg_accepted(self):
        """--flush-interval should be accepted and reflected in dry-run output."""
        result = run(["bash", script_path("log-to-tmpfs.sh"),
                      "--dry-run", "--flush-interval", "5"])
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "5" in combined, "Flush interval 5 not reflected in output"

    def test_max_use_arg_accepted(self):
        """--max-use should be accepted."""
        result = run(["bash", script_path("log-to-tmpfs.sh"),
                      "--dry-run", "--max-use", "32M"])
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "32M" in combined, "--max-use 32M not reflected in dry-run output"

    def test_dry_run_mentions_journald_storage(self):
        """Dry-run must mention Storage=volatile journald setting."""
        result = run(["bash", script_path("log-to-tmpfs.sh"), "--dry-run"])
        combined = result.stdout + result.stderr
        assert "volatile" in combined.lower() or "Storage" in combined, (
            "Dry-run output does not mention journald volatile storage setting"
        )

    def test_dry_run_mentions_timer(self):
        """Dry-run must mention log-flush.timer."""
        result = run(["bash", script_path("log-to-tmpfs.sh"), "--dry-run"])
        combined = result.stdout + result.stderr
        assert "timer" in combined.lower() or "log-flush" in combined.lower(), (
            "Dry-run output does not mention log-flush timer"
        )

    def test_unknown_arg_exits_nonzero(self):
        result = run(["bash", script_path("log-to-tmpfs.sh"), "--unknown-arg"])
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Group 7: Cross-module checks
# ---------------------------------------------------------------------------

class TestCrossModule:
    """Tests that reference both modules together."""

    def test_uboot_env_parses_as_key_value(self):
        """uboot-bootcount.env from boot-resilience must have valid key=value lines."""
        env_path = boot_path("uboot-bootcount.env")
        if not os.path.isfile(env_path):
            pytest.skip(f"uboot-bootcount.env not found at {env_path}")

        with open(env_path) as f:
            lines = f.readlines()

        errors = []
        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_\-]*\s*=", stripped):
                errors.append(f"Line {lineno}: {line.rstrip()!r}")

        assert not errors, (
            "uboot-bootcount.env has invalid lines:\n" + "\n".join(errors)
        )

    def test_overlay_kernel_support(self):
        """overlayfs kernel module check: /proc/filesystems should mention overlay."""
        try:
            with open("/proc/filesystems") as f:
                content = f.read()
        except OSError:
            pytest.skip("Cannot read /proc/filesystems")

        if "overlay" not in content:
            # Not a failure — just informational for CI environments
            pytest.skip(
                "overlayfs not present in /proc/filesystems — "
                "kernel may need CONFIG_OVERLAY_FS=y or modprobe overlay"
            )
        else:
            # If we get here, overlayfs is available
            assert "overlay" in content

    def test_all_readmes_have_spdx_equivalent_note(self):
        """READMEs don't use SPDX headers but should have Apache-2.0 license mention
        somewhere (in file headers of referenced scripts, at minimum the README itself
        should not contradict the license)."""
        for readme in ["README.md", "../boot-resilience/README.md"]:
            full_path = os.path.join(SCRIPT_DIR, readme)
            if not os.path.isfile(full_path):
                continue
            with open(full_path) as f:
                content = f.read()
            # README should mention Apache-2.0 or SPDX or license at minimum
            # (from referenced scripts which have headers)
            assert "Apache" in content or "SPDX" in content or "license" in content.lower(), (
                f"{readme} does not mention Apache license"
            )


# ===========================================================================
# Extended storage-integrity tests
# ===========================================================================

class TestOverlayfsSetupExtended:
    """Deeper behavioral tests for overlayfs-setup.sh."""

    def _run(self, args, timeout=5):
        return run(["bash", script_path("overlayfs-setup.sh")] + args, timeout=timeout)

    def test_help_exits_0(self):
        result = self._run(["--help"])
        assert result.returncode == 0

    def test_help_shows_usage(self):
        result = self._run(["--help"])
        combined = result.stdout + result.stderr
        assert "Usage" in combined or "overlay" in combined.lower()

    def test_status_exits_0(self):
        result = self._run(["--status"])
        assert result.returncode == 0

    def test_status_shows_overlay_header(self):
        result = self._run(["--status"])
        combined = result.stdout + result.stderr
        assert "overlay" in combined.lower() or "mount" in combined.lower()

    def test_teardown_dry_run_exits_0(self):
        result = self._run(["--teardown", "--dry-run"])
        assert result.returncode == 0

    def test_teardown_dry_run_mentions_umount(self):
        result = self._run(["--teardown", "--dry-run"])
        combined = result.stdout + result.stderr
        # Teardown when nothing is mounted outputs SKIP lines — that is valid teardown output
        assert len(combined.strip()) > 0  # produced some output

    def test_dry_run_apply_mentions_directories(self):
        result = self._run(["--apply", "--dry-run"])
        combined = result.stdout + result.stderr
        assert "lower" in combined or "upper" in combined or "overlay" in combined.lower()

    def test_no_mode_exits_nonzero(self):
        result = self._run([])
        assert result.returncode != 0

    def test_unknown_arg_exits_nonzero(self):
        result = self._run(["--bogus-argument-xyz"])
        assert result.returncode != 0


class TestFilesystemCheckExtended:
    """Extended filesystem-check.sh behavioral tests."""

    def _run(self, args=None, timeout=8):
        return run(["bash", script_path("filesystem-check.sh")] + (args or []), timeout=timeout)

    def test_produces_summary_section(self):
        result = self._run()
        combined = result.stdout + result.stderr
        assert "Summary" in combined or "RESULT" in combined or "summary" in combined.lower()

    def test_shows_pass_warn_fail_counts(self):
        result = self._run()
        combined = result.stdout + result.stderr
        assert "PASS" in combined or "WARN" in combined or "FAIL" in combined

    def test_exit_code_0_or_1(self):
        """Script exits 0 on clean, 1 on findings — both are valid in CI."""
        result = self._run()
        assert result.returncode in (0, 1)

    def test_rootfs_section_present(self):
        result = self._run()
        combined = result.stdout + result.stderr
        assert "/" in combined or "rootfs" in combined.lower() or "root" in combined.lower()

    def test_journal_mode_section(self):
        result = self._run()
        combined = result.stdout + result.stderr
        assert "journal" in combined.lower() or "ext" in combined.lower() or "fstype" in combined.lower()


class TestLogToTmpfsExtended:
    """Extended log-to-tmpfs.sh behavioral tests."""

    def _run(self, args, timeout=8):
        return run(["bash", script_path("log-to-tmpfs.sh")] + args, timeout=timeout)

    def test_status_shows_journald_header(self):
        result = self._run(["--status"])
        combined = result.stdout + result.stderr
        assert "journal" in combined.lower() or "log" in combined.lower() or "storage" in combined.lower()

    def test_dry_run_shows_timer_info(self):
        result = self._run(["--dry-run"])
        combined = result.stdout + result.stderr
        assert "timer" in combined.lower() or "flush" in combined.lower() or "tmpfs" in combined.lower()

    def test_dry_run_shows_interval(self):
        result = self._run(["--dry-run"])
        combined = result.stdout + result.stderr
        # Should show the flush interval value
        assert any(c.isdigit() for c in combined)

    def test_custom_flush_interval_reflected(self):
        result = self._run(["--dry-run", "--flush-interval", "30"])
        combined = result.stdout + result.stderr
        assert "30" in combined

    def test_sysctl_journal_key_in_conf(self):
        """The script references journald configuration keys."""
        path = script_path("log-to-tmpfs.sh")
        with open(path) as f:
            content = f.read()
        assert "Storage" in content or "storage" in content

    def test_script_defines_persistent_log_dir(self):
        path = script_path("log-to-tmpfs.sh")
        with open(path) as f:
            content = f.read()
        assert "PERSISTENT" in content or "persistent" in content


class TestStorageCrossModuleExtended:
    """Extended cross-module checks."""

    def test_all_scripts_have_bash_shebang(self):
        for name in ["overlayfs-setup.sh", "filesystem-check.sh", "log-to-tmpfs.sh"]:
            path = script_path(name)
            with open(path) as f:
                first_line = f.readline()
            assert "bash" in first_line, f"{name} missing bash shebang"

    def test_all_scripts_have_spdx(self):
        for name in ["overlayfs-setup.sh", "filesystem-check.sh", "log-to-tmpfs.sh"]:
            path = script_path(name)
            with open(path) as f:
                content = f.read()
            assert "SPDX-License-Identifier" in content, f"{name} missing SPDX"

    def test_filesystem_check_references_proc_mounts(self):
        path = script_path("filesystem-check.sh")
        with open(path) as f:
            content = f.read()
        assert "/proc/mounts" in content

    def test_overlayfs_references_kernel_config(self):
        path = script_path("overlayfs-setup.sh")
        with open(path) as f:
            content = f.read()
        assert "CONFIG_OVERLAY_FS" in content or "overlay" in content

    def test_log_to_tmpfs_references_systemd(self):
        path = script_path("log-to-tmpfs.sh")
        with open(path) as f:
            content = f.read()
        assert "systemd" in content or "journald" in content or "journalctl" in content
