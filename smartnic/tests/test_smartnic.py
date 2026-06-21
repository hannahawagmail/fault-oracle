# SPDX-License-Identifier: Apache-2.0
"""
test_smartnic.py — Tests for SmartNIC/DPU module scripts and configs

Run with:
    pytest smartnic/tests/test_smartnic.py -v

These tests validate that all scripts exist, have correct syntax, include
required boilerplate (SPDX, set -e, Usage:), and behave correctly when invoked
in --dry-run or status-check modes that do not require real DPU hardware.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Root of the smartnic/ module (two levels up from this file's directory)
SMARTNIC_DIR = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def run_script(script_path: Path, *args, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a shell script with bash and capture output."""
    cmd = ["bash", str(script_path)] + list(args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def find_shell_scripts() -> list[Path]:
    """Return all .sh files under SMARTNIC_DIR."""
    return sorted(SMARTNIC_DIR.rglob("*.sh"))


# ---------------------------------------------------------------------------
# Class TestScriptExists
# ---------------------------------------------------------------------------

class TestScriptExists:
    """Verify that all expected shell scripts are present on disk."""

    def test_dpu_setup_sh_exists(self):
        path = SMARTNIC_DIR / "control-plane" / "dpu-setup.sh"
        assert path.exists(), f"Missing: {path}"
        assert path.is_file(), f"Not a file: {path}"

    def test_ovs_offload_config_sh_exists(self):
        path = SMARTNIC_DIR / "offload" / "ovs-offload-config.sh"
        assert path.exists(), f"Missing: {path}"
        assert path.is_file(), f"Not a file: {path}"


# ---------------------------------------------------------------------------
# Class TestScriptSyntax
# ---------------------------------------------------------------------------

class TestScriptSyntax:
    """Validate shell script syntax and required boilerplate in all .sh files."""

    @pytest.fixture(params=[str(p) for p in find_shell_scripts()])
    def shell_script(self, request) -> Path:
        return Path(request.param)

    def test_bash_syntax_check(self, shell_script: Path):
        """bash -n exits 0 for syntactically valid scripts."""
        result = subprocess.run(
            ["bash", "-n", str(shell_script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"bash -n failed for {shell_script}:\n{result.stderr}"
        )

    def test_spdx_license_header(self, shell_script: Path):
        """Every .sh file must contain the Apache-2.0 SPDX identifier."""
        content = shell_script.read_text(encoding="utf-8")
        assert "SPDX-License-Identifier: Apache-2.0" in content, (
            f"Missing SPDX header in {shell_script}"
        )

    def test_set_e_or_pipefail(self, shell_script: Path):
        """Every .sh file must use set -e or set -euo pipefail for safety."""
        content = shell_script.read_text(encoding="utf-8")
        has_set_e = "set -e" in content
        has_pipefail = "set -euo pipefail" in content
        assert has_set_e or has_pipefail, (
            f"Missing 'set -e' or 'set -euo pipefail' in {shell_script}"
        )

    def test_usage_documentation(self, shell_script: Path):
        """Every .sh file must contain a Usage: section for self-documentation."""
        content = shell_script.read_text(encoding="utf-8")
        assert "Usage:" in content, (
            f"Missing 'Usage:' documentation in {shell_script}"
        )


# ---------------------------------------------------------------------------
# Class TestDPUSetup
# ---------------------------------------------------------------------------

class TestDPUSetup:
    """Behavioral tests for dpu-setup.sh sub-commands."""

    @pytest.fixture(autouse=True)
    def script(self) -> Path:
        self._script = SMARTNIC_DIR / "control-plane" / "dpu-setup.sh"
        assert self._script.exists(), f"Script not found: {self._script}"

    def test_detect_exits_gracefully(self):
        """--detect should exit 0 (success) or 1 (not on DPU); never 127 or crash."""
        result = run_script(self._script, "--detect")
        assert result.returncode in (0, 1, 2), (
            f"--detect exited with unexpected code {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        # Must never exit 127 (script/command not found)
        assert result.returncode != 127, "Script not found (exit 127)"

    def test_dry_run_install_exporter_exits_0(self):
        """--dry-run --install-exporter must exit 0 and print DRY-RUN lines."""
        result = run_script(self._script, "--dry-run", "--install-exporter")
        assert result.returncode == 0, (
            f"--dry-run --install-exporter failed (exit {result.returncode}).\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "DRY-RUN" in combined or "dry" in combined.lower(), (
            f"--dry-run did not produce DRY-RUN output:\n{combined}"
        )

    def test_configure_scrape_output(self):
        """--configure-scrape 192.168.1.100 must exit 0 and print valid YAML snippet."""
        result = run_script(self._script, "--configure-scrape", "192.168.1.100")
        assert result.returncode == 0, (
            f"--configure-scrape failed (exit {result.returncode}).\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        output = result.stdout
        assert "job_name" in output, f"Missing 'job_name' in output:\n{output}"
        assert "targets" in output, f"Missing 'targets' in output:\n{output}"
        assert "9200" in output, f"Missing port 9200 in output:\n{output}"

    def test_status_exits_gracefully(self):
        """--status must exit 0 or 1 (graceful when exporter is not installed)."""
        result = run_script(self._script, "--status")
        assert result.returncode in (0, 1, 2), (
            f"--status exited with unexpected code {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert result.returncode != 127, "Script not found (exit 127)"


# ---------------------------------------------------------------------------
# Class TestOVSOffload
# ---------------------------------------------------------------------------

class TestOVSOffload:
    """Behavioral tests for ovs-offload-config.sh sub-commands."""

    @pytest.fixture(autouse=True)
    def script(self) -> Path:
        self._script = SMARTNIC_DIR / "offload" / "ovs-offload-config.sh"
        assert self._script.exists(), f"Script not found: {self._script}"

    def test_status_exits_gracefully(self):
        """--status should exit 0 (ovs present), 1 (error), or 2 (ovs not installed)."""
        result = run_script(self._script, "--status")
        assert result.returncode in (0, 1, 2), (
            f"--status exited with unexpected code {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert result.returncode != 127, "Script not found (exit 127)"

    def test_dry_run_enable_offload_exits_gracefully(self):
        """--dry-run --enable-offload eth0 should exit 0 or 2 (no ovs-vsctl)."""
        result = run_script(self._script, "--dry-run", "--enable-offload", "eth0")
        assert result.returncode in (0, 2), (
            f"--dry-run --enable-offload exited with unexpected code {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# Class TestP4Program
# ---------------------------------------------------------------------------

class TestP4Program:
    """Static analysis tests for the P4_16 reference program."""

    @pytest.fixture(autouse=True)
    def p4_file(self) -> Path:
        self._p4 = SMARTNIC_DIR / "offload" / "p4-simple-forward.p4"

    def test_p4_file_exists(self):
        assert self._p4.exists(), f"Missing P4 file: {self._p4}"

    def _read(self) -> str:
        return self._p4.read_text(encoding="utf-8")

    def test_includes_core_p4(self):
        assert "#include <core.p4>" in self._read(), "Missing #include <core.p4>"

    def test_includes_v1model_p4(self):
        assert "#include <v1model.p4>" in self._read(), "Missing #include <v1model.p4>"

    def test_header_ethernet_t(self):
        assert "header ethernet_t" in self._read(), "Missing header ethernet_t"

    def test_header_ipv4_t(self):
        assert "header ipv4_t" in self._read(), "Missing header ipv4_t"

    def test_header_vxlan_t(self):
        assert "header vxlan_t" in self._read(), "Missing header vxlan_t"

    def test_table_l2_forward(self):
        assert "table l2_forward" in self._read(), "Missing table l2_forward"

    def test_table_vxlan_tunnel_table(self):
        assert "table vxlan_tunnel_table" in self._read(), "Missing table vxlan_tunnel_table"

    def test_table_acl_table(self):
        assert "table acl_table" in self._read(), "Missing table acl_table"

    def test_action_vxlan_decap(self):
        assert "action vxlan_decap" in self._read(), "Missing action vxlan_decap"

    def test_action_vxlan_encap(self):
        assert "action vxlan_encap" in self._read(), "Missing action vxlan_encap"

    def test_parser_my_parser(self):
        assert "parser MyParser" in self._read(), "Missing parser MyParser"

    def test_control_my_ingress(self):
        assert "control MyIngress" in self._read(), "Missing control MyIngress"

    def test_v1switch_instantiation(self):
        assert "V1Switch(" in self._read(), "Missing V1Switch( package instantiation"

    def test_balanced_braces(self):
        """The count of { must equal the count of } for syntactic correctness."""
        content = self._read()
        open_count = content.count("{")
        close_count = content.count("}")
        assert open_count == close_count, (
            f"Unbalanced braces in P4 file: {open_count} '{{' vs {close_count} '}}'"
        )


# ---------------------------------------------------------------------------
# Class TestDPUServiceFile
# ---------------------------------------------------------------------------

class TestDPUServiceFile:
    """Validate the systemd service unit file for the DPU exporter."""

    @pytest.fixture(autouse=True)
    def service_file(self) -> Path:
        self._svc = SMARTNIC_DIR / "control-plane" / "dpu-fault-exporter.service"

    def test_service_file_exists(self):
        assert self._svc.exists(), f"Missing service file: {self._svc}"

    def _read(self) -> str:
        return self._svc.read_text(encoding="utf-8")

    def test_contains_watchdog_sec(self):
        assert "WatchdogSec=" in self._read(), "Missing WatchdogSec= directive"

    def test_contains_type_notify(self):
        assert "Type=notify" in self._read(), "Missing Type=notify directive"

    def test_contains_oom_score_adjust(self):
        assert "OOMScoreAdjust=" in self._read(), "Missing OOMScoreAdjust= directive"

    def test_contains_listen_addr(self):
        assert "--listen-addr" in self._read(), "Missing --listen-addr flag in ExecStart"

    def test_contains_disable_aer(self):
        assert "--disable-aer" in self._read(), (
            "Missing --disable-aer flag. DPU does not use host PCIe AER path."
        )


# ---------------------------------------------------------------------------
# Class TestEDACIntegration
# ---------------------------------------------------------------------------

class TestEDACIntegration:
    """Validate the DPU EDAC integration documentation."""

    @pytest.fixture(autouse=True)
    def md_file(self) -> Path:
        self._md = SMARTNIC_DIR / "fault-resilience" / "dpu-edac-integration.md"

    def test_md_file_exists(self):
        assert self._md.exists(), f"Missing: {self._md}"

    def _read(self) -> str:
        return self._md.read_text(encoding="utf-8")

    def test_minimum_length(self):
        content = self._read()
        assert len(content) > 3000, (
            f"dpu-edac-integration.md is too short ({len(content)} chars); "
            "expected > 3000 chars"
        )

    def test_contains_lpddr5(self):
        assert "LPDDR5" in self._read(), "Missing 'LPDDR5' reference"

    def test_contains_ecc(self):
        assert "ECC" in self._read(), "Missing 'ECC' reference"

    def test_contains_dpu_high_ce_rate_alert(self):
        assert "DPUHighCERate" in self._read(), "Missing DPUHighCERate alert rule"

    def test_contains_dpu_mgmt_plane_down_alert(self):
        assert "DPUManagementPlaneDown" in self._read(), (
            "Missing DPUManagementPlaneDown alert rule"
        )

    def test_contains_role_dpu_label(self):
        content = self._read()
        has_relabel = "relabel_configs" in content
        has_role_label = 'role="dpu"' in content or "role: 'dpu'" in content or "role=dpu" in content
        assert has_relabel or has_role_label, (
            "Missing relabel_configs or role=dpu label in documentation"
        )
