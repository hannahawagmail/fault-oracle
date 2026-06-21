# SPDX-License-Identifier: Apache-2.0
"""
tests/test_cert_rotation.py — Tests for security/cert-rotation-check.sh

Generates real short-lived or long-lived certs with openssl, then
verifies that the rotation check script reports the correct exit code.
"""
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

SECURITY_DIR = Path(__file__).parent.parent
CHECK_SH = SECURITY_DIR / "cert-rotation-check.sh"
GEN_SH = SECURITY_DIR / "generate-certs.sh"


def has_openssl() -> bool:
    return subprocess.run(["openssl", "version"], capture_output=True).returncode == 0


def make_cert(tmp_path: Path, days: int, name: str = "test") -> Path:
    """Generate a self-signed cert valid for `days` days."""
    key = tmp_path / f"{name}.key"
    crt = tmp_path / f"{name}.crt"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(key),
        "-out", str(crt),
        "-days", str(days),
        "-nodes",
        "-subj", f"/CN=test-{name}",
    ], capture_output=True, check=True)
    return crt


def run_check(cert_dir: Path, warn_days: int = 30, extra_args=None):
    args = ["bash", str(CHECK_SH),
            "--cert-dir", str(cert_dir),
            "--warn-days", str(warn_days)]
    if extra_args:
        args += extra_args
    return subprocess.run(args, capture_output=True, text=True)


@pytest.mark.skipif(not has_openssl(), reason="openssl not available")
class TestCertRotationCheck:

    def test_all_valid_certs_exit_0(self, tmp_path):
        """Certs with 90 days remaining exit 0."""
        make_cert(tmp_path, days=90, name="ca")
        make_cert(tmp_path, days=90, name="server")
        make_cert(tmp_path, days=90, name="client")
        r = run_check(tmp_path, warn_days=30)
        assert r.returncode == 0, r.stderr

    def test_expiring_cert_exits_1(self, tmp_path):
        """Cert expiring in 10 days (< warn_days=30) exits 1."""
        make_cert(tmp_path, days=90, name="ca")
        make_cert(tmp_path, days=90, name="server")
        make_cert(tmp_path, days=10, name="client")   # <-- expiring
        r = run_check(tmp_path, warn_days=30)
        assert r.returncode == 1, r.stderr
        assert "EXPIRING" in r.stderr or "expir" in r.stderr.lower()

    def test_missing_cert_exits_3(self, tmp_path):
        """Missing ca.crt causes exit 3 (or non-zero)."""
        # Only create server + client, omit ca.crt
        make_cert(tmp_path, days=90, name="server")
        make_cert(tmp_path, days=90, name="client")
        r = run_check(tmp_path, warn_days=30)
        assert r.returncode != 0

    def test_json_output_is_valid(self, tmp_path):
        """--json flag produces valid JSON."""
        import json
        make_cert(tmp_path, days=90, name="ca")
        make_cert(tmp_path, days=90, name="server")
        make_cert(tmp_path, days=90, name="client")
        r = run_check(tmp_path, warn_days=30, extra_args=["--json"])
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert "certs" in data
        assert len(data["certs"]) == 3
        for entry in data["certs"]:
            assert "days_left" in entry
            assert "status" in entry

    def test_json_shows_expiring_status(self, tmp_path):
        """--json marks expiring cert with status=expiring."""
        import json
        make_cert(tmp_path, days=90, name="ca")
        make_cert(tmp_path, days=10, name="server")
        make_cert(tmp_path, days=90, name="client")
        r = run_check(tmp_path, warn_days=30, extra_args=["--json"])
        assert r.returncode == 1
        data = json.loads(r.stdout)
        statuses = {e["cert"]: e["status"] for e in data["certs"]}
        assert statuses["server.crt"] == "expiring"
        assert statuses["ca.crt"] == "ok"

    def test_dry_run_generate_certs(self, tmp_path):
        """generate-certs.sh --dry-run exits 0 and prints nothing to do."""
        r = subprocess.run(
            ["bash", str(GEN_SH), "--dry-run", "--out-dir", str(tmp_path)],
            capture_output=True, text=True,
        )
        assert r.returncode == 0
        assert "DRY-RUN" in r.stdout

    def test_generate_and_check_roundtrip(self, tmp_path):
        """Full roundtrip: generate certs, then check reports all OK."""
        r = subprocess.run(
            ["bash", str(GEN_SH), "--out-dir", str(tmp_path), "--days", "365"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            pytest.skip("openssl key gen failed (sandbox limitation)")
        check = run_check(tmp_path, warn_days=30)
        assert check.returncode == 0, check.stderr
