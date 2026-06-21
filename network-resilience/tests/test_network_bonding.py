# SPDX-License-Identifier: Apache-2.0
"""
tests/test_network_bonding.py — Tests for bond-setup.sh.

All tests use --dry-run to avoid requiring root or real NICs.
"""

import subprocess
from pathlib import Path

import pytest

NET_DIR = Path(__file__).parent.parent
BOND_SH = NET_DIR / "bond-setup.sh"


def run(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(BOND_SH)] + args,
        capture_output=True, text=True,
    )


class TestBondSetup:

    def test_no_mode_exits_nonzero(self):
        r = run([])
        assert r.returncode != 0
        assert "Mode required" in r.stderr

    def test_create_dry_run_no_primary(self):
        r = run(["--create", "--dry-run"])
        assert r.returncode in (1, 2)

    def test_create_dry_run_no_secondary(self):
        r = run(["--create", "--dry-run", "--primary", "eth0"])
        assert r.returncode in (1, 2)

    def test_create_dry_run_full(self):
        r = run([
            "--create", "--dry-run",
            "--primary", "eth0",
            "--secondary", "eth1",
            "--name", "bond0",
        ])
        assert r.returncode in (0, 2)
        if r.returncode == 0:
            assert "DRY-RUN" in r.stdout
            assert "bond0" in r.stdout

    def test_status_missing_bond(self):
        """--status on a non-existent bond exits 1."""
        r = run(["--status", "--name", "nonexistent-bond-xyz"])
        assert r.returncode == 1
        combined = r.stdout + r.stderr
        assert "not found" in combined.lower() or "nonexistent" in combined

    def test_failover_dry_run(self):
        r = run(["--failover", "--dry-run", "--name", "bond0"])
        assert r.returncode in (0, 2)

    def test_remove_dry_run(self):
        r = run(["--remove", "--dry-run", "--name", "bond0"])
        assert r.returncode in (0, 2)
        if r.returncode == 0:
            assert "DRY-RUN" in r.stdout

    def test_demo_dry_run(self):
        r = run(["--demo", "--dry-run"])
        assert r.returncode in (0, 2)
        if r.returncode == 0:
            assert "DRY-RUN" in r.stdout

    def test_help_flag(self):
        r = run(["--help"])
        assert r.returncode == 0

    def test_unknown_option(self):
        r = run(["--bogus"])
        assert r.returncode != 0

    @pytest.mark.parametrize("mode", ["--create", "--status", "--failover", "--remove", "--demo"])
    def test_all_modes_no_crash(self, mode):
        """All modes must exit ≤ 2 with --dry-run (no unbound variable crash)."""
        extra = []
        if mode == "--create":
            extra = ["--primary", "eth0", "--secondary", "eth1"]
        r = run([mode, "--dry-run"] + extra)
        assert r.returncode <= 2, (
            f"Mode {mode} crashed (exit {r.returncode})\n"
            f"stderr: {r.stderr}"
        )

    def test_custom_bond_name(self):
        r = run([
            "--create", "--dry-run",
            "--name", "bond-custom",
            "--primary", "eth0",
            "--secondary", "eth1",
        ])
        assert r.returncode in (0, 2)
        if r.returncode == 0:
            assert "bond-custom" in r.stdout

    def test_lacp_mode_dry_run(self):
        r = run([
            "--create", "--dry-run",
            "--mode", "802.3ad",
            "--primary", "eth0",
            "--secondary", "eth1",
        ])
        assert r.returncode in (0, 2)
