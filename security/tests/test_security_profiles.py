# SPDX-License-Identifier: Apache-2.0
"""
tests/test_security_profiles.py — Static validation of seccomp and AppArmor profiles.
"""
import json
import re
from pathlib import Path

import pytest

SECURITY_DIR = Path(__file__).parent.parent
SECCOMP_FILE = SECURITY_DIR / "seccomp-profile.json"
APPARMOR_FILE = SECURITY_DIR / "apparmor-profile"
SYSTEMD_FILE  = SECURITY_DIR / "systemd-unit.service"

REQUIRED_SYSCALLS = {
    "read", "write", "close", "openat", "socket", "bind", "listen",
    "accept", "accept4", "epoll_create1", "epoll_ctl", "epoll_wait",
    "epoll_pwait", "futex", "clone", "exit_group", "getrandom",
}

FORBIDDEN_SYSCALLS = {"ptrace", "mount", "umount2", "kexec_load",
                       "init_module", "finit_module", "delete_module",
                       "pivot_root", "chroot"}


@pytest.fixture(scope="module")
def seccomp():
    return json.loads(SECCOMP_FILE.read_text())


@pytest.fixture(scope="module")
def apparmor():
    return APPARMOR_FILE.read_text()


@pytest.fixture(scope="module")
def systemd():
    return SYSTEMD_FILE.read_text()


class TestSeccompProfile:

    def test_default_action_is_deny(self, seccomp):
        assert seccomp["defaultAction"] == "SCMP_ACT_ERRNO"

    def test_arm64_architecture_present(self, seccomp):
        arches = [a["architecture"] for a in seccomp["archMap"]]
        assert "SCMP_ARCH_AARCH64" in arches

    def test_required_syscalls_allowed(self, seccomp):
        allowed = set()
        for rule in seccomp["syscalls"]:
            if rule["action"] == "SCMP_ACT_ALLOW":
                allowed.update(rule["names"])
        missing = REQUIRED_SYSCALLS - allowed
        assert not missing, f"Required syscalls missing from allowlist: {missing}"

    def test_no_forbidden_syscalls_allowed(self, seccomp):
        allowed = set()
        for rule in seccomp["syscalls"]:
            if rule["action"] == "SCMP_ACT_ALLOW":
                allowed.update(rule["names"])
        present = FORBIDDEN_SYSCALLS & allowed
        assert not present, f"Dangerous syscalls in allowlist: {present}"

    def test_json_is_valid(self):
        data = json.loads(SECCOMP_FILE.read_text())
        assert "syscalls" in data
        assert "defaultAction" in data

    def test_all_rules_have_action(self, seccomp):
        for rule in seccomp["syscalls"]:
            assert "action" in rule, f"Rule missing action: {rule}"
            assert "names" in rule, f"Rule missing names: {rule}"


class TestAppArmorProfile:

    def test_sysfs_edac_readable(self, apparmor):
        assert "/sys/devices/system/edac/**" in apparmor

    def test_proc_mem_denied(self, apparmor):
        assert "deny /proc/**/mem" in apparmor

    def test_sys_ptrace_denied(self, apparmor):
        assert "deny capability sys_ptrace" in apparmor

    def test_sys_admin_denied(self, apparmor):
        assert "deny capability sys_admin" in apparmor

    def test_tls_certs_readable(self, apparmor):
        assert "/etc/hw-fault-exporter/tls/**" in apparmor

    def test_log_dir_writable(self, apparmor):
        assert "/var/log/hw-fault-exporter/**" in apparmor

    def test_network_tcp_allowed(self, apparmor):
        assert "network tcp" in apparmor

    def test_etc_write_denied(self, apparmor):
        assert "deny /etc/**" in apparmor


class TestSystemdUnit:

    def test_no_new_privileges(self, systemd):
        assert "NoNewPrivileges=yes" in systemd

    def test_protect_system_strict(self, systemd):
        assert "ProtectSystem=strict" in systemd

    def test_private_tmp(self, systemd):
        assert "PrivateTmp=yes" in systemd

    def test_memory_deny_write_execute(self, systemd):
        assert "MemoryDenyWriteExecute=yes" in systemd

    def test_restrict_realtime(self, systemd):
        assert "RestrictRealtime=yes" in systemd

    def test_apparmor_profile_set(self, systemd):
        assert "AppArmorProfile=" in systemd

    def test_tls_flags_in_exec(self, systemd):
        assert "--tls-cert" in systemd
        assert "--tls-key" in systemd
        assert "--tls-ca" in systemd

    def test_capability_bounding_set(self, systemd):
        assert "CapabilityBoundingSet=" in systemd
        # Should NOT include CAP_SYS_ADMIN
        cap_line = [l for l in systemd.splitlines()
                    if l.startswith("CapabilityBoundingSet=")]
        assert cap_line
        assert "CAP_SYS_ADMIN" not in cap_line[0]
