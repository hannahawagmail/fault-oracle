# SPDX-License-Identifier: Apache-2.0
"""
tests/test_rauc_ota.py — Tests for RAUC OTA bundle scripts.

Uses a mock `rauc` CLI injected via PATH so no real RAUC installation
is needed in CI.
"""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

OTA_DIR = Path(__file__).parent.parent
CREATE_SH = OTA_DIR / "rauc-bundle-create.sh"
DRY_SH = OTA_DIR / "rauc-install-dry.sh"


def run(script: Path, args: list, tmpdir: Path = None,
        env_overrides: dict = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("CDPATH", None)
    if tmpdir:
        env["PATH"] = str(tmpdir) + ":" + env.get("PATH", "")
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(script)] + args,
        capture_output=True, text=True, env=env,
    )


def make_mock_rauc(tmpdir: Path, info_output: str = "Compatible: test-board\nVersion: 1.0",
                   fail_bundle: bool = False, fail_info: bool = False,
                   version: str = "1.8.0") -> None:
    """Create a mock rauc binary in tmpdir."""
    rauc = tmpdir / "rauc"
    exit_code = "1" if fail_info else "0"
    bundle_exit = "1" if fail_bundle else "0"
    rauc.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        case "$1" in
            --version) echo "rauc {version}"; exit 0 ;;
            info)      echo "{info_output}"; exit {exit_code} ;;
            bundle)    exit {bundle_exit} ;;
            check-bundle) exit 0 ;;
            install)   exit 0 ;;
            *) exit 0 ;;
        esac
    """))
    rauc.chmod(0o755)


# ---------------------------------------------------------------------------
# rauc-bundle-create.sh tests
# ---------------------------------------------------------------------------

class TestRaucBundleCreate:

    def test_missing_args_exits_1(self, tmp_path):
        make_mock_rauc(tmp_path)
        r = run(CREATE_SH, [], tmpdir=tmp_path)
        assert r.returncode in (1, 2)

    def test_no_rauc_exits_2(self, tmp_path):
        # Empty tmp_path — no mock rauc
        r = run(CREATE_SH, [
            "--rootfs", "/dev/null",
            "--output", str(tmp_path / "out.raucb"),
            "--keyfile", "/dev/null",
            "--certfile", "/dev/null",
        ], tmpdir=tmp_path)
        assert r.returncode == 2

    def test_dry_run_missing_rootfs(self, tmp_path):
        make_mock_rauc(tmp_path)
        r = run(CREATE_SH, [
            "--dry-run",
            "--rootfs", "/nonexistent/rootfs.tar.gz",
            "--output", str(tmp_path / "out.raucb"),
            "--keyfile", str(tmp_path / "key.pem"),
            "--certfile", str(tmp_path / "cert.pem"),
        ], tmpdir=tmp_path)
        # Rootfs not found → exit 1
        assert r.returncode == 1

    def test_dry_run_full_success(self, tmp_path):
        make_mock_rauc(tmp_path)
        # Create dummy files
        rootfs = tmp_path / "rootfs.tar.gz"
        rootfs.write_bytes(b"fake rootfs")
        keyfile = tmp_path / "signing.key"
        keyfile.write_text("FAKE KEY")
        certfile = tmp_path / "signing.crt"
        certfile.write_text("FAKE CERT")
        output = tmp_path / "update.raucb"

        r = run(CREATE_SH, [
            "--dry-run",
            "--rootfs",   str(rootfs),
            "--output",   str(output),
            "--keyfile",  str(keyfile),
            "--certfile", str(certfile),
            "--version",  "1.2.3",
            "--slot",     "rootfs.0",
        ], tmpdir=tmp_path)

        assert r.returncode == 0
        assert "DRY-RUN" in r.stdout
        assert "1.2.3" in r.stdout or "rootfs.0" in r.stdout

    def test_help_flag(self, tmp_path):
        make_mock_rauc(tmp_path)
        r = run(CREATE_SH, ["--help"], tmpdir=tmp_path)
        assert r.returncode == 0

    def test_unknown_option(self, tmp_path):
        make_mock_rauc(tmp_path)
        r = run(CREATE_SH, ["--unknown"], tmpdir=tmp_path)
        assert r.returncode != 0


# ---------------------------------------------------------------------------
# rauc-install-dry.sh tests
# ---------------------------------------------------------------------------

class TestRaucInstallDry:

    def test_no_bundle_exits_nonzero(self, tmp_path):
        make_mock_rauc(tmp_path)
        r = run(DRY_SH, [], tmpdir=tmp_path)
        assert r.returncode != 0

    def test_bundle_not_found_exits_2(self, tmp_path):
        make_mock_rauc(tmp_path)
        r = run(DRY_SH, ["--bundle", "/nonexistent/update.raucb"], tmpdir=tmp_path)
        assert r.returncode == 2

    def test_no_rauc_exits_2(self, tmp_path):
        # No mock rauc in empty tmpdir
        fake_bundle = tmp_path / "update.raucb"
        fake_bundle.write_bytes(b"fake bundle")
        r = run(DRY_SH, ["--bundle", str(fake_bundle)], tmpdir=tmp_path)
        assert r.returncode == 2

    def test_valid_bundle_passes(self, tmp_path):
        make_mock_rauc(tmp_path)
        fake_bundle = tmp_path / "update.raucb"
        fake_bundle.write_bytes(b"fake bundle")
        r = run(DRY_SH, ["--bundle", str(fake_bundle)], tmpdir=tmp_path)
        assert r.returncode == 0
        combined = r.stdout + r.stderr
        assert "PASSED" in combined or "valid" in combined.lower()

    def test_failing_rauc_info_returns_nonzero(self, tmp_path):
        make_mock_rauc(tmp_path, fail_info=True)
        fake_bundle = tmp_path / "update.raucb"
        fake_bundle.write_bytes(b"fake bundle")
        r = run(DRY_SH, ["--bundle", str(fake_bundle)], tmpdir=tmp_path)
        assert r.returncode != 0

    def test_help_flag(self, tmp_path):
        make_mock_rauc(tmp_path)
        r = run(DRY_SH, ["--help"], tmpdir=tmp_path)
        assert r.returncode == 0

    def test_positional_bundle_arg(self, tmp_path):
        """Accept bundle path as positional argument (file.raucb)."""
        make_mock_rauc(tmp_path)
        fake_bundle = tmp_path / "update.raucb"
        fake_bundle.write_bytes(b"fake bundle")
        r = run(DRY_SH, [str(fake_bundle)], tmpdir=tmp_path)
        assert r.returncode == 0
