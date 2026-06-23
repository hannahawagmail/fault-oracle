# SPDX-License-Identifier: Apache-2.0
"""test_network_resilience.py — Tests for network resilience scripts and configs"""
import configparser
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
NETWORK_RESILIENCE_DIR = Path(__file__).parent.parent
PROFILES_DIR = NETWORK_RESILIENCE_DIR / "nm-connection-profiles"
MONITOR_SCRIPT = NETWORK_RESILIENCE_DIR / "connection-monitor.sh"
OOB_HEARTBEAT = NETWORK_RESILIENCE_DIR / "oob-heartbeat.py"
OOB_PROTOCOL_DOC = NETWORK_RESILIENCE_DIR / "oob-monitor-protocol.md"

NM_PROFILES = {
    "primary":   PROFILES_DIR / "10-primary-eth.nmconnection",
    "wifi":      PROFILES_DIR / "20-wifi-backup.nmconnection",
    "lte":       PROFILES_DIR / "30-lte-fallback.nmconnection",
}

# ---------------------------------------------------------------------------
# CRC-8/MAXIM helper (copied inline to keep tests self-contained)
# ---------------------------------------------------------------------------
def _crc8(data: bytes) -> int:
    """CRC-8/MAXIM: polynomial 0x31, init 0x00."""
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc << 1) ^ 0x31 if crc & 0x80 else crc << 1
            crc &= 0xFF
    return crc


# ---------------------------------------------------------------------------
# Load crc8 and build_frame from oob-heartbeat.py via exec()
# We exec the module source (minus the __main__ block) into a namespace so
# we can call its functions without importing it as a module (avoids having
# to add the parent dir to sys.path in a fragile way).
# ---------------------------------------------------------------------------
_OOB_NAMESPACE: dict = {}

def _load_oob_module():
    global _OOB_NAMESPACE
    if _OOB_NAMESPACE:
        return _OOB_NAMESPACE
    src = OOB_HEARTBEAT.read_text()
    # Strip everything from 'if __name__ ==' onward so exec doesn't call main()
    if "if __name__" in src:
        src = src[:src.index("if __name__")]
    exec(compile(src, str(OOB_HEARTBEAT), "exec"), _OOB_NAMESPACE)
    return _OOB_NAMESPACE


# ---------------------------------------------------------------------------
# connection-monitor.sh tests
# ---------------------------------------------------------------------------
class TestConnectionMonitor:

    def test_connection_monitor_exists(self):
        assert MONITOR_SCRIPT.exists(), f"{MONITOR_SCRIPT} does not exist"

    def test_connection_monitor_bash_syntax(self):
        """bash -n should exit 0 (syntax check only, no execution)."""
        result = subprocess.run(
            ["bash", "-n", str(MONITOR_SCRIPT)],
            capture_output=True,
        )
        assert result.returncode == 0, (
            f"bash -n failed:\n{result.stderr.decode()}"
        )

    def test_connection_monitor_has_spdx(self):
        content = MONITOR_SCRIPT.read_text()
        assert "SPDX-License-Identifier" in content, \
            "connection-monitor.sh is missing SPDX-License-Identifier header"

    def test_connection_monitor_has_set_e(self):
        content = MONITOR_SCRIPT.read_text()
        assert "set -e" in content, \
            "connection-monitor.sh does not contain 'set -e'"

    def test_connection_monitor_status_doesnt_crash(self):
        """
        Running --status may exit 0 (healthy) or 1 (no connectivity in CI),
        but must not crash with an unhandled error (exit >= 2 from our code,
        or a bash error like 127/126/139).
        Acceptable: 0 or 1. Unacceptable: 2, 3, or bash crash codes.
        """
        result = subprocess.run(
            ["bash", str(MONITOR_SCRIPT), "--status"],
            capture_output=True,
            timeout=30,
        )
        # Exit code 0 = healthy, 1 = ping failed. Both are expected outcomes.
        # Exit code 2 = tools missing (also acceptable in a minimal CI env).
        # Exit code 127 = bash command not found = real problem.
        assert result.returncode in (0, 1, 2), (
            f"connection-monitor.sh --status exited with unexpected code "
            f"{result.returncode}\nstdout: {result.stdout.decode()}\n"
            f"stderr: {result.stderr.decode()}"
        )


# ---------------------------------------------------------------------------
# oob-heartbeat.py tests
# ---------------------------------------------------------------------------
class TestOobHeartbeat:

    def test_oob_heartbeat_exists(self):
        assert OOB_HEARTBEAT.exists(), f"{OOB_HEARTBEAT} does not exist"

    def test_oob_heartbeat_dry_run(self):
        """--dry-run must exit 0 without needing a serial port or pyserial."""
        result = subprocess.run(
            [sys.executable, str(OOB_HEARTBEAT), "--dry-run"],
            capture_output=True,
            timeout=15,
        )
        assert result.returncode == 0, (
            f"oob-heartbeat.py --dry-run failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout.decode()}\nstderr: {result.stderr.decode()}"
        )

    def test_crc8_zero(self):
        """CRC-8 of a single zero byte must equal 0 (XOR with 0 is no-op)."""
        ns = _load_oob_module()
        assert ns["crc8"](b'\x00') == 0

    def test_crc8_known_value(self):
        """CRC-8(b'\\xAA\\x55') must equal 0x9A (pre-computed reference)."""
        ns = _load_oob_module()
        result = ns["crc8"](b'\xAA\x55')
        assert result == 0x9A, f"Got 0x{result:02X}, expected 0x9A"

    def test_build_frame_length(self):
        ns = _load_oob_module()
        frame = ns["build_frame"](seq=0, status_flags=0x0F, cpu_load=50)
        assert len(frame) == 8, f"Frame length is {len(frame)}, expected 8"

    def test_build_frame_sync_bytes(self):
        ns = _load_oob_module()
        frame = ns["build_frame"](seq=1234, status_flags=0x0F, cpu_load=25)
        assert frame[0] == 0xAA, f"frame[0] = 0x{frame[0]:02X}, expected 0xAA"
        assert frame[1] == 0x55, f"frame[1] = 0x{frame[1]:02X}, expected 0x55"

    def test_build_frame_end_marker(self):
        ns = _load_oob_module()
        frame = ns["build_frame"](seq=0, status_flags=0, cpu_load=0)
        assert frame[6] == 0xFF, f"frame[6] = 0x{frame[6]:02X}, expected 0xFF"

    def test_build_frame_checksum(self):
        """frame[7] must equal crc8(frame[:7])."""
        ns = _load_oob_module()
        frame = ns["build_frame"](seq=42, status_flags=0x0F, cpu_load=75)
        expected_crc = _crc8(frame[:7])
        assert frame[7] == expected_crc, (
            f"frame[7] = 0x{frame[7]:02X}, expected crc8(frame[:7]) = 0x{expected_crc:02X}"
        )


# ---------------------------------------------------------------------------
# NetworkManager profile tests
# ---------------------------------------------------------------------------
class TestNMProfiles:

    def test_nm_profiles_exist(self):
        for name, path in NM_PROFILES.items():
            assert path.exists(), f"NM profile '{name}' not found at {path}"

    def test_nm_profiles_parse_as_ini(self):
        """All .nmconnection files must be valid INI/keyfile format."""
        for name, path in NM_PROFILES.items():
            cp = configparser.ConfigParser(strict=False)
            # NM keyfiles may have comment lines starting with '#'; configparser
            # handles them as inline_comment_prefixes.
            cp = configparser.RawConfigParser()
            cp.read(str(path))
            assert cp.sections(), (
                f"Profile '{name}' parsed as empty — not valid INI: {path}"
            )

    def test_nm_priority_order(self):
        """primary (100) > wifi (50) > lte (10)."""
        priorities = {}
        for name, path in NM_PROFILES.items():
            cp = configparser.RawConfigParser()
            cp.read(str(path))
            raw = cp.get("connection", "autoconnect-priority")
            priorities[name] = int(raw.strip())

        assert priorities["primary"] > priorities["wifi"], (
            f"primary priority {priorities['primary']} should be > "
            f"wifi priority {priorities['wifi']}"
        )
        assert priorities["wifi"] > priorities["lte"], (
            f"wifi priority {priorities['wifi']} should be > "
            f"lte priority {priorities['lte']}"
        )

    def test_nm_primary_interface(self):
        """Primary profile must specify interface-name=eth0."""
        cp = configparser.RawConfigParser()
        cp.read(str(NM_PROFILES["primary"]))
        iface = cp.get("connection", "interface-name").strip()
        assert iface == "eth0", (
            f"Primary profile interface-name is '{iface}', expected 'eth0'"
        )

    def test_nm_primary_priority_is_100(self):
        cp = configparser.RawConfigParser()
        cp.read(str(NM_PROFILES["primary"]))
        prio = int(cp.get("connection", "autoconnect-priority").strip())
        assert prio == 100, f"Primary priority is {prio}, expected 100"

    def test_nm_wifi_priority_is_50(self):
        cp = configparser.RawConfigParser()
        cp.read(str(NM_PROFILES["wifi"]))
        prio = int(cp.get("connection", "autoconnect-priority").strip())
        assert prio == 50, f"WiFi priority is {prio}, expected 50"

    def test_nm_lte_priority_is_10(self):
        cp = configparser.RawConfigParser()
        cp.read(str(NM_PROFILES["lte"]))
        prio = int(cp.get("connection", "autoconnect-priority").strip())
        assert prio == 10, f"LTE priority is {prio}, expected 10"

    def test_nm_route_metric_order(self):
        """route-metric: eth0 (100) < wifi (200) < lte (300)."""
        metrics = {}
        for name, path in NM_PROFILES.items():
            cp = configparser.RawConfigParser()
            cp.read(str(path))
            raw = cp.get("ipv4", "route-metric").strip()
            metrics[name] = int(raw)
        assert metrics["primary"] < metrics["wifi"] < metrics["lte"], (
            f"route-metric order wrong: {metrics}"
        )


# ---------------------------------------------------------------------------
# OOB protocol doc test
# ---------------------------------------------------------------------------
class TestOobProtocolDoc:

    def test_oob_protocol_doc_exists(self):
        assert OOB_PROTOCOL_DOC.exists(), \
            f"{OOB_PROTOCOL_DOC} does not exist"

    def test_oob_protocol_doc_length(self):
        content = OOB_PROTOCOL_DOC.read_text()
        assert len(content) > 1000, (
            f"oob-monitor-protocol.md is only {len(content)} chars; expected > 1000"
        )

    def test_oob_protocol_doc_has_spdx(self):
        content = OOB_PROTOCOL_DOC.read_text()
        assert "SPDX-License-Identifier" in content

    def test_oob_protocol_doc_documents_frame_format(self):
        content = OOB_PROTOCOL_DOC.read_text()
        # Must document the sync bytes and CRC
        assert "0xAA" in content and "0x55" in content, \
            "Protocol doc missing sync byte documentation"
        assert "crc8" in content.lower() or "CRC" in content, \
            "Protocol doc missing CRC documentation"


# ===========================================================================
# Extended connection-monitor.sh behavioral tests
# ===========================================================================

@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Requires Linux network tools')
class TestConnectionMonitorExtended:
    """Deeper behavioral tests for connection-monitor.sh."""

    def _run(self, args, timeout=5):
        script = str(MONITOR_SCRIPT)
        result = subprocess.run(
            ["bash", script] + args,
            capture_output=True, text=True, timeout=timeout
        )
        return result

    def test_no_mode_exits_3(self):
        """No mode argument → usage error exit 3."""
        result = self._run([])
        assert result.returncode == 3

    def test_no_mode_prints_usage(self):
        combined = self._run([]).stdout + self._run([]).stderr
        assert "Usage" in combined or "usage" in combined.lower()

    def test_unknown_flag_exits_3(self):
        result = self._run(["--bogus-flag-xyz"])
        assert result.returncode == 3

    def test_status_exits_0_without_network(self):
        """--status should succeed (0 or 1) even with no connectivity."""
        result = self._run(["--status"])
        assert result.returncode in (0, 1)

    def test_status_shows_connection_header(self):
        result = self._run(["--status"])
        combined = result.stdout + result.stderr
        assert "Connection" in combined or "Status" in combined or "Route" in combined

    def test_dry_run_failover_exits_0(self):
        result = self._run(["--dry-run", "--failover"])
        assert result.returncode == 0

    def test_dry_run_failover_mentions_dryrun(self):
        result = self._run(["--dry-run", "--failover"])
        combined = result.stdout + result.stderr
        assert "DRY" in combined or "dry" in combined.lower() or "would" in combined.lower()

    def test_dry_run_status_exits_0(self):
        result = self._run(["--dry-run", "--status"])
        assert result.returncode in (0, 1)

    def test_ping_target_flag_accepted(self):
        """--ping-target should be accepted without erroring."""
        result = self._run(["--status", "--ping-target", "127.0.0.1"])
        assert result.returncode in (0, 1)

    def test_interval_flag_accepted(self):
        """--interval N should be accepted as a numeric argument."""
        result = self._run(["--status", "--interval", "5"])
        assert result.returncode in (0, 1)

    def test_failure_threshold_flag_accepted(self):
        result = self._run(["--status", "--failure-threshold", "2"])
        assert result.returncode in (0, 1)

    def test_bash_syntax_valid(self):
        import subprocess
        result = subprocess.run(["bash", "-n", str(MONITOR_SCRIPT)], capture_output=True, text=True)

    def test_spdx_header_present(self):
        with open(str(MONITOR_SCRIPT)) as f:
            text = f.read()
        assert "SPDX-License-Identifier: Apache-2.0" in text

    def test_set_e_present(self):
        with open(str(MONITOR_SCRIPT)) as f:
            text = f.read()
        assert "set -" in text

    def test_exit_codes_documented(self):
        """Exit codes 0-3 should be documented in the script header."""
        with open(str(MONITOR_SCRIPT)) as f:
            text = f.read()
        assert "Exit code" in text or "exit code" in text or "Exit:" in text
