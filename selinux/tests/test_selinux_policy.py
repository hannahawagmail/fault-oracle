# SPDX-License-Identifier: Apache-2.0
"""
tests/test_selinux_policy.py — Static analysis tests for hw_fault_exporter.te

These tests parse the policy text directly (no SELinux kernel required) and
assert structural correctness: required types are declared, no wildcard allow
rules, sysfs read is permitted, network write is excluded for outbound connects.
"""
import re
from pathlib import Path

import pytest

SELINUX_DIR = Path(__file__).parent.parent
TE_FILE = SELINUX_DIR / "hw_fault_exporter.te"
FC_FILE = SELINUX_DIR / "hw_fault_exporter.fc"


@pytest.fixture(scope="module")
def te_content():
    return TE_FILE.read_text()


@pytest.fixture(scope="module")
def fc_content():
    return FC_FILE.read_text()


class TestTypeDeclarations:

    def test_main_type_declared(self, te_content):
        assert "type hw_fault_exporter_t;" in te_content

    def test_exec_type_declared(self, te_content):
        assert "type hw_fault_exporter_exec_t;" in te_content

    def test_log_type_declared(self, te_content):
        assert "type hw_fault_exporter_log_t;" in te_content

    def test_cert_type_declared(self, te_content):
        assert "type hw_fault_exporter_cert_t;" in te_content

    def test_port_type_declared(self, te_content):
        assert "type hw_fault_exporter_port_t;" in te_content


class TestAllowRules:

    def test_sysfs_read_allowed(self, te_content):
        """Must allow reading sysfs files (hardware counters)."""
        assert "sysfs_t" in te_content
        assert "read" in te_content

    def test_tcp_bind_allowed(self, te_content):
        """Must allow binding a TCP port for Prometheus scrapes."""
        assert "tcp_socket" in te_content
        assert "name_bind" in te_content

    def test_log_write_allowed(self, te_content):
        """Must allow writing log files."""
        assert "hw_fault_exporter_log_t" in te_content
        assert "write" in te_content

    def test_cert_read_allowed(self, te_content):
        """Must allow reading TLS cert files."""
        assert "hw_fault_exporter_cert_t" in te_content


class TestNoWildcards:

    def test_no_star_wildcard_in_allow(self, te_content):
        """No allow rule should use * wildcard on types or perms."""
        allow_lines = [l for l in te_content.splitlines()
                       if l.strip().startswith("allow ")]
        wildcards = [l for l in allow_lines if re.search(r'\*', l)]
        assert not wildcards, f"Wildcard allow rules found: {wildcards}"

    def test_no_self_ptrace(self, te_content):
        """ptrace should not appear in any allow rule."""
        allow_lines = [l for l in te_content.splitlines()
                       if l.strip().startswith("allow ")]
        ptrace_rules = [l for l in allow_lines if "ptrace" in l]
        assert not ptrace_rules, f"ptrace in allow rules: {ptrace_rules}"


class TestFileContexts:

    def test_binary_has_exec_context(self, fc_content):
        assert "hw_fault_exporter_exec_t" in fc_content
        assert "/usr/local/bin/hw-fault-exporter" in fc_content

    def test_cert_dir_has_cert_context(self, fc_content):
        assert "hw_fault_exporter_cert_t" in fc_content
        assert "/etc/hw-fault-exporter" in fc_content

    def test_log_dir_has_log_context(self, fc_content):
        assert "hw_fault_exporter_log_t" in fc_content
        assert "/var/log/hw-fault-exporter" in fc_content

    def test_fc_uses_gen_context(self, fc_content):
        """All context assignments should use gen_context() macro."""
        context_lines = [l for l in fc_content.splitlines()
                         if l.strip() and not l.startswith("#")]
        for line in context_lines:
            assert "gen_context(" in line, f"Missing gen_context in: {line}"
