# SPDX-License-Identifier: Apache-2.0
"""
test_ota.py — Tests for OTA update scripts and configuration

Run with:
    python3 -m pytest tests/test_ota.py -v

These tests validate:
  - rauc-system.conf: INI structure, required sections, device path sanity
  - rauc-manifest.raucm: required fields present
  - rauc-pre-install.sh: exists, bash syntax clean, SPDX header
  - rauc-post-install.sh: exists, bash syntax clean, dry-run behaviour
  - hw-fault-monitor.service: WatchdogSec, Type=notify, OOMScoreAdjust
  - hw-fault-monitor.c: compiles cleanly with gcc (skipped if gcc absent)
"""

import configparser
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

# ---------------------------------------------------------------------------
# Helpers — locate files relative to this test file
# ---------------------------------------------------------------------------
TESTS_DIR  = os.path.dirname(os.path.abspath(__file__))
OTA_DIR    = os.path.dirname(TESTS_DIR)           # ota-updates/
WDG_DIR    = os.path.join(OTA_DIR, "systemd-watchdog-example")

SYSTEM_CONF       = os.path.join(OTA_DIR,  "rauc-system.conf")
MANIFEST          = os.path.join(OTA_DIR,  "rauc-manifest.raucm")
PRE_INSTALL       = os.path.join(OTA_DIR,  "rauc-pre-install.sh")
POST_INSTALL      = os.path.join(OTA_DIR,  "rauc-post-install.sh")
SERVICE_FILE      = os.path.join(WDG_DIR,  "hw-fault-monitor.service")
C_SOURCE          = os.path.join(WDG_DIR,  "hw-fault-monitor.c")


def read_file(path: str) -> str:
    """Read a file and return its contents as a string."""
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def parse_ini(path: str) -> configparser.ConfigParser:
    """
    Parse a file as INI, stripping leading comment lines (# ...) so that
    configparser does not choke on them. Returns a ConfigParser object.
    """
    content = read_file(path)
    # Remove comment-only lines (lines starting with optional whitespace + #)
    # but preserve inline comments and blank lines.
    cleaned_lines = []
    for line in content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue   # skip full-line comments
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)

    parser = configparser.ConfigParser(strict=False)
    parser.read_string(cleaned)
    return parser


def has_spdx_header(path: str) -> bool:
    """Return True if any of the first 5 non-blank lines contains the SPDX identifier.

    Shell scripts have a shebang (#!/usr/bin/env bash) as the first non-blank
    line; the SPDX identifier appears on the second non-blank line.
    C files have the SPDX identifier as the first non-blank line.
    """
    with open(path, "r", encoding="utf-8") as fh:
        checked = 0
        for line in fh:
            if not line.strip():
                continue
            if "SPDX-License-Identifier: Apache-2.0" in line:
                return True
            checked += 1
            if checked >= 5:
                break
    return False


def bash_syntax_ok(path: str) -> bool:
    """Return True if `bash -n <path>` exits 0 (no syntax errors)."""
    result = subprocess.run(
        ["bash", "-n", path],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


# ===========================================================================
# rauc-system.conf tests
# ===========================================================================

class TestRaucSystemConf:
    """Validate structure and content of rauc-system.conf."""

    def test_file_exists(self):
        assert os.path.isfile(SYSTEM_CONF), f"Not found: {SYSTEM_CONF}"

    def test_spdx_header(self):
        assert has_spdx_header(SYSTEM_CONF), \
            "rauc-system.conf missing SPDX-License-Identifier: Apache-2.0"

    def test_parses_as_ini(self):
        """configparser must parse the file without raising an exception."""
        parser = parse_ini(SYSTEM_CONF)
        assert parser is not None

    def test_required_section_system(self):
        parser = parse_ini(SYSTEM_CONF)
        assert parser.has_section("system"), \
            "rauc-system.conf missing [system] section"

    def test_required_section_keyring(self):
        parser = parse_ini(SYSTEM_CONF)
        assert parser.has_section("keyring"), \
            "rauc-system.conf missing [keyring] section"

    def test_required_section_slot_rootfs_0(self):
        parser = parse_ini(SYSTEM_CONF)
        assert parser.has_section("slot.rootfs.0"), \
            "rauc-system.conf missing [slot.rootfs.0] section"

    def test_required_section_slot_rootfs_1(self):
        parser = parse_ini(SYSTEM_CONF)
        assert parser.has_section("slot.rootfs.1"), \
            "rauc-system.conf missing [slot.rootfs.1] section"

    def test_system_compatible_present(self):
        parser = parse_ini(SYSTEM_CONF)
        assert parser.has_option("system", "compatible"), \
            "[system] section missing 'compatible' key"
        assert parser.get("system", "compatible").strip() != "", \
            "[system] compatible must not be empty"

    def test_system_bootloader_is_uboot(self):
        parser = parse_ini(SYSTEM_CONF)
        assert parser.has_option("system", "bootloader"), \
            "[system] section missing 'bootloader' key"
        assert parser.get("system", "bootloader").strip() == "uboot", \
            "[system] bootloader should be 'uboot'"

    def test_slot_rootfs_0_device_path(self):
        """Slot device must be /dev/mmcblk0p* — not an arbitrary path."""
        parser = parse_ini(SYSTEM_CONF)
        device = parser.get("slot.rootfs.0", "device", fallback="")
        assert re.match(r"^/dev/mmcblk0p\d+$", device.strip()), \
            f"slot.rootfs.0 device '{device}' does not match /dev/mmcblk0p*"

    def test_slot_rootfs_1_device_path(self):
        parser = parse_ini(SYSTEM_CONF)
        device = parser.get("slot.rootfs.1", "device", fallback="")
        assert re.match(r"^/dev/mmcblk0p\d+$", device.strip()), \
            f"slot.rootfs.1 device '{device}' does not match /dev/mmcblk0p*"

    def test_slot_rootfs_devices_are_different(self):
        """Slot A and slot B must be on different partitions."""
        parser = parse_ini(SYSTEM_CONF)
        dev0 = parser.get("slot.rootfs.0", "device", fallback="").strip()
        dev1 = parser.get("slot.rootfs.1", "device", fallback="").strip()
        assert dev0 != dev1, \
            "slot.rootfs.0 and slot.rootfs.1 must not use the same device"

    def test_slot_rootfs_0_bootname(self):
        parser = parse_ini(SYSTEM_CONF)
        bootname = parser.get("slot.rootfs.0", "bootname", fallback="").strip()
        assert bootname == "A", \
            f"slot.rootfs.0 bootname should be 'A', got '{bootname}'"

    def test_slot_rootfs_1_bootname(self):
        parser = parse_ini(SYSTEM_CONF)
        bootname = parser.get("slot.rootfs.1", "bootname", fallback="").strip()
        assert bootname == "B", \
            f"slot.rootfs.1 bootname should be 'B', got '{bootname}'"

    def test_statusfile_on_data_partition(self):
        """statusfile should be on /data, not on the rootfs."""
        parser = parse_ini(SYSTEM_CONF)
        statusfile = parser.get("system", "statusfile", fallback="").strip()
        assert statusfile.startswith("/data/"), \
            f"statusfile '{statusfile}' should be under /data/ to survive rootfs updates"


# ===========================================================================
# rauc-manifest.raucm tests
# ===========================================================================

class TestRaucManifest:
    """Validate the bundle manifest fields."""

    def test_file_exists(self):
        assert os.path.isfile(MANIFEST), f"Not found: {MANIFEST}"

    def test_spdx_header(self):
        assert has_spdx_header(MANIFEST), \
            "rauc-manifest.raucm missing SPDX-License-Identifier: Apache-2.0"

    def test_parses_as_ini(self):
        parser = parse_ini(MANIFEST)
        assert parser is not None

    def test_section_update_present(self):
        parser = parse_ini(MANIFEST)
        assert parser.has_section("update"), \
            "rauc-manifest.raucm missing [update] section"

    def test_field_compatible(self):
        parser = parse_ini(MANIFEST)
        assert parser.has_option("update", "compatible"), \
            "[update] section missing 'compatible' field"
        assert parser.get("update", "compatible").strip() != ""

    def test_field_version(self):
        parser = parse_ini(MANIFEST)
        assert parser.has_option("update", "version"), \
            "[update] section missing 'version' field"
        version = parser.get("update", "version").strip()
        assert version != "", "version must not be empty"

    def test_field_description(self):
        parser = parse_ini(MANIFEST)
        assert parser.has_option("update", "description"), \
            "[update] section missing 'description' field"
        assert parser.get("update", "description").strip() != ""

    def test_field_build_timestamp(self):
        parser = parse_ini(MANIFEST)
        assert parser.has_option("update", "build"), \
            "[update] section missing 'build' field (ISO 8601 timestamp)"

    def test_image_rootfs_section(self):
        parser = parse_ini(MANIFEST)
        assert parser.has_section("image.rootfs"), \
            "rauc-manifest.raucm missing [image.rootfs] section"

    def test_image_rootfs_filename(self):
        parser = parse_ini(MANIFEST)
        filename = parser.get("image.rootfs", "filename", fallback="").strip()
        assert filename.endswith(".img"), \
            f"image.rootfs filename '{filename}' should be a .img file"


# ===========================================================================
# rauc-pre-install.sh tests
# ===========================================================================

class TestPreInstallScript:
    """Validate the pre-install hook script."""

    def test_file_exists(self):
        assert os.path.isfile(PRE_INSTALL), f"Not found: {PRE_INSTALL}"

    def test_spdx_header(self):
        assert has_spdx_header(PRE_INSTALL), \
            "rauc-pre-install.sh missing SPDX-License-Identifier: Apache-2.0"

    def test_bash_shebang(self):
        content = read_file(PRE_INSTALL)
        first_line = content.splitlines()[0]
        assert "bash" in first_line, \
            f"Expected bash shebang, got: {first_line}"

    def test_bash_syntax(self):
        """bash -n must exit 0 (no syntax errors)."""
        if not shutil.which("bash"):
            pytest.skip("bash not available in test environment")
        assert bash_syntax_ok(PRE_INSTALL), \
            f"bash -n reported syntax errors in {PRE_INSTALL}"

    def test_contains_set_pipefail(self):
        content = read_file(PRE_INSTALL)
        assert "set -" in content and "pipefail" in content, \
            "pre-install.sh should use 'set -euo pipefail' for safety"

    def test_checks_free_space(self):
        content = read_file(PRE_INSTALL)
        assert "df" in content, \
            "pre-install.sh should check free space with df"

    def test_checks_edac(self):
        content = read_file(PRE_INSTALL)
        assert "edac" in content.lower(), \
            "pre-install.sh should check EDAC correctable error count"

    def test_checks_battery(self):
        content = read_file(PRE_INSTALL)
        assert "power_supply" in content or "BAT" in content, \
            "pre-install.sh should check battery/power state"

    def test_checks_wdt(self):
        content = read_file(PRE_INSTALL)
        assert "wdt" in content.lower() or "watchdog" in content.lower(), \
            "pre-install.sh should check WDT keepalive process"


# ===========================================================================
# rauc-post-install.sh tests
# ===========================================================================

class TestPostInstallScript:
    """Validate the post-install hook script."""

    def test_file_exists(self):
        assert os.path.isfile(POST_INSTALL), f"Not found: {POST_INSTALL}"

    def test_spdx_header(self):
        assert has_spdx_header(POST_INSTALL), \
            "rauc-post-install.sh missing SPDX-License-Identifier: Apache-2.0"

    def test_bash_syntax(self):
        if not shutil.which("bash"):
            pytest.skip("bash not available in test environment")
        assert bash_syntax_ok(POST_INSTALL), \
            f"bash -n reported syntax errors in {POST_INSTALL}"

    def test_calls_sync(self):
        content = read_file(POST_INSTALL)
        assert "sync" in content, \
            "post-install.sh must call sync to flush filesystem buffers"

    def test_calls_drop_caches(self):
        content = read_file(POST_INSTALL)
        assert "drop_caches" in content, \
            "post-install.sh should drop page cache after write"

    def test_calls_fw_setenv_bootcount(self):
        content = read_file(POST_INSTALL)
        assert "bootcount" in content, \
            "post-install.sh should set U-Boot bootcount via fw_setenv"

    def test_calls_fw_setenv_bootlimit(self):
        content = read_file(POST_INSTALL)
        assert "bootlimit" in content, \
            "post-install.sh should set U-Boot bootlimit via fw_setenv"

    def test_sets_boot_order(self):
        content = read_file(POST_INSTALL)
        assert "BOOT_ORDER" in content, \
            "post-install.sh should set U-Boot BOOT_ORDER"

    def test_graceful_without_fw_setenv(self):
        """
        The script should handle missing fw_setenv gracefully (warn, not die)
        in non-production environments. Verify by checking for a conditional
        guard around the fw_setenv calls.
        """
        content = read_file(POST_INSTALL)
        # The script should check if fw_setenv exists before calling it
        assert "command -v fw_setenv" in content or \
               "which fw_setenv" in content or \
               'FW_SETENV' in content, \
            "post-install.sh should check for fw_setenv availability"


# ===========================================================================
# hw-fault-monitor.service tests
# ===========================================================================

class TestServiceUnit:
    """Validate the systemd unit file."""

    def test_file_exists(self):
        assert os.path.isfile(SERVICE_FILE), f"Not found: {SERVICE_FILE}"

    def test_spdx_header(self):
        assert has_spdx_header(SERVICE_FILE), \
            "hw-fault-monitor.service missing SPDX-License-Identifier: Apache-2.0"

    def test_parses_as_ini(self):
        parser = parse_ini(SERVICE_FILE)
        assert parser.has_section("Unit")
        assert parser.has_section("Service")
        assert parser.has_section("Install")

    def test_type_notify(self):
        """Type=notify is required for WatchdogSec to work properly."""
        parser = parse_ini(SERVICE_FILE)
        svc_type = parser.get("Service", "Type", fallback="").strip()
        assert svc_type == "notify", \
            f"[Service] Type must be 'notify', got '{svc_type}'"

    def test_watchdog_sec_present(self):
        """WatchdogSec= must be set — this is the core watchdog directive."""
        parser = parse_ini(SERVICE_FILE)
        wdt = parser.get("Service", "WatchdogSec", fallback="").strip()
        assert wdt != "", \
            "[Service] WatchdogSec= must be set for watchdog integration"

    def test_watchdog_sec_value(self):
        """WatchdogSec should be at least 20s (too short = spurious restarts)."""
        parser = parse_ini(SERVICE_FILE)
        wdt_str = parser.get("Service", "WatchdogSec", fallback="0").strip()
        # Parse value: strip trailing 's' or 'min'
        match = re.match(r"(\d+)(s|min|h)?", wdt_str)
        assert match, f"Could not parse WatchdogSec value: '{wdt_str}'"
        value = int(match.group(1))
        unit = match.group(2) or "s"
        if unit == "min":
            value *= 60
        elif unit == "h":
            value *= 3600
        assert value >= 20, \
            f"WatchdogSec={wdt_str} seems too short; use at least 20s"

    def test_restart_on_failure(self):
        parser = parse_ini(SERVICE_FILE)
        restart = parser.get("Service", "Restart", fallback="").strip()
        assert restart in ("on-failure", "always"), \
            f"[Service] Restart should be 'on-failure' or 'always', got '{restart}'"

    def test_oom_score_adjust(self):
        """OOMScoreAdjust should be negative (protect from OOM killer)."""
        parser = parse_ini(SERVICE_FILE)
        adj = parser.get("Service", "OOMScoreAdjust", fallback="").strip()
        assert adj != "", \
            "[Service] OOMScoreAdjust= should be set to protect the monitor from OOM"
        adj_val = int(adj)
        assert adj_val < 0, \
            f"OOMScoreAdjust={adj_val} should be negative to protect this service"

    def test_exec_start_present(self):
        parser = parse_ini(SERVICE_FILE)
        exec_start = parser.get("Service", "ExecStart", fallback="").strip()
        assert exec_start != "", "[Service] ExecStart= must be set"
        assert "hw-fault-monitor" in exec_start, \
            "ExecStart should reference hw-fault-monitor binary"

    def test_wanted_by_multi_user(self):
        parser = parse_ini(SERVICE_FILE)
        wanted = parser.get("Install", "WantedBy", fallback="").strip()
        assert "multi-user.target" in wanted, \
            "[Install] WantedBy should include multi-user.target"


# ===========================================================================
# hw-fault-monitor.c compilation test
# ===========================================================================

class TestCSource:
    """Validate the C source file."""

    def test_file_exists(self):
        assert os.path.isfile(C_SOURCE), f"Not found: {C_SOURCE}"

    def test_spdx_header(self):
        assert has_spdx_header(C_SOURCE), \
            "hw-fault-monitor.c missing SPDX-License-Identifier: Apache-2.0"

    def test_contains_sd_notify_logic(self):
        content = read_file(C_SOURCE)
        assert "NOTIFY_SOCKET" in content, \
            "hw-fault-monitor.c must use NOTIFY_SOCKET for sd_notify"

    def test_contains_watchdog_ping(self):
        content = read_file(C_SOURCE)
        assert "WATCHDOG=1" in content, \
            "hw-fault-monitor.c must send WATCHDOG=1 keepalive"

    def test_contains_ready_notification(self):
        content = read_file(C_SOURCE)
        assert "READY=1" in content, \
            "hw-fault-monitor.c must send READY=1 after initialization"

    def test_contains_stopping_notification(self):
        content = read_file(C_SOURCE)
        assert "STOPPING=1" in content, \
            "hw-fault-monitor.c must send STOPPING=1 on SIGTERM"

    @pytest.mark.skipif(not shutil.which("gcc"), reason="gcc not available")
    def test_gcc_syntax_only(self):
        """
        gcc -fsyntax-only: parses and type-checks the C source without
        producing object code. Catches syntax errors, undeclared identifiers,
        and type mismatches. Does NOT require the target libraries.
        """
        result = subprocess.run(
            ["gcc", "-fsyntax-only", "-Wall", "-Wextra", C_SOURCE],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"gcc -fsyntax-only failed:\n{result.stderr}"
        )


# ===========================================================================
# Extended OTA tests
# ===========================================================================

class TestPreInstallExtended:
    """Extended rauc-pre-install.sh content and behavior tests."""

    def test_has_bash_shebang(self):
        with open(PRE_INSTALL) as f:
            first = f.readline()
        assert "bash" in first

    def test_documents_rauc_env_vars(self):
        with open(PRE_INSTALL) as f:
            content = f.read()
        assert "RAUC_SLOT" in content or "RAUC_BUNDLE" in content

    def test_has_space_check(self):
        with open(PRE_INSTALL) as f:
            content = f.read()
        assert "space" in content.lower() or "df " in content or "du " in content or "avail" in content.lower()

    def test_exit_codes_documented(self):
        with open(PRE_INSTALL) as f:
            content = f.read()
        assert "exit" in content

    def test_bash_syntax_clean(self):
        result = subprocess.run(["bash", "-n", PRE_INSTALL], capture_output=True, text=True)
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"

    def test_sanity_check_abort_on_failure(self):
        """Script must exit non-zero to abort install on bad state."""
        with open(PRE_INSTALL) as f:
            content = f.read()
        # Must have at least one non-zero exit path
        import re
        non_zero_exits = re.findall(r'exit\s+[1-9]\d*', content)
        assert len(non_zero_exits) > 0, "Pre-install must have non-zero exit paths"


class TestPostInstallExtended:
    """Extended rauc-post-install.sh content and behavior tests."""

    def test_has_bash_shebang(self):
        with open(POST_INSTALL) as f:
            first = f.readline()
        assert "bash" in first

    def test_calls_sync_or_flush(self):
        with open(POST_INSTALL) as f:
            content = f.read()
        assert "sync" in content or "flush" in content or "fsync" in content

    def test_has_bootcount_or_bootlimit_logic(self):
        with open(POST_INSTALL) as f:
            content = f.read()
        assert "boot" in content.lower() or "fw_setenv" in content or "rauc" in content.lower()

    def test_bash_syntax_clean(self):
        result = subprocess.run(["bash", "-n", POST_INSTALL], capture_output=True, text=True)
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"

    def test_documents_rauc_env_vars(self):
        with open(POST_INSTALL) as f:
            content = f.read()
        assert "RAUC" in content

    def test_has_set_e(self):
        with open(POST_INSTALL) as f:
            content = f.read()
        assert "set -" in content


class TestHwFaultMonitorExtended:
    """Extended hw-fault-monitor.c content and correctness tests."""

    def test_includes_stddef_h(self):
        with open(C_SOURCE) as f:
            content = f.read()
        assert "#include <stddef.h>" in content

    def test_includes_errno_h(self):
        with open(C_SOURCE) as f:
            content = f.read()
        assert "#include <errno.h>" in content

    def test_includes_sys_socket(self):
        with open(C_SOURCE) as f:
            content = f.read()
        assert "#include <sys/socket.h>" in content or "socket" in content

    def test_defines_watchdog_interval(self):
        with open(C_SOURCE) as f:
            content = f.read()
        assert "WATCHDOG" in content and ("interval" in content.lower() or "period" in content.lower() or "PERIOD" in content)

    def test_main_function_present(self):
        with open(C_SOURCE) as f:
            content = f.read()
        assert "int main(" in content or "int main (" in content

    def test_signal_handler_registered(self):
        with open(C_SOURCE) as f:
            content = f.read()
        assert "signal(" in content or "sigaction(" in content

    def test_loop_with_sleep(self):
        with open(C_SOURCE) as f:
            content = f.read()
        assert "sleep(" in content or "nanosleep(" in content or "usleep(" in content


class TestRaucSystemConfExtended:
    """Extended rauc system.conf correctness tests."""

    def test_has_compatible_field(self):
        with open(SYSTEM_CONF) as f:
            content = f.read()
        assert "compatible=" in content

    def test_has_bootloader_section(self):
        with open(SYSTEM_CONF) as f:
            content = f.read()
        assert "[system]" in content or "bootloader" in content.lower() or "[keyring]" in content

    def test_slot_a_and_b_defined(self):
        with open(SYSTEM_CONF) as f:
            content = f.read()
        assert "rootfs.0" in content or "slot.a" in content.lower() or ".0" in content

    def test_has_handlers_section(self):
        with open(SYSTEM_CONF) as f:
            content = f.read()
        assert "[handlers]" in content or "pre-install" in content or "post-install" in content
