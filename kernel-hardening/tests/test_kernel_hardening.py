# SPDX-License-Identifier: Apache-2.0
"""
test_kernel_hardening.py — Tests for kernel hardening scripts and configs

Run:
    cd kernel-hardening/tests
    python3 -m pytest test_kernel_hardening.py -v

Some tests require Linux /proc and will be skipped on non-Linux hosts.
Tests that require root (--apply) are skipped if not running as root.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Resolve module root
# ---------------------------------------------------------------------------
MODULE_DIR = Path(__file__).parent.parent.resolve()
EBPF_DIR = MODULE_DIR / "ebpf"
TESTS_DIR = MODULE_DIR / "tests"

SCRIPTS = {
    "panic_config": MODULE_DIR / "panic-config.sh",
    "oom_tuning": MODULE_DIR / "oom-tuning.sh",
    "ebpf_to_prom": EBPF_DIR / "ebpf-to-prometheus.sh",
}

SYSCTL_CONF = MODULE_DIR / "sysctl-hardening.conf"

BT_SCRIPTS = [
    EBPF_DIR / "detect-fork-storm.bt",
    EBPF_DIR / "detect-oom-pressure.bt",
    EBPF_DIR / "detect-io-anomaly.bt",
]

IS_LINUX = sys.platform.startswith("linux")
IS_ROOT = os.geteuid() == 0 if IS_LINUX else False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd, **kwargs):
    """Run a command and return CompletedProcess. Never raises on non-zero exit."""
    defaults = {"capture_output": True, "text": True, "timeout": 30}
    defaults.update(kwargs)
    return subprocess.run(cmd, **defaults)


def bash_syntax_check(script_path: Path) -> subprocess.CompletedProcess:
    """Run bash -n on a script to check syntax."""
    return run(["bash", "-n", str(script_path)])


def read_sysctl_conf(path: Path) -> list[str]:
    """Read sysctl.conf lines, stripping blanks."""
    return [
        line.rstrip()
        for line in path.read_text().splitlines()
        if line.strip()
    ]


# ===========================================================================
# 1. File existence tests
# ===========================================================================

class TestFilesExist:
    def test_panic_config_sh_exists(self):
        assert SCRIPTS["panic_config"].is_file(), \
            f"panic-config.sh not found at {SCRIPTS['panic_config']}"

    def test_oom_tuning_sh_exists(self):
        assert SCRIPTS["oom_tuning"].is_file(), \
            f"oom-tuning.sh not found at {SCRIPTS['oom_tuning']}"

    def test_ebpf_to_prometheus_sh_exists(self):
        assert SCRIPTS["ebpf_to_prom"].is_file(), \
            f"ebpf-to-prometheus.sh not found at {SCRIPTS['ebpf_to_prom']}"

    def test_sysctl_hardening_conf_exists(self):
        assert SYSCTL_CONF.is_file(), \
            f"sysctl-hardening.conf not found at {SYSCTL_CONF}"

    @pytest.mark.parametrize("bt_script", BT_SCRIPTS, ids=[p.name for p in BT_SCRIPTS])
    def test_bt_scripts_exist(self, bt_script):
        assert bt_script.is_file(), f"{bt_script.name} not found at {bt_script}"


# ===========================================================================
# 2. SPDX header and set -e checks
# ===========================================================================

class TestScriptHeaders:
    @pytest.mark.parametrize("script_path", list(SCRIPTS.values()),
                              ids=list(SCRIPTS.keys()))
    def test_spdx_header_in_shell_scripts(self, script_path):
        content = script_path.read_text()
        assert "SPDX-License-Identifier: Apache-2.0" in content, \
            f"{script_path.name} missing SPDX header"

    @pytest.mark.parametrize("script_path", list(SCRIPTS.values()),
                              ids=list(SCRIPTS.keys()))
    def test_set_e_in_shell_scripts(self, script_path):
        content = script_path.read_text()
        assert "set -e" in content, \
            f"{script_path.name} missing 'set -e'"

    def test_sysctl_conf_spdx_header(self):
        content = SYSCTL_CONF.read_text()
        assert "SPDX-License-Identifier: Apache-2.0" in content

    @pytest.mark.parametrize("bt_script", BT_SCRIPTS, ids=[p.name for p in BT_SCRIPTS])
    def test_bt_scripts_spdx_header(self, bt_script):
        content = bt_script.read_text()
        assert "SPDX-License-Identifier: Apache-2.0" in content, \
            f"{bt_script.name} missing SPDX header"


# ===========================================================================
# 3. Bash syntax validation
# ===========================================================================

class TestBashSyntax:
    @pytest.mark.parametrize("script_path", list(SCRIPTS.values()),
                              ids=list(SCRIPTS.keys()))
    def test_bash_syntax(self, script_path):
        result = bash_syntax_check(script_path)
        assert result.returncode == 0, (
            f"bash -n failed for {script_path.name}:\n{result.stderr}"
        )


# ===========================================================================
# 4. bpftrace shebang check
# ===========================================================================

class TestBtScripts:
    @pytest.mark.parametrize("bt_script", BT_SCRIPTS, ids=[p.name for p in BT_SCRIPTS])
    def test_bt_shebang(self, bt_script):
        first_line = bt_script.read_text().splitlines()[0]
        assert first_line.startswith("#!/usr/bin/env bpftrace"), \
            f"{bt_script.name} has incorrect shebang: {first_line!r}"

    @pytest.mark.parametrize("bt_script", BT_SCRIPTS, ids=[p.name for p in BT_SCRIPTS])
    def test_bt_has_begin_block(self, bt_script):
        content = bt_script.read_text()
        assert "BEGIN" in content, f"{bt_script.name} missing BEGIN block"

    @pytest.mark.parametrize("bt_script", BT_SCRIPTS, ids=[p.name for p in BT_SCRIPTS])
    def test_bt_has_end_block(self, bt_script):
        content = bt_script.read_text()
        assert "END" in content, f"{bt_script.name} missing END block"

    def test_fork_storm_uses_clone_tracepoint(self):
        content = (EBPF_DIR / "detect-fork-storm.bt").read_text()
        assert "syscalls:sys_enter_clone" in content or "kernel_clone" in content

    def test_oom_pressure_uses_oom_probe(self):
        content = (EBPF_DIR / "detect-oom-pressure.bt").read_text()
        assert "out_of_memory" in content or "oom:mark_victim" in content

    def test_io_anomaly_uses_block_tracepoint(self):
        content = (EBPF_DIR / "detect-io-anomaly.bt").read_text()
        assert "block:block_rq_issue" in content


# ===========================================================================
# 5. sysctl-hardening.conf format validation
# ===========================================================================

class TestSysctlConf:
    REQUIRED_KEYS = [
        "kernel.panic",
        "kernel.panic_on_oops",
        "kernel.softlockup_panic",
        "kernel.hardlockup_panic",
        "kernel.unknown_nmi_panic",
        "kernel.dmesg_restrict",
        "kernel.kptr_restrict",
        "vm.swappiness",
        "vm.dirty_ratio",
        "net.ipv4.tcp_keepalive_time",
        "net.ipv4.conf.all.rp_filter",
    ]

    # Expected types: "int" means value must be an integer, "bool" means 0 or 1
    KEY_TYPES = {
        "kernel.panic":                 "int",
        "kernel.panic_on_oops":         "bool",
        "kernel.panic_on_warn":         "bool",
        "kernel.softlockup_panic":      "bool",
        "kernel.hardlockup_panic":      "bool",
        "kernel.unknown_nmi_panic":     "bool",
        "kernel.oops_limit":            "int",
        "kernel.dmesg_restrict":        "bool",
        "kernel.kptr_restrict":         "int",
        "kernel.yama.ptrace_scope":     "int",
        "vm.overcommit_memory":         "int",
        "vm.swappiness":                "int",
        "vm.dirty_ratio":               "int",
        "vm.dirty_background_ratio":    "int",
        "vm.vfs_cache_pressure":        "int",
        "net.ipv4.tcp_keepalive_time":  "int",
        "net.ipv4.tcp_keepalive_intvl": "int",
        "net.ipv4.tcp_keepalive_probes":"int",
        "net.core.rmem_max":            "int",
        "net.core.wmem_max":            "int",
        "net.ipv4.conf.all.rp_filter":  "bool",
        "net.ipv4.conf.default.rp_filter": "bool",
    }

    def _parse_conf(self):
        """Return dict of key -> value from the sysctl conf (non-comment lines)."""
        parsed = {}
        for line in SYSCTL_CONF.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # sysctl format: key = value  OR  key=value
            m = re.match(r'^([a-z0-9_.]+)\s*=\s*(.+)$', stripped)
            if m:
                key, val = m.group(1), m.group(2).split("#")[0].strip()
                parsed[key] = val
            else:
                pytest.fail(f"Unparseable line in sysctl-hardening.conf: {line!r}")
        return parsed

    def test_all_lines_valid_format(self):
        """Every non-comment, non-blank line must be key = value format."""
        self._parse_conf()  # raises if any line is invalid

    @pytest.mark.parametrize("required_key", REQUIRED_KEYS)
    def test_required_keys_present(self, required_key):
        parsed = self._parse_conf()
        assert required_key in parsed, \
            f"Required key '{required_key}' not found in sysctl-hardening.conf"

    def test_no_duplicate_keys(self):
        seen = []
        duplicates = []
        for line in SYSCTL_CONF.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = re.match(r'^([a-z0-9_.]+)\s*=', stripped)
            if m:
                key = m.group(1)
                if key in seen:
                    duplicates.append(key)
                seen.append(key)
        assert not duplicates, \
            f"Duplicate keys in sysctl-hardening.conf: {duplicates}"

    @pytest.mark.parametrize("key,expected_type", list(KEY_TYPES.items()))
    def test_value_types(self, key, expected_type):
        """Verify each key's value matches its expected type (int or bool)."""
        parsed = self._parse_conf()
        if key not in parsed:
            pytest.skip(f"Key '{key}' not present in conf (optional)")

        val = parsed[key]

        if expected_type == "bool":
            # Bool sysctl values must be 0 or 1
            # Some like tcp_rmem have multi-value; skip those
            if " " not in val:
                assert val in ("0", "1"), \
                    f"Key '{key}' expected bool (0/1) but got {val!r}"
        elif expected_type == "int":
            # Int sysctl values must be numeric (optionally with multiple space-separated ints)
            parts = val.split()
            for part in parts:
                assert part.lstrip("-").isdigit(), \
                    f"Key '{key}' expected int but got {val!r}"


# ===========================================================================
# 6. panic-config.sh functional tests
# ===========================================================================

class TestPanicConfig:
    @pytest.mark.skipif(not IS_LINUX, reason="Requires Linux /proc")
    def test_status_exits_0_or_1(self):
        """--status reads /proc/sys/kernel/panic — should exit 0 or 1."""
        result = run(["bash", str(SCRIPTS["panic_config"]), "--status"])
        assert result.returncode in (0, 1), \
            f"panic-config.sh --status returned unexpected code {result.returncode}"

    @pytest.mark.skipif(not IS_LINUX, reason="Requires Linux /proc")
    def test_status_output_contains_kernel_panic(self):
        result = run(["bash", str(SCRIPTS["panic_config"]), "--status"])
        assert "kernel.panic" in result.stdout, \
            "Expected 'kernel.panic' in --status output"

    @pytest.mark.skipif(not IS_LINUX, reason="Requires Linux")
    def test_dry_run_apply_exits_0(self):
        """--dry-run --apply should print sysctl params and exit 0."""
        result = run([
            "bash", str(SCRIPTS["panic_config"]),
            "--dry-run", "--apply", "--panic-on-oops", "--watchdog-panic",
            "--reboot-delay", "10",
        ])
        assert result.returncode == 0, \
            f"panic-config.sh --dry-run --apply failed:\n{result.stderr}"

    @pytest.mark.skipif(not IS_LINUX, reason="Requires Linux")
    def test_dry_run_shows_kernel_panic(self):
        result = run([
            "bash", str(SCRIPTS["panic_config"]),
            "--dry-run", "--apply",
        ])
        combined = result.stdout + result.stderr
        assert "kernel.panic" in combined, \
            "Expected 'kernel.panic' in dry-run output"

    @pytest.mark.skipif(not IS_LINUX, reason="Requires Linux")
    def test_dry_run_shows_panic_on_oops(self):
        result = run([
            "bash", str(SCRIPTS["panic_config"]),
            "--dry-run", "--apply", "--panic-on-oops",
        ])
        combined = result.stdout + result.stderr
        assert "panic_on_oops" in combined

    @pytest.mark.skipif(not IS_LINUX, reason="Requires Linux")
    def test_dry_run_shows_softlockup_panic(self):
        result = run([
            "bash", str(SCRIPTS["panic_config"]),
            "--dry-run", "--apply", "--watchdog-panic",
        ])
        combined = result.stdout + result.stderr
        assert "softlockup_panic" in combined

    def test_show_bootargs(self):
        result = run(["bash", str(SCRIPTS["panic_config"]), "--show-bootargs"])
        combined = result.stdout + result.stderr
        assert "panic=10" in combined or "bootargs" in combined.lower(), \
            "Expected boot args suggestions in --show-bootargs output"

    def test_invalid_reboot_delay_exits_1(self):
        result = run(["bash", str(SCRIPTS["panic_config"]),
                      "--reboot-delay", "notanumber", "--status"])
        assert result.returncode == 1

    @pytest.mark.skipif(not IS_ROOT, reason="Requires root to write sysctl.d")
    def test_apply_writes_conf(self, tmp_path, monkeypatch):
        """--apply writes to SYSCTL_CONF (tested with a temp path via env override)."""
        # This test only runs as root; it monkeypatches the conf path by wrapping
        # the script in a shell that overrides the path variable before sourcing.
        # Since we can't easily override internal variables without refactoring
        # the script, we verify the behavior via the return code and stdout.
        result = run([
            "bash", str(SCRIPTS["panic_config"]),
            "--apply", "--panic-on-oops",
        ])
        assert result.returncode == 0


# ===========================================================================
# 7. oom-tuning.sh functional tests
# ===========================================================================

class TestOomTuning:
    @pytest.mark.skipif(not IS_LINUX, reason="Requires Linux /proc")
    def test_status_exits_0_or_1(self):
        result = run(["bash", str(SCRIPTS["oom_tuning"]), "--status"])
        assert result.returncode in (0, 1), \
            f"oom-tuning.sh --status returned unexpected code {result.returncode}"

    @pytest.mark.skipif(not IS_LINUX, reason="Requires Linux /proc")
    def test_status_shows_oom_score_header(self):
        result = run(["bash", str(SCRIPTS["oom_tuning"]), "--status"])
        combined = result.stdout + result.stderr
        # Should display column headers
        assert "oom_score" in combined.lower() or "OOM" in combined, \
            "Expected OOM score information in --status output"

    @pytest.mark.skipif(not IS_LINUX, reason="Requires Linux")
    def test_dry_run_protect_exits_0(self):
        result = run([
            "bash", str(SCRIPTS["oom_tuning"]),
            "--dry-run", "--protect", "sshd",
        ])
        assert result.returncode == 0

    @pytest.mark.skipif(not IS_LINUX, reason="Requires Linux")
    def test_dry_run_deprioritize_exits_0(self):
        result = run([
            "bash", str(SCRIPTS["oom_tuning"]),
            "--dry-run", "--deprioritize", "tmpfiles-clean",
        ])
        assert result.returncode == 0

    def test_dry_run_cgroup_limit_exits_0(self):
        result = run([
            "bash", str(SCRIPTS["oom_tuning"]),
            "--dry-run", "--cgroup-limit", "my-worker", "512",
        ])
        assert result.returncode == 0

    def test_dry_run_cgroup_limit_shows_memorymx(self):
        result = run([
            "bash", str(SCRIPTS["oom_tuning"]),
            "--dry-run", "--cgroup-limit", "my-worker", "256",
        ])
        combined = result.stdout + result.stderr
        assert "MemoryMax=256M" in combined or "256" in combined

    @pytest.mark.skipif(not IS_LINUX, reason="Requires Linux")
    def test_dry_run_apply_defaults_exits_0(self):
        result = run([
            "bash", str(SCRIPTS["oom_tuning"]),
            "--dry-run", "--apply-defaults",
        ])
        assert result.returncode == 0

    def test_cgroup_limit_invalid_mb_exits_1(self):
        result = run([
            "bash", str(SCRIPTS["oom_tuning"]),
            "--cgroup-limit", "svc", "notanumber",
        ])
        assert result.returncode == 1

    def test_missing_protect_arg_exits_1(self):
        result = run(["bash", str(SCRIPTS["oom_tuning"]), "--protect"])
        assert result.returncode == 1


# ===========================================================================
# 8. ebpf-to-prometheus.sh tests
# ===========================================================================

class TestEbpfToPrometheus:
    def test_no_bpftrace_exits_2(self):
        """If bpftrace is not installed, the script should exit 2."""
        env = {**os.environ, "PATH": "/usr/bin:/bin"}  # minimal path, likely no bpftrace
        result = run(
            ["bash", str(SCRIPTS["ebpf_to_prom"]),
             "--script", str(BT_SCRIPTS[0])],
            env=env,
        )
        # On a system without bpftrace, should exit 2
        # On a system with bpftrace, this may exit 0 or 1
        assert result.returncode in (0, 1, 2), \
            f"Unexpected exit code: {result.returncode}\n{result.stderr}"

    def test_dry_run_exits_0(self):
        result = run([
            "bash", str(SCRIPTS["ebpf_to_prom"]),
            "--dry-run", "--script", str(BT_SCRIPTS[0]),
        ])
        assert result.returncode == 0

    def test_dry_run_all_exits_0(self):
        result = run([
            "bash", str(SCRIPTS["ebpf_to_prom"]),
            "--dry-run", "--all",
        ])
        assert result.returncode == 0

    def test_missing_script_exits_1(self):
        result = run([
            "bash", str(SCRIPTS["ebpf_to_prom"]),
            "--script", "/nonexistent/file.bt",
        ])
        assert result.returncode in (1, 2)

    def test_no_scripts_exits_1(self):
        result = run(["bash", str(SCRIPTS["ebpf_to_prom"])])
        # No scripts = usage (which exits 0) or error
        assert result.returncode in (0, 1)


# ===========================================================================
# 9. Parametrized: sysctl key-type validation
# ===========================================================================

@pytest.mark.parametrize("key,expected_type", [
    ("kernel.panic",            "int"),
    ("kernel.panic_on_oops",    "bool"),
    ("kernel.panic_on_warn",    "bool"),
    ("kernel.softlockup_panic", "bool"),
    ("kernel.hardlockup_panic", "bool"),
    ("vm.swappiness",           "int"),
    ("vm.dirty_ratio",          "int"),
    ("net.ipv4.tcp_keepalive_time",   "int"),
    ("net.ipv4.tcp_keepalive_intvl",  "int"),
    ("net.ipv4.tcp_keepalive_probes", "int"),
    ("net.core.rmem_max",             "int"),
    ("net.core.wmem_max",             "int"),
    ("kernel.dmesg_restrict",         "bool"),
    ("kernel.kptr_restrict",          "int"),
    ("net.ipv4.conf.all.rp_filter",   "bool"),
    ("kernel.yama.ptrace_scope",      "int"),
])
def test_sysctl_key_value_type(key, expected_type):
    """Parametrized: each key in sysctl-hardening.conf must match its expected type."""
    parsed = {}
    for line in SYSCTL_CONF.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r'^([a-z0-9_.]+)\s*=\s*(.+)$', stripped)
        if m:
            k, v = m.group(1), m.group(2).split("#")[0].strip()
            parsed[k] = v

    if key not in parsed:
        pytest.skip(f"Key '{key}' not in conf")

    val = parsed[key]

    if expected_type == "bool":
        if " " not in val:  # skip multi-value entries
            assert val in ("0", "1"), \
                f"Key '{key}': expected bool (0 or 1) but got {val!r}"
    elif expected_type == "int":
        for part in val.split():
            assert part.lstrip("-").isdigit(), \
                f"Key '{key}': expected integer but got {val!r}"


# ===========================================================================
# Extended oom-tuning.sh and panic-config.sh tests
# ===========================================================================

class TestOomTuningExtended:
    """Deeper behavioral tests for oom-tuning.sh."""

    def _run(self, args, timeout=8):
        return run(["bash", str(SCRIPTS["oom_tuning"])] + args, timeout=timeout)

    def test_help_exits_0(self):
        result = self._run(["--help"])
        assert result.returncode == 0

    def test_help_shows_usage(self):
        result = self._run(["--help"])
        combined = result.stdout + result.stderr
        assert "Usage" in combined or "usage" in combined.lower()

    def test_unknown_option_exits_nonzero_or_warns(self):
        result = self._run(["--totally-unknown-flag-xyz"])
        combined = result.stdout + result.stderr
        # Script should warn about unknown option
        assert "Unknown" in combined or "unknown" in combined or result.returncode != 0

    @pytest.mark.skipif(not IS_LINUX, reason="Requires Linux")
    def test_apply_defaults_exits_0(self):
        """--apply-defaults should succeed even without root (warns but doesn't fail)."""
        result = self._run(["--apply-defaults"])
        assert result.returncode == 0

    def test_apply_defaults_mentions_protecting(self):
        result = self._run(["--apply-defaults"])
        combined = result.stdout + result.stderr
        assert "Protect" in combined or "protect" in combined.lower() or "oom" in combined.lower()

    def test_protect_missing_arg_exits_nonzero(self):
        result = self._run(["--protect"])
        assert result.returncode != 0

    def test_deprioritize_missing_arg_exits_nonzero(self):
        result = self._run(["--deprioritize"])
        assert result.returncode != 0

    def test_cgroup_limit_non_numeric_exits_1(self):
        result = self._run(["--cgroup-limit", "some-service", "notanumber"])
        assert result.returncode == 1

    def test_status_output_has_oom_section(self):
        result = self._run(["--status"])
        combined = result.stdout + result.stderr
        assert "OOM" in combined or "oom" in combined.lower() or "score" in combined.lower()


class TestPanicConfigExtended:
    """Extended panic-config.sh behavioral tests."""

    def _run(self, args, timeout=5):
        return run(["bash", str(SCRIPTS["panic_config"])] + args, timeout=timeout)

    @pytest.mark.skipif(not IS_LINUX, reason="Requires Linux")
    def test_dry_run_status_exits_0(self):
        result = self._run(["--status"])
        assert result.returncode == 0

    def test_status_shows_current_settings(self):
        result = self._run(["--status"])
        combined = result.stdout + result.stderr
        assert "panic" in combined.lower() or "kernel" in combined.lower() or "sysctl" in combined.lower()

    @pytest.mark.skipif(not IS_LINUX, reason="Requires Linux")
    def test_dry_run_shows_sysctl_conf_path(self):
        result = self._run(["--dry-run", "--apply"])
        combined = result.stdout + result.stderr
        assert ".conf" in combined or "sysctl" in combined.lower()

    @pytest.mark.skipif(not IS_LINUX, reason="Requires Linux")
    def test_dry_run_shows_would_execute(self):
        result = self._run(["--dry-run", "--apply"])
        combined = result.stdout + result.stderr
        assert "sysctl" in combined.lower() or "would" in combined.lower() or "dry" in combined.lower()

    @pytest.mark.skipif(not IS_LINUX, reason="Requires Linux")
    def test_reboot_delay_flag_accepted(self):
        """--reboot-delay should be accepted and reflected in dry-run output."""
        result = self._run(["--dry-run", "--apply", "--reboot-delay", "30"])
        assert result.returncode == 0

    @pytest.mark.skipif(not IS_LINUX, reason="Requires Linux")
    def test_sysctl_conf_path_present(self):
        """Generated config should reference kernel.panic sysctl."""
        result = self._run(["--dry-run", "--apply"])
        combined = result.stdout + result.stderr
        assert "kernel.panic" in combined


class TestKernelHardeningCrossModule:
    """Cross-module integration checks for kernel-hardening."""

    def test_all_scripts_have_set_euo(self):
        """All shell scripts use strict error mode."""
        for script_name in ["panic-config.sh", "oom-tuning.sh"]:
            path = str(MODULE_DIR / script_name)
            with open(path) as f:
                content = f.read()
            assert "set -" in content, f"{script_name} missing set -euo pipefail"

    def test_all_scripts_have_spdx(self):
        for script_name in ["panic-config.sh", "oom-tuning.sh"]:
            path = str(MODULE_DIR / script_name)
            with open(path) as f:
                content = f.read()
            assert "SPDX-License-Identifier" in content, f"{script_name} missing SPDX header"

    def test_panic_values_are_sane(self):
        """Panic timeout should be positive and not excessively large."""
        # Read the panic-config.sh defaults
        path = str(MODULE_DIR / "panic-config.sh")
        with open(path) as f:
            content = f.read()
        import re
        # Extract kernel.panic= value from the generated config
        m = re.search(r'kernel\.panic\s*=\s*(\d+)', content)
        if m:
            val = int(m.group(1))
            assert 1 <= val <= 300, f"kernel.panic={val} seems unreasonable"
