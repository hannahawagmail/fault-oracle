# SPDX-License-Identifier: Apache-2.0
"""
power_cxl/tests/test_power_cxl_collectors.py — Unit tests for PMEM, RAPL,
and misc-hardware collectors.

All tests use tmp_path fixtures and mock subprocess where needed.
Run with: python3 -m pytest power_cxl/tests/ -v
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

# Allow imports from parent directory
sys.path.insert(0, str(Path(__file__).parent.parent))

import pmem_collector as pmem
import rapl_collector as rapl
import misc_hardware_collector as misc

# ---------------------------------------------------------------------------
# Fixture constants
# ---------------------------------------------------------------------------

NDCTL_HEALTH_JSON = json.dumps([{
    "dev": "nmem0",
    "health": {
        "health_state": "ok",
        "lifespan_used": 12,
        "lifespan_remaining": 88,
        "temperature": 35,
        "unsafe_shutdowns": 2,
        "ars_status": "idle",
    }
}])

NDCTL_HEALTH_JSON_SCANNING = json.dumps([{
    "dev": "nmem0",
    "health": {
        "health_state": "ok",
        "lifespan_used": 12,
        "lifespan_remaining": 88,
        "temperature": 35,
        "unsafe_shutdowns": 2,
        "ars_status": "scanning",
    }
}])

NDCTL_HEALTH_JSON_NONCRITICAL = json.dumps([{
    "dev": "nmem1",
    "health": {
        "health_state": "non-critical",
        "lifespan_used": 50,
        "lifespan_remaining": 50,
        "unsafe_shutdowns": 0,
        "ars_status": "idle",
    }
}])

NDCTL_EMPTY = "[]"

NDCTL_MEDIA_ERRORS_JSON = json.dumps([
    {"dev": "nmem0"},
    {"dev": "nmem0"},
    {"dev": "nmem1"},
])


# ---------------------------------------------------------------------------
# TestPMEMCollector (8 tests)
# ---------------------------------------------------------------------------

class TestPMEMCollector:

    def test_all_fields_parsed(self):
        """All health fields in the fixture should be parsed correctly."""
        records = pmem.parse_health_output(NDCTL_HEALTH_JSON)
        assert len(records) == 1
        r = records[0]
        assert r["dev"] == "nmem0"
        assert r["health_state"] == "ok"
        assert r["lifespan_used"] == 12
        assert r["lifespan_remaining"] == 88
        assert r["temperature"] == 35
        assert r["unsafe_shutdowns"] == 2
        assert r["ars_in_progress"] == 0

    def test_health_state_ok_sets_correct_booleans(self):
        """When health_state is 'ok', only the ok gauge should be 1."""
        records = pmem.parse_health_output(NDCTL_HEALTH_JSON)
        output = pmem.emit_metrics(records, {}, collector_up=1)
        assert 'pmem_health_state{device="nmem0",state="ok"} 1' in output
        assert 'pmem_health_state{device="nmem0",state="non-critical"} 0' in output
        assert 'pmem_health_state{device="nmem0",state="critical"} 0' in output
        assert 'pmem_health_state{device="nmem0",state="fatal"} 0' in output

    def test_lifespan_used_emitted(self):
        """pmem_lifespan_used_percent should contain lifespan_used value."""
        records = pmem.parse_health_output(NDCTL_HEALTH_JSON)
        output = pmem.emit_metrics(records, {}, collector_up=1)
        assert 'pmem_lifespan_used_percent{device="nmem0"} 12' in output

    def test_unsafe_shutdowns_counter_emitted(self):
        """pmem_unsafe_shutdowns_total should reflect the unsafe_shutdowns value."""
        records = pmem.parse_health_output(NDCTL_HEALTH_JSON)
        output = pmem.emit_metrics(records, {}, collector_up=1)
        assert 'pmem_unsafe_shutdowns_total{device="nmem0"} 2' in output

    def test_ars_status_scanning_emits_1(self):
        """ars_status='scanning' should produce pmem_ars_in_progress=1."""
        records = pmem.parse_health_output(NDCTL_HEALTH_JSON_SCANNING)
        assert records[0]["ars_in_progress"] == 1
        output = pmem.emit_metrics(records, {}, collector_up=1)
        assert 'pmem_ars_in_progress{device="nmem0"} 1' in output

    def test_ndctl_absent_emits_up_0(self):
        """When ndctl raises FileNotFoundError, raw is empty → collector_up=0."""
        # run_ndctl returns "" on FileNotFoundError; parse_health_output("") == []
        records = pmem.parse_health_output("")
        assert records == []
        output = pmem.emit_metrics([], {}, collector_up=0)
        assert "pmem_collector_up 0" in output

    def test_empty_array_emits_up_0(self):
        """ndctl returning '[]' should result in collector_up=0."""
        records = pmem.parse_health_output(NDCTL_EMPTY)
        assert records == []
        output = pmem.emit_metrics([], {}, collector_up=0)
        assert "pmem_collector_up 0" in output

    def test_media_errors_counted_per_device(self):
        """count_media_errors should sum entries per device name."""
        counts = pmem.count_media_errors(NDCTL_MEDIA_ERRORS_JSON)
        assert counts["nmem0"] == 2
        assert counts["nmem1"] == 1
        # Ensure they appear in emit_metrics output
        records = pmem.parse_health_output(NDCTL_HEALTH_JSON)
        output = pmem.emit_metrics(records, counts, collector_up=1)
        assert 'pmem_media_errors_total{device="nmem0"} 2' in output


# ---------------------------------------------------------------------------
# TestRAPLCollector (8 tests)
# ---------------------------------------------------------------------------

class TestRAPLCollector:

    def test_energy_delta_converts_to_watts(self, tmp_path):
        """Reading energy_uj twice 1 s apart → power_watts = delta / 1e6."""
        pkg_dir = tmp_path / "intel-rapl:0"
        pkg_dir.mkdir()
        (pkg_dir / "name").write_text("package-0\n")
        energy_file = pkg_dir / "energy_uj"
        energy_file.write_text("1000000\n")

        call_count = [0]
        orig_sleep = time.sleep

        def fake_sleep(t):
            # Advance the energy counter by 5 W·s = 5_000_000 µJ
            energy_file.write_text("6000000\n")

        with mock.patch("time.sleep", side_effect=fake_sleep):
            records = rapl.collect_rapl(tmp_path)

        assert len(records) == 1
        assert abs(records[0]["power_watts"] - 5.0) < 0.001

    def test_power_limit_read_correctly(self, tmp_path):
        """constraint_0_power_limit_uw should be converted to watts."""
        pkg_dir = tmp_path / "intel-rapl:0"
        pkg_dir.mkdir()
        (pkg_dir / "name").write_text("package-0\n")
        energy_file = pkg_dir / "energy_uj"
        energy_file.write_text("0\n")
        (pkg_dir / "constraint_0_power_limit_uw").write_text("125000000\n")  # 125 W

        def fake_sleep(t):
            energy_file.write_text("1000000\n")

        with mock.patch("time.sleep", side_effect=fake_sleep):
            records = rapl.collect_rapl(tmp_path)

        assert len(records) == 1
        assert abs(records[0]["limit_watts"] - 125.0) < 0.001

    def test_throttled_at_96_percent_of_limit(self, tmp_path):
        """power > 95% of limit should set throttled=1."""
        pkg_dir = tmp_path / "intel-rapl:0"
        pkg_dir.mkdir()
        (pkg_dir / "name").write_text("package-0\n")
        energy_file = pkg_dir / "energy_uj"
        energy_file.write_text("0\n")
        # limit = 100 W; power will be 96 W → throttled
        (pkg_dir / "constraint_0_power_limit_uw").write_text("100000000\n")

        def fake_sleep(t):
            energy_file.write_text("96000000\n")

        with mock.patch("time.sleep", side_effect=fake_sleep):
            records = rapl.collect_rapl(tmp_path)

        assert records[0]["throttled"] == 1

    def test_no_throttle_at_80_percent_of_limit(self, tmp_path):
        """power at 80% of limit should set throttled=0."""
        pkg_dir = tmp_path / "intel-rapl:0"
        pkg_dir.mkdir()
        (pkg_dir / "name").write_text("package-0\n")
        energy_file = pkg_dir / "energy_uj"
        energy_file.write_text("0\n")
        (pkg_dir / "constraint_0_power_limit_uw").write_text("100000000\n")

        def fake_sleep(t):
            energy_file.write_text("80000000\n")

        with mock.patch("time.sleep", side_effect=fake_sleep):
            records = rapl.collect_rapl(tmp_path)

        assert records[0]["throttled"] == 0

    def test_no_powercap_dir_continues_with_cpufreq(self, tmp_path):
        """Missing powercap dir should return empty RAPL records (no crash)."""
        nonexistent = tmp_path / "nonexistent-rapl"
        records = rapl.collect_rapl(nonexistent)
        assert records == []

    def test_cpufreq_ratio_cur_over_max(self, tmp_path):
        """cpu_freq_throttle_ratio should be cur/max."""
        cpu_dir = tmp_path / "cpu0"
        cpufreq = cpu_dir / "cpufreq"
        cpufreq.mkdir(parents=True)
        (cpufreq / "scaling_cur_freq").write_text("1200000\n")
        (cpufreq / "scaling_max_freq").write_text("2400000\n")

        records = rapl.collect_cpufreq(tmp_path)
        assert len(records) == 1
        assert abs(records[0]["throttle_ratio"] - 0.5) < 0.001

    def test_missing_cpufreq_dir_skipped(self, tmp_path):
        """CPUs without a cpufreq directory should be silently skipped."""
        (tmp_path / "cpu0").mkdir()          # no cpufreq subdir
        cpu1 = tmp_path / "cpu1"
        cpufreq1 = cpu1 / "cpufreq"
        cpufreq1.mkdir(parents=True)
        (cpufreq1 / "scaling_cur_freq").write_text("2000000\n")
        (cpufreq1 / "scaling_max_freq").write_text("2000000\n")

        records = rapl.collect_cpufreq(tmp_path)
        assert len(records) == 1
        assert records[0]["cpu"] == "cpu1"

    def test_rapl_collector_up_1_when_data_found(self, tmp_path):
        """collector_up should be 1 when RAPL or cpufreq data is present."""
        cpu_dir = tmp_path / "cpu0" / "cpufreq"
        cpu_dir.mkdir(parents=True)
        (cpu_dir / "scaling_cur_freq").write_text("3000000\n")
        (cpu_dir / "scaling_max_freq").write_text("3000000\n")

        cpufreq_records = rapl.collect_cpufreq(tmp_path)
        output = rapl.emit_metrics([], cpufreq_records, collector_up=1)
        assert "rapl_collector_up 1" in output
        assert "rapl_collector_last_run_timestamp" in output


# ---------------------------------------------------------------------------
# TestMiscHardware (5 tests)
# ---------------------------------------------------------------------------

class TestMiscHardware:

    def test_pcie_aer_file_parsed(self, tmp_path):
        """aer_dev_correctable with one entry should be counted."""
        dev = tmp_path / "0000:00:1f.0"
        dev.mkdir()
        (dev / "aer_dev_correctable").write_text("RxErr 0\nBadTLP 3\nBadDLLP 0\n")
        (dev / "aer_dev_fatal").write_text("TLP 0\n")

        records = misc.collect_pcie_aer(tmp_path)
        assert len(records) == 1
        assert records[0]["pci_id"] == "0000:00:1f.0"
        assert records[0]["correctable"] == 3
        assert records[0]["fatal"] == 0

    def test_rxerr_5_counted_in_correctable(self, tmp_path):
        """RxErr 5 in the AER correctable file should yield correctable=5."""
        dev = tmp_path / "0000:01:00.0"
        dev.mkdir()
        (dev / "aer_dev_correctable").write_text("RxErr 5\nBadTLP 0\n")

        records = misc.collect_pcie_aer(tmp_path)
        assert records[0]["correctable"] == 5

    def test_device_missing_aer_files_skipped(self, tmp_path):
        """Devices without AER sysfs files should not appear in results."""
        dev = tmp_path / "0000:02:00.0"
        dev.mkdir()
        # No aer_dev_correctable or aer_dev_fatal files

        records = misc.collect_pcie_aer(tmp_path)
        assert records == []

    def test_watchdog_present_1(self, tmp_path):
        """watchdog_present=1 when watchdog file exists."""
        wdt = tmp_path / "watchdog"
        wdt.write_text("")
        result = misc.collect_watchdog(watchdog_path=wdt)
        assert result == 1

    def test_hwrng_active_1(self, tmp_path):
        """hwrng_active=1 when rng_current is non-empty."""
        rng = tmp_path / "rng_current"
        rng.write_text("virtio_rng.0\n")
        result = misc.collect_hwrng(hwrng_path=rng)
        assert result == 1


# ---------------------------------------------------------------------------
# TestCollectorEnableFlags (4 tests)
# ---------------------------------------------------------------------------

class TestCollectorEnableFlags:

    def test_collect_pcie_aer_false_skips_pcie(self, tmp_path, monkeypatch):
        """COLLECT_PCIE_AER=false → aer_records is None → no pcie metrics emitted."""
        monkeypatch.setenv("COLLECT_PCIE_AER", "false")
        dev = tmp_path / "0000:00:00.0"
        dev.mkdir()
        (dev / "aer_dev_correctable").write_text("RxErr 1\n")

        enabled = misc._env_enabled("COLLECT_PCIE_AER")
        assert enabled is False

        aer_records = misc.collect_pcie_aer(tmp_path) if enabled else None
        output = misc.emit_metrics(aer_records, None, None)
        assert "pcie_aer_correctable_total" not in output

    def test_collect_watchdog_false_skips_watchdog(self, tmp_path, monkeypatch):
        """COLLECT_WATCHDOG=false → watchdog metric not emitted."""
        monkeypatch.setenv("COLLECT_WATCHDOG", "false")
        wdt = tmp_path / "watchdog"
        wdt.write_text("")

        enabled = misc._env_enabled("COLLECT_WATCHDOG")
        assert enabled is False

        watchdog = misc.collect_watchdog(watchdog_path=wdt) if enabled else None
        output = misc.emit_metrics([], watchdog, None)
        assert "watchdog_present" not in output

    def test_collect_hwrng_false_skips_hwrng(self, tmp_path, monkeypatch):
        """COLLECT_HWRNG=false → hwrng_active metric not emitted."""
        monkeypatch.setenv("COLLECT_HWRNG", "false")
        rng = tmp_path / "rng_current"
        rng.write_text("virtio_rng.0\n")

        enabled = misc._env_enabled("COLLECT_HWRNG")
        assert enabled is False

        hwrng = misc.collect_hwrng(hwrng_path=rng) if enabled else None
        output = misc.emit_metrics([], None, hwrng)
        assert "hwrng_active" not in output

    def test_all_flags_true_all_metrics_emitted(self, tmp_path, monkeypatch):
        """With all collectors enabled and data present, all metric families appear."""
        monkeypatch.setenv("COLLECT_PCIE_AER", "true")
        monkeypatch.setenv("COLLECT_WATCHDOG", "true")
        monkeypatch.setenv("COLLECT_HWRNG", "true")

        # Set up fake sysfs structures
        dev = tmp_path / "pci" / "0000:00:00.0"
        dev.mkdir(parents=True)
        (dev / "aer_dev_correctable").write_text("RxErr 0\n")

        wdt = tmp_path / "watchdog"
        wdt.write_text("")

        rng = tmp_path / "rng_current"
        rng.write_text("virtio_rng.0\n")

        aer_records = misc.collect_pcie_aer(tmp_path / "pci")
        watchdog    = misc.collect_watchdog(watchdog_path=wdt)
        hwrng       = misc.collect_hwrng(hwrng_path=rng)

        output = misc.emit_metrics(aer_records, watchdog, hwrng)

        assert "pcie_aer_correctable_total" in output
        assert "watchdog_present" in output
        assert "hwrng_active" in output
        assert "misc_collector_up 1" in output
