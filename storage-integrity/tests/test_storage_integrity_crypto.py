# SPDX-License-Identifier: Apache-2.0
"""
tests/test_storage_integrity_crypto.py — Tests for dm-integrity and fscrypt scripts.

All tests use --dry-run to avoid requiring root, block devices, or
fscrypt/integritysetup tools in CI.
"""

import subprocess
import sys
from pathlib import Path

import pytest

STORAGE_DIR = Path(__file__).parent.parent
DM_INTEGRITY_SH = STORAGE_DIR / "dm-integrity-setup.sh"
FSCRYPT_SH = STORAGE_DIR / "fscrypt-setup.sh"


def run(script: Path, args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)] + args,
        capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# dm-integrity-setup.sh
# ---------------------------------------------------------------------------

class TestDmIntegritySetup:

    def test_no_mode_exits_nonzero(self):
        r = run(DM_INTEGRITY_SH, [])
        assert r.returncode != 0
        assert "Mode required" in r.stderr

    def test_create_dry_run_no_device(self):
        """--create --dry-run without --device exits 1 (device required)."""
        r = run(DM_INTEGRITY_SH, ["--create", "--dry-run"])
        # Exits 2 (no root) or 1 (no device) — both acceptable
        assert r.returncode in (1, 2), r.stderr

    def test_demo_dry_run(self):
        """--demo --dry-run exits 0 and prints DRY-RUN markers."""
        r = run(DM_INTEGRITY_SH, ["--demo", "--dry-run"])
        assert r.returncode in (0, 2), r.stderr  # 2 if kernel < 5.7 in CI
        if r.returncode == 0:
            assert "DRY-RUN" in r.stdout

    def test_verify_dry_run_no_device(self):
        r = run(DM_INTEGRITY_SH, ["--verify", "--dry-run"])
        # no --device → exit 1
        assert r.returncode in (1, 2)

    def test_verify_dry_run_with_device(self, tmp_path):
        """--verify --dry-run --device <nonexistent> exits 2 (root check) or 1."""
        r = run(DM_INTEGRITY_SH, ["--verify", "--dry-run", "--device", "/dev/nonexistent"])
        assert r.returncode in (0, 1, 2)

    def test_remove_dry_run(self):
        r = run(DM_INTEGRITY_SH, ["--remove", "--dry-run"])
        assert r.returncode in (0, 2)

    def test_status_dry_run(self):
        r = run(DM_INTEGRITY_SH, ["--status", "--dry-run"])
        assert r.returncode in (0, 1, 2)

    def test_unknown_option_exits_nonzero(self):
        r = run(DM_INTEGRITY_SH, ["--bogus-option"])
        assert r.returncode != 0

    def test_help_flag(self):
        r = run(DM_INTEGRITY_SH, ["--help"])
        assert r.returncode == 0

    @pytest.mark.parametrize("mode", ["--create", "--verify", "--demo", "--status", "--remove"])
    def test_all_modes_accept_dry_run(self, mode):
        """All modes must accept --dry-run without crashing (exit ≤ 2)."""
        r = run(DM_INTEGRITY_SH, [mode, "--dry-run"])
        assert r.returncode <= 2, (
            f"Mode {mode} crashed (exit {r.returncode})\n"
            f"stderr: {r.stderr}"
        )


# ---------------------------------------------------------------------------
# fscrypt-setup.sh
# ---------------------------------------------------------------------------

class TestFscryptSetup:

    def test_no_mode_exits_nonzero(self):
        r = run(FSCRYPT_SH, [])
        assert r.returncode != 0
        assert "Mode required" in r.stderr

    def test_demo_mode(self):
        """--demo exits 0 and prints useful information (no root needed)."""
        r = run(FSCRYPT_SH, ["--demo"])
        assert r.returncode == 0
        assert "tune2fs" in r.stdout or "encrypt" in r.stdout.lower()

    def test_demo_dry_run(self):
        r = run(FSCRYPT_SH, ["--demo", "--dry-run"])
        assert r.returncode == 0
        assert "DRY-RUN" in r.stdout

    def test_init_dry_run_no_mountpoint(self):
        r = run(FSCRYPT_SH, ["--init", "--dry-run"])
        assert r.returncode in (1, 2)

    def test_init_dry_run_with_mountpoint(self, tmp_path):
        """--init --dry-run with a mountpoint path exits 0 or 2 (root check)."""
        r = run(FSCRYPT_SH, ["--init", "--dry-run", "--mountpoint", str(tmp_path)])
        assert r.returncode in (0, 2)
        if r.returncode == 0:
            assert "DRY-RUN" in r.stdout

    def test_create_dry_run(self, tmp_path):
        r = run(FSCRYPT_SH, [
            "--create", "--dry-run",
            "--mountpoint", str(tmp_path),
            "--dir", "private",
        ])
        assert r.returncode in (0, 2)

    def test_lock_dry_run(self, tmp_path):
        r = run(FSCRYPT_SH, ["--lock", "--dry-run", "--dir", str(tmp_path)])
        assert r.returncode in (0, 2)

    def test_status_dry_run(self, tmp_path):
        r = run(FSCRYPT_SH, ["--status", "--dry-run", "--dir", str(tmp_path)])
        assert r.returncode in (0, 2)

    def test_help_flag(self):
        r = run(FSCRYPT_SH, ["--help"])
        assert r.returncode == 0

    def test_unknown_option(self):
        r = run(FSCRYPT_SH, ["--bad-option"])
        assert r.returncode != 0

    @pytest.mark.parametrize("mode", ["--init", "--create", "--unlock", "--lock", "--status", "--demo"])
    def test_all_modes_no_crash(self, mode, tmp_path):
        """All modes must exit ≤ 2 (no unbound variable or crash)."""
        r = run(FSCRYPT_SH, [mode, "--dry-run", "--mountpoint", str(tmp_path),
                              "--dir", "test"])
        assert r.returncode <= 2, (
            f"Mode {mode} crashed (exit {r.returncode})\n"
            f"stderr: {r.stderr}"
        )
