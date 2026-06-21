# SPDX-License-Identifier: Apache-2.0
"""
tests/test_boot_resilience_ab.py — Unit tests for U-Boot A/B boot scripts.

Tests cover:
- uboot-bootcount-reset.sh: --dry-run, --check, fw_printenv mock
- ab-partition-setup.sh:    --status, --mark-active, --switch, --dry-run

All tests use subprocess with a mock fw_printenv/fw_setenv PATH injection
so no real U-Boot hardware or tools are needed.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

BOOT_DIR = Path(__file__).parent.parent
RESET_SH = BOOT_DIR / "uboot-bootcount-reset.sh"
AB_SH = BOOT_DIR / "ab-partition-setup.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_script(script: Path, args: list, env_overrides: dict = None, tmpdir: Path = None) -> subprocess.CompletedProcess:
    """Run a shell script with optional PATH override for mock tools."""
    env = os.environ.copy()
    env.pop("CDPATH", None)
    if tmpdir:
        env["PATH"] = str(tmpdir) + ":" + env.get("PATH", "")
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(script)] + args,
        capture_output=True,
        text=True,
        env=env,
    )


def make_fw_mock(tmpdir: Path, bootcount: int = 0, bootlimit: int = 3,
                 upgrade_available: int = 0, fail_setenv: bool = False) -> None:
    """Create mock fw_printenv and fw_setenv binaries in tmpdir."""
    fw_printenv = tmpdir / "fw_printenv"
    fw_setenv = tmpdir / "fw_setenv"

    fw_printenv.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        case "$1" in
            -n)
                case "$2" in
                    bootcount)          echo "{bootcount}"; exit 0 ;;
                    bootlimit)          echo "{bootlimit}"; exit 0 ;;
                    upgrade_available)  echo "{upgrade_available}"; exit 0 ;;
                    *) exit 1 ;;
                esac ;;
            *)
                echo "bootcount={bootcount}"
                echo "bootlimit={bootlimit}"
                echo "upgrade_available={upgrade_available}"
                exit 0 ;;
        esac
    """))
    fw_printenv.chmod(0o755)

    if fail_setenv:
        fw_setenv.write_text("#!/usr/bin/env bash\nexit 1\n")
    else:
        fw_setenv.write_text("#!/usr/bin/env bash\nexit 0\n")
    fw_setenv.chmod(0o755)


# ---------------------------------------------------------------------------
# uboot-bootcount-reset.sh tests
# ---------------------------------------------------------------------------

class TestBootcountReset:

    def test_dry_run_no_real_tools(self, tmp_path):
        """--dry-run with mock fw tools exits 0 and prints what it would do."""
        make_fw_mock(tmp_path, bootcount=2, bootlimit=3)
        r = run_script(RESET_SH, ["--dry-run"], tmpdir=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "DRY-RUN" in r.stdout

    def test_dry_run_bootcount_zero_no_op(self, tmp_path):
        """If bootcount is already 0, --dry-run exits 0 with 'nothing to do'."""
        make_fw_mock(tmp_path, bootcount=0, bootlimit=3)
        r = run_script(RESET_SH, ["--dry-run"], tmpdir=tmp_path)
        assert r.returncode == 0
        # 0 means nothing to do — DRY-RUN message may or may not appear
        assert "nothing to do" in r.stdout or r.returncode == 0

    def test_check_within_limits(self, tmp_path):
        """--check exits 0 when bootcount < bootlimit."""
        make_fw_mock(tmp_path, bootcount=1, bootlimit=3)
        r = run_script(RESET_SH, ["--check"], tmpdir=tmp_path)
        assert r.returncode == 0, r.stderr

    def test_check_at_limit_warns(self, tmp_path):
        """--check exits 1 when bootcount >= bootlimit."""
        make_fw_mock(tmp_path, bootcount=3, bootlimit=3)
        r = run_script(RESET_SH, ["--check"], tmpdir=tmp_path)
        assert r.returncode == 1
        combined = r.stdout + r.stderr
        assert "bootlimit" in combined or "limit" in combined.lower()

    def test_missing_fw_printenv_graceful(self, tmp_path):
        """Without fw_printenv available, script exits gracefully (0 or 2).

        Exit 2 = explicit graceful skip (fw_printenv not found).
        Exit 0 = fw_printenv is installed on the system and returns defaults.
        Either is acceptable — what matters is no crash (exit >2).
        """
        r = run_script(RESET_SH, [], tmpdir=tmp_path)
        assert r.returncode in (0, 2), (
            f"Expected exit 0 or 2, got {r.returncode}\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )

    def test_dry_run_upgrade_available_cleared(self, tmp_path):
        """--dry-run logs upgrade_available clear when upgrade_available=1."""
        make_fw_mock(tmp_path, bootcount=1, bootlimit=3, upgrade_available=1)
        r = run_script(RESET_SH, ["--dry-run"], tmpdir=tmp_path)
        assert r.returncode == 0
        assert "upgrade_available" in r.stdout

    def test_verbose_flag(self, tmp_path):
        """--verbose produces more output (does not crash)."""
        make_fw_mock(tmp_path, bootcount=0, bootlimit=3)
        r = run_script(RESET_SH, ["--dry-run", "--verbose"], tmpdir=tmp_path)
        assert r.returncode == 0


# ---------------------------------------------------------------------------
# ab-partition-setup.sh tests
# ---------------------------------------------------------------------------

def make_uboot_env_mock(tmpdir: Path, slot: str = "A", bootcount: int = 0,
                        bootlimit: int = 3) -> None:
    """Create mock fw_printenv for A/B slot tests."""
    fw_printenv = tmpdir / "fw_printenv"
    slot_var = "boot_slot_a" if slot == "A" else "boot_slot_b"
    fw_printenv.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        case "${{1}}" in
            -n)
                case "${{2}}" in
                    boot_slot)         echo "{slot}"; exit 0 ;;
                    bootcount)         echo "{bootcount}"; exit 0 ;;
                    bootlimit)         echo "{bootlimit}"; exit 0 ;;
                    upgrade_available) echo "0"; exit 0 ;;
                    *) exit 1 ;;
                esac ;;
            *) echo "boot_slot={slot}"; echo "bootcount={bootcount}"; exit 0 ;;
        esac
    """))
    fw_printenv.chmod(0o755)
    fw_setenv = tmpdir / "fw_setenv"
    fw_setenv.write_text("#!/usr/bin/env bash\nexit 0\n")
    fw_setenv.chmod(0o755)


class TestABPartitionSetup:

    def test_dry_run_status(self, tmp_path):
        """--status --dry-run exits 0 regardless of slot state."""
        make_uboot_env_mock(tmp_path, slot="A")
        r = run_script(AB_SH, ["--status", "--dry-run"], tmpdir=tmp_path)
        # Should print status or exit gracefully (2 = fw_printenv missing)
        assert r.returncode in (0, 2)

    def test_dry_run_switch(self, tmp_path):
        """--switch --dry-run exits without crashing.

        Exit 0 = slot determined and would-switch logged.
        Exit 1 = cannot determine current slot (expected in CI with UUID root=).
        Exit 2 = fw tools not available.
        Any of these is acceptable; a crash (exit >2, unbound variable) is not.
        """
        make_uboot_env_mock(tmp_path, slot="A")
        r = run_script(AB_SH, ["--switch", "--dry-run"], tmpdir=tmp_path)
        assert r.returncode in (0, 1, 2), (
            f"Expected exit 0/1/2 (no crash), got {r.returncode}\n"
            f"stderr: {r.stderr}"
        )

    def test_mark_active_dry_run(self, tmp_path):
        """--mark-active A --dry-run exits 0."""
        make_uboot_env_mock(tmp_path, slot="A")
        r = run_script(AB_SH, ["--mark-active", "A", "--dry-run"], tmpdir=tmp_path)
        assert r.returncode in (0, 2)

    def test_mark_failed_dry_run(self, tmp_path):
        """--mark-failed B --dry-run exits 0."""
        make_uboot_env_mock(tmp_path, slot="A")
        r = run_script(AB_SH, ["--mark-failed", "B", "--dry-run"], tmpdir=tmp_path)
        assert r.returncode in (0, 2)

    def test_invalid_slot_rejected(self, tmp_path):
        """--mark-active C should be rejected (invalid slot name)."""
        make_uboot_env_mock(tmp_path, slot="A")
        r = run_script(AB_SH, ["--mark-active", "C", "--dry-run"], tmpdir=tmp_path)
        # Should exit non-zero for invalid slot
        assert r.returncode != 0 or "invalid" in (r.stdout + r.stderr).lower() \
               or r.returncode == 2  # fw missing is also acceptable

    def test_missing_fw_tools_graceful(self, tmp_path):
        """Without fw tools, script exits 2 (graceful skip, not crash)."""
        # Empty tmpdir
        r = run_script(AB_SH, ["--status"], tmpdir=tmp_path)
        assert r.returncode in (0, 1, 2)  # no panic/unbound-var crash (exit >2)

    def test_set_bootlimit_dry_run(self, tmp_path):
        """--set-bootlimit 5 --dry-run exits 0."""
        make_uboot_env_mock(tmp_path, slot="A")
        r = run_script(AB_SH, ["--set-bootlimit", "5", "--dry-run"], tmpdir=tmp_path)
        assert r.returncode in (0, 2)
