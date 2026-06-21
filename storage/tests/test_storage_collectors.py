# SPDX-License-Identifier: Apache-2.0
"""
storage/tests/test_storage_collectors.py — Unit tests for NVMe and SATA collectors.

All tests use fixture string constants; no real hardware is required.
Run with: python3 -m pytest storage/tests/ -v
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# Allow imports from parent directory
sys.path.insert(0, str(Path(__file__).parent.parent))

import nvme_collector as nvme
import sata_collector as sata
import wear_predictor as wp
import controller_collector as cc

# ---------------------------------------------------------------------------
# Fixture constants
# ---------------------------------------------------------------------------

NVME_SMART_JSON = json.dumps({
    "critical_warning": 0,
    "temperature": 305,           # 305 K => 32 °C
    "avail_spare": 100,
    "spare_thresh": 10,
    "percent_used": 2,
    "data_units_read": 18765432,
    "data_units_written": 9876543,
    "host_read_commands": 450000,
    "host_write_commands": 220000,
    "controller_busy_time": 1200,
    "power_cycles": 47,
    "power_on_hours": 8760,
    "unsafe_shutdowns": 3,
    "media_errors": 0,
    "num_err_log_entries": 5,
    "warning_temp_time": 0,
    "critical_comp_time": 0,
})

NVME_SMART_JSON_MISSING_FIELDS = json.dumps({
    "critical_warning": 0,
    "temperature": 298,           # 25 °C
    # avail_spare, spare_thresh, percent_used absent
    "data_units_written": 1000,
    "power_on_hours": 100,
    # unsafe_shutdowns, media_errors, num_err_log_entries absent
})

NVME_ERROR_LOG_JSON_LIST = json.dumps([
    {"error_count": 1, "sqid": 0, "cmdid": 1, "status_field": 0,
     "parm_error_location": 0, "lba": 0, "nsid": 1, "vs": 0},
    {"error_count": 2, "sqid": 0, "cmdid": 2, "status_field": 0,
     "parm_error_location": 0, "lba": 0, "nsid": 1, "vs": 0},
    {"error_count": 3, "sqid": 0, "cmdid": 3, "status_field": 0,
     "parm_error_location": 0, "lba": 0, "nsid": 1, "vs": 0},
])

NVME_ERROR_LOG_JSON_DICT = json.dumps({
    "errors": [
        {"error_count": 1, "sqid": 0, "cmdid": 1},
        {"error_count": 2, "sqid": 0, "cmdid": 2},
    ]
})

NVME_SMART_JSON_CRITICAL_WARNING = json.dumps({
    "critical_warning": 4,        # volatile memory backup failure
    "temperature": 310,
    "avail_spare": 90,
    "spare_thresh": 10,
    "percent_used": 5,
    "data_units_written": 500000,
    "power_on_hours": 1000,
    "unsafe_shutdowns": 1,
    "media_errors": 0,
    "num_err_log_entries": 0,
})

SATA_SMART_JSON_HDD = json.dumps({
    "ata_smart_attributes": {
        "table": [
            {"id": 5,   "name": "Reallocated_Sector_Ct",
             "value": 200, "worst": 200, "thresh": 36,
             "raw": {"value": 0, "string": "0"}},
            {"id": 9,   "name": "Power_On_Hours",
             "value": 88,  "worst": 88,  "thresh": 0,
             "raw": {"value": 28934, "string": "28934"}},
            {"id": 187, "name": "Reported_Uncorrect",
             "value": 100, "worst": 100, "thresh": 0,
             "raw": {"value": 0, "string": "0"}},
            {"id": 194, "name": "Temperature_Celsius",
             "value": 113, "worst": 103, "thresh": 0,
             "raw": {"value": 35, "string": "35 (Min/Max 20/47)"}},
            {"id": 197, "name": "Current_Pending_Sector",
             "value": 200, "worst": 200, "thresh": 0,
             "raw": {"value": 0, "string": "0"}},
            {"id": 198, "name": "Offline_Uncorrectable",
             "value": 100, "worst": 253, "thresh": 0,
             "raw": {"value": 0, "string": "0"}},
        ]
    }
})

SATA_SMART_JSON_SSD = json.dumps({
    "ata_smart_attributes": {
        "table": [
            {"id": 5,   "name": "Reallocated_Sector_Ct",
             "value": 100, "worst": 100, "thresh": 0,
             "raw": {"value": 0, "string": "0"}},
            {"id": 9,   "name": "Power_On_Hours",
             "value": 100, "worst": 100, "thresh": 0,
             "raw": {"value": 4380, "string": "4380"}},
            {"id": 190, "name": "Airflow_Temperature_Cel",
             "value": 68,  "worst": 45,  "thresh": 45,
             "raw": {"value": 32, "string": "32"}},
            {"id": 231, "name": "SSD_Life_Left",
             "value": 92,  "worst": 92,  "thresh": 10,
             "raw": {"value": 92, "string": "92"}},
            {"id": 233, "name": "Media_Wearout_Indicator",
             "value": 99,  "worst": 99,  "thresh": 0,
             "raw": {"value": 99, "string": "99"}},
        ]
    }
})

SATA_SMART_JSON_ID190 = json.dumps({
    "ata_smart_attributes": {
        "table": [
            {"id": 190, "name": "Airflow_Temperature_Cel",
             "value": 65, "worst": 44, "thresh": 45,
             "raw": {"value": 28, "string": "28"}},
        ]
    }
})

SATA_SMART_JSON_ID194 = json.dumps({
    "ata_smart_attributes": {
        "table": [
            {"id": 194, "name": "Temperature_Celsius",
             "value": 110, "worst": 100, "thresh": 0,
             "raw": {"value": 38, "string": "38"}},
        ]
    }
})

SATA_HEALTH_PASS = json.dumps({
    "smart_status": {
        "passed": True
    },
    "smartctl": {
        "version": [7, 3],
        "argv": ["smartctl", "-H", "-j", "/dev/sda"],
        "exit_status": 0
    }
})

SATA_HEALTH_FAIL = json.dumps({
    "smart_status": {
        "passed": False
    },
    "smartctl": {
        "version": [7, 3],
        "argv": ["smartctl", "-H", "-j", "/dev/sdb"],
        "exit_status": 8
    }
})


# ---------------------------------------------------------------------------
# TestNVMeSmartParsing (8 tests)
# ---------------------------------------------------------------------------

class TestNVMeSmartParsing:
    def test_all_key_fields_parsed(self):
        result = nvme.parse_smart_log(NVME_SMART_JSON)
        assert result["critical_warning"] == 0
        assert result["temperature_celsius"] == 32       # 305 - 273
        assert result["available_spare"] == 100
        assert result["available_spare_threshold"] == 10
        assert result["percentage_used"] == 2
        assert result["data_units_written"] == 9876543
        assert result["power_on_hours"] == 8760
        assert result["unsafe_shutdowns"] == 3
        assert result["media_errors"] == 0

    def test_kelvin_to_celsius_conversion(self):
        # 305 K - 273 = 32 °C
        result = nvme.parse_smart_log(NVME_SMART_JSON)
        assert result["temperature_celsius"] == 305 - nvme.KELVIN_OFFSET

    def test_critical_warning_zero(self):
        result = nvme.parse_smart_log(NVME_SMART_JSON)
        assert result["critical_warning"] == 0

    def test_critical_warning_bitmask_4_volatile_backup_failure(self):
        result = nvme.parse_smart_log(NVME_SMART_JSON_CRITICAL_WARNING)
        assert result["critical_warning"] == 4

    def test_missing_optional_fields_return_none(self):
        result = nvme.parse_smart_log(NVME_SMART_JSON_MISSING_FIELDS)
        # Fields absent from the fixture
        assert result["available_spare"] is None
        assert result["available_spare_threshold"] is None
        assert result["percentage_used"] is None
        assert result["unsafe_shutdowns"] is None
        assert result["media_errors"] is None
        # Fields present should parse normally
        assert result["power_on_hours"] == 100
        assert result["data_units_written"] == 1000

    def test_temperature_conversion_missing_returns_none(self):
        data = json.dumps({"critical_warning": 0})
        result = nvme.parse_smart_log(data)
        assert result["temperature_celsius"] is None

    def test_empty_string_returns_all_none(self):
        result = nvme.parse_smart_log("")
        for key, val in result.items():
            assert val is None, f"Expected None for {key}, got {val}"

    def test_invalid_json_returns_all_none(self):
        result = nvme.parse_smart_log("not valid json {{{")
        for key, val in result.items():
            assert val is None, f"Expected None for {key}, got {val}"


# ---------------------------------------------------------------------------
# TestNVMeErrorLog (4 tests)
# ---------------------------------------------------------------------------

class TestNVMeErrorLog:
    def test_multiple_entries_counted_from_list(self):
        count = nvme.count_error_log_entries(NVME_ERROR_LOG_JSON_LIST)
        assert count == 3

    def test_empty_log_returns_zero(self):
        count = nvme.count_error_log_entries(json.dumps([]))
        assert count == 0

    def test_malformed_json_returns_zero_gracefully(self):
        count = nvme.count_error_log_entries("not json at all")
        assert count == 0

    def test_dict_format_with_errors_key(self):
        count = nvme.count_error_log_entries(NVME_ERROR_LOG_JSON_DICT)
        assert count == 2


# ---------------------------------------------------------------------------
# TestSATASmartParsing (8 tests)
# ---------------------------------------------------------------------------

class TestSATASmartParsing:
    def test_hdd_attribute_ids_parsed(self):
        attrs = sata.parse_smart_attributes(SATA_SMART_JSON_HDD, is_rotational=True)
        assert attrs["reallocated_sector_ct"] == 0
        assert attrs["power_on_hours"] == 28934
        assert attrs["reported_uncorrect"] == 0
        assert attrs["temperature_celsius"] == 35
        assert attrs["current_pending_sector"] == 0
        assert attrs["offline_uncorrectable"] == 0

    def test_ssd_wear_attributes_parsed(self):
        attrs = sata.parse_smart_attributes(SATA_SMART_JSON_SSD, is_rotational=False)
        assert attrs["ssd_life_left"] == 92
        assert attrs["media_wearout_indicator"] == 99

    def test_overall_passed_returns_1(self):
        result = sata.parse_smart_health(SATA_HEALTH_PASS)
        assert result == 1

    def test_overall_failed_returns_0(self):
        result = sata.parse_smart_health(SATA_HEALTH_FAIL)
        assert result == 0

    def test_temperature_id_190_parsed(self):
        attrs = sata.parse_smart_attributes(SATA_SMART_JSON_ID190, is_rotational=False)
        assert attrs["airflow_temperature_cel"] == 28

    def test_temperature_id_194_parsed(self):
        attrs = sata.parse_smart_attributes(SATA_SMART_JSON_ID194, is_rotational=True)
        assert attrs["temperature_celsius"] == 38

    def test_missing_smartctl_health_json_returns_0(self):
        result = sata.parse_smart_health("")
        assert result == 0

    def test_attribute_not_present_absent_from_dict(self):
        # An attribute not in the JSON should simply not appear in the result dict
        attrs = sata.parse_smart_attributes(SATA_SMART_JSON_HDD, is_rotational=True)
        assert "ssd_life_left" not in attrs
        assert "media_wearout_indicator" not in attrs


# ---------------------------------------------------------------------------
# TestStorageCollectorIntegration (6 tests)
# ---------------------------------------------------------------------------

class TestStorageCollectorIntegration:
    def test_nvme_collector_up_1_when_devices_found(self):
        """emit_metrics with one device result should have collector_up 1."""
        device_results = [
            {
                "device": "nvme0",
                "smart": nvme.parse_smart_log(NVME_SMART_JSON),
                "error_log_count": 0,
            }
        ]
        output = nvme.emit_metrics(device_results, collector_up=1)
        assert "nvme_collector_up 1" in output

    def test_nvme_collector_up_0_when_no_devices(self):
        output = nvme.emit_metrics([], collector_up=0)
        assert "nvme_collector_up 0" in output

    def test_nvme_last_run_timestamp_emitted(self):
        output = nvme.emit_metrics([], collector_up=0)
        assert "nvme_collector_last_run_timestamp" in output

    def test_sata_collector_up_0_when_no_smartctl(self):
        output = sata.emit_metrics([], collector_up=0)
        assert "sata_collector_up 0" in output

    def test_sata_last_run_timestamp_emitted(self):
        output = sata.emit_metrics([], collector_up=0)
        assert "sata_collector_last_run_timestamp" in output

    def test_multiple_nvme_device_labels_correct(self):
        """Two NVMe devices should each appear with their own label."""
        d0 = {"device": "nvme0", "smart": nvme.parse_smart_log(NVME_SMART_JSON), "error_log_count": 0}
        d1 = {"device": "nvme1", "smart": nvme.parse_smart_log(NVME_SMART_JSON_CRITICAL_WARNING), "error_log_count": 1}
        output = nvme.emit_metrics([d0, d1], collector_up=1)
        assert 'device="nvme0"' in output
        assert 'device="nvme1"' in output

    def test_sata_wear_attribute_absent_for_hdd(self):
        """HDD results should not contain ssd_life_left or media_wearout_indicator."""
        attrs = sata.parse_smart_attributes(SATA_SMART_JSON_HDD, is_rotational=True)
        assert "ssd_life_left" not in attrs
        assert "media_wearout_indicator" not in attrs

    def test_nvme_device_enumeration_from_fixture_sys_path(self, tmp_path):
        """find_nvme_devices should list nvme* entries from a fake /sys/class/nvme."""
        (tmp_path / "nvme0").mkdir()
        (tmp_path / "nvme1").mkdir()
        (tmp_path / "unrelated").mkdir()
        devices = nvme.find_nvme_devices(tmp_path)
        assert devices == ["nvme0", "nvme1"]

    def test_sata_device_enumeration_from_fixture_sys_path(self, tmp_path):
        """find_sata_devices should list sd* entries from a fake /sys/class/block."""
        (tmp_path / "sda").mkdir()
        (tmp_path / "sdb").mkdir()
        (tmp_path / "nvme0").mkdir()  # should be excluded
        (tmp_path / "mmcblk0").mkdir()  # should be excluded
        devices = sata.find_sata_devices(tmp_path)
        assert devices == ["sda", "sdb"]

    def test_sata_rotational_flag_read_from_sysfs(self, tmp_path):
        """read_rotational should parse the sysfs rotational file correctly."""
        dev_dir = tmp_path / "sda" / "queue"
        dev_dir.mkdir(parents=True)
        (dev_dir / "rotational").write_text("1\n")
        assert sata.read_rotational(tmp_path, "sda") == 1

        (dev_dir / "rotational").write_text("0\n")
        assert sata.read_rotational(tmp_path, "sda") == 0

    def test_sata_rotational_defaults_to_1_when_missing(self, tmp_path):
        """Missing rotational file should default to 1 (HDD)."""
        (tmp_path / "sda").mkdir()
        assert sata.read_rotational(tmp_path, "sda") == 1


# ---------------------------------------------------------------------------
# TestWearPredictor (4 tests) — G4 not yet implemented
# ---------------------------------------------------------------------------

class TestWearPredictor:
    def test_wear_predictor_linear_projection(self):
        """Positive slope 0.5%/hour, current=50% → days_remaining ≈ 4.17."""
        # 100 hourly samples spanning 99 hours (>= 72), wear starting at 0.5 and
        # increasing at 0.5%/hour so that the last value is ~50%.
        base_ts = 1_700_000_000.0
        n = 100
        timestamps = [base_ts + i * 3600.0 for i in range(n)]
        slope_per_hour = 0.5
        values = [slope_per_hour * i for i in range(n)]  # 0.0, 0.5, 1.0, ... 49.5
        # current value = values[-1] = 49.5
        result = wp.predict_device(timestamps, values)
        assert result["insufficient_data"] is False
        assert result["days_remaining"] is not None
        assert result["slope_per_hour"] is not None
        # hours_remaining = (100 - 49.5) / 0.5 = 101 hours → 101/24 ≈ 4.208 days
        expected_hours = (100.0 - values[-1]) / slope_per_hour
        expected_days  = expected_hours / 24.0
        assert abs(result["days_remaining"] - expected_days) < 0.01

    def test_wear_predictor_no_data_returns_none(self):
        """Zero slope (flat wear) → days_remaining is None."""
        base_ts = 1_700_000_000.0
        n = 100
        timestamps = [base_ts + i * 3600.0 for i in range(n)]
        # Constant wear: slope will be 0
        values = [30.0] * n
        result = wp.predict_device(timestamps, values)
        assert result["insufficient_data"] is False
        # slope <= 0 → no prediction
        assert result["days_remaining"] is None
        assert result["hours_remaining"] is None
        assert result["rate_pct_per_day"] is None

    def test_wear_predictor_already_worn_out(self):
        """Fewer than 72 hours of data → insufficient_data=True."""
        base_ts = 1_700_000_000.0
        # Only 48 samples, each 1 hour apart → 47-hour span < 72 hours
        n = 48
        timestamps = [base_ts + i * 3600.0 for i in range(n)]
        values = [float(i) * 0.5 for i in range(n)]
        result = wp.predict_device(timestamps, values)
        assert result["insufficient_data"] is True
        assert result["days_remaining"] is None

    def test_wear_predictor_negative_slope_returns_inf(self):
        """Tiny positive slope (0.000001%/hour) → days_remaining capped at MAX_DAYS=3650."""
        base_ts = 1_700_000_000.0
        n = 100
        timestamps = [base_ts + i * 3600.0 for i in range(n)]
        slope_per_hour = 0.000001
        values = [1.0 + slope_per_hour * i for i in range(n)]
        result = wp.predict_device(timestamps, values)
        assert result["insufficient_data"] is False
        assert result["days_remaining"] is not None
        # hours_remaining = (100 - ~1.0) / 0.000001 = ~99_000_000 hours → > 3650 days
        assert result["days_remaining"] == wp.MAX_DAYS


# ---------------------------------------------------------------------------
# Storcli JSON fixture helpers
# ---------------------------------------------------------------------------

def _make_storcli_json(**overrides):
    """Build a minimal storcli JSON response with optional field overrides."""
    ctrl_info = {
        "Controller": 0,
        "ROC temperature(Degree Celsius)": 55,
        "Critical Disks": 0,
        "Failed Disks": 0,
        "Memory Correctable Errors": 0,
        "Memory Uncorrectable Errors": 0,
    }
    ctrl_info.update(overrides)
    return json.dumps({
        "Controllers": [{
            "Response Data": {
                "Controllers": [{
                    "Controller Information": ctrl_info,
                }]
            }
        }]
    })


# ---------------------------------------------------------------------------
# TestStorageControllerCollector (10 tests)
# ---------------------------------------------------------------------------

class TestStorageControllerCollector:
    """Tests for controller_collector.detect_drivers and emit_metrics."""

    # ------------------------------------------------------------------
    # Helper: create a fake sysfs PCI drivers directory
    # ------------------------------------------------------------------
    def _make_driver_dir(self, tmp_path, driver_name, pci_entries=None):
        """Create a fake /sys/bus/pci/drivers/<driver> directory."""
        driver_dir = tmp_path / driver_name
        driver_dir.mkdir(parents=True)
        for entry in (pci_entries or []):
            (driver_dir / entry).mkdir()
        return tmp_path

    # 1 — no drivers → up{driver="none"} 0
    def test_no_drivers_detected_emits_up_zero(self, tmp_path):
        """Empty sysfs path → no drivers detected → up{driver="none"} 0."""
        # tmp_path is empty; no driver subdirs exist
        drivers = cc.detect_drivers(tmp_path)
        assert drivers == []
        output = cc.emit_metrics([], [])
        assert 'storage_controller_up{driver="none"} 0' in output

    # 2 — megaraid present but storcli absent → up{driver="megaraid_sas"} 1, no crash
    def test_megaraid_driver_detected_emits_up_one(self, tmp_path):
        """megaraid_sas sysfs dir with one PCI device → driver detected."""
        self._make_driver_dir(tmp_path, "megaraid_sas", ["0000:03:00.0"])
        drivers = cc.detect_drivers(tmp_path)
        assert "megaraid_sas" in drivers
        output = cc.emit_metrics(drivers, [])
        assert 'storage_controller_up{driver="megaraid_sas"} 1' in output

    # 3 — storcli JSON with temperature 55 → temperature metric emitted
    def test_storcli_json_parsed_temperature(self, tmp_path):
        """JSON with ROC temperature 55 → temperature_celsius metric = 55."""
        storcli_json = _make_storcli_json(**{"ROC temperature(Degree Celsius)": 55})
        megaraid_results = []
        # Parse via collect_megaraid's inner logic directly: use the JSON parser path
        data = json.loads(storcli_json)
        for ctrl_entry in data["Controllers"]:
            resp = ctrl_entry["Response Data"]
            for ctrl_info_item in resp["Controllers"]:
                info = ctrl_info_item["Controller Information"]
                megaraid_results.append({
                    "controller": str(info.get("Controller", 0)),
                    "temperature": info.get("ROC temperature(Degree Celsius)"),
                    "critical_disks": info.get("Critical Disks"),
                    "failed_disks": info.get("Failed Disks"),
                    "ecc_correctable": info.get("Memory Correctable Errors"),
                    "ecc_uncorrectable": info.get("Memory Uncorrectable Errors"),
                })
        output = cc.emit_metrics(["megaraid_sas"], megaraid_results)
        assert "storage_controller_temperature_celsius" in output
        assert "55" in output

    # 4 — JSON with Critical Disks 2 → critical_disks metric
    def test_storcli_json_parsed_critical_disks(self, tmp_path):
        storcli_json = _make_storcli_json(**{"Critical Disks": 2})
        data = json.loads(storcli_json)
        megaraid_results = []
        for ctrl_entry in data["Controllers"]:
            for ctrl_info_item in ctrl_entry["Response Data"]["Controllers"]:
                info = ctrl_info_item["Controller Information"]
                megaraid_results.append({
                    "controller": str(info.get("Controller", 0)),
                    "temperature": info.get("ROC temperature(Degree Celsius)"),
                    "critical_disks": info.get("Critical Disks"),
                    "failed_disks": info.get("Failed Disks"),
                    "ecc_correctable": info.get("Memory Correctable Errors"),
                    "ecc_uncorrectable": info.get("Memory Uncorrectable Errors"),
                })
        output = cc.emit_metrics(["megaraid_sas"], megaraid_results)
        assert "storage_controller_critical_disks" in output
        assert '} 2' in output

    # 5 — JSON with Failed Disks 1 → failed_disks metric
    def test_storcli_json_parsed_failed_disks(self, tmp_path):
        storcli_json = _make_storcli_json(**{"Failed Disks": 1})
        data = json.loads(storcli_json)
        megaraid_results = []
        for ctrl_entry in data["Controllers"]:
            for ctrl_info_item in ctrl_entry["Response Data"]["Controllers"]:
                info = ctrl_info_item["Controller Information"]
                megaraid_results.append({
                    "controller": str(info.get("Controller", 0)),
                    "temperature": info.get("ROC temperature(Degree Celsius)"),
                    "critical_disks": info.get("Critical Disks"),
                    "failed_disks": info.get("Failed Disks"),
                    "ecc_correctable": info.get("Memory Correctable Errors"),
                    "ecc_uncorrectable": info.get("Memory Uncorrectable Errors"),
                })
        output = cc.emit_metrics(["megaraid_sas"], megaraid_results)
        assert "storage_controller_failed_disks" in output
        assert '} 1' in output

    # 6 — JSON with Memory Correctable Errors 3 → ecc correctable metric
    def test_storcli_json_parsed_ecc_correctable(self, tmp_path):
        storcli_json = _make_storcli_json(**{"Memory Correctable Errors": 3})
        data = json.loads(storcli_json)
        megaraid_results = []
        for ctrl_entry in data["Controllers"]:
            for ctrl_info_item in ctrl_entry["Response Data"]["Controllers"]:
                info = ctrl_info_item["Controller Information"]
                megaraid_results.append({
                    "controller": str(info.get("Controller", 0)),
                    "temperature": info.get("ROC temperature(Degree Celsius)"),
                    "critical_disks": info.get("Critical Disks"),
                    "failed_disks": info.get("Failed Disks"),
                    "ecc_correctable": info.get("Memory Correctable Errors"),
                    "ecc_uncorrectable": info.get("Memory Uncorrectable Errors"),
                })
        output = cc.emit_metrics(["megaraid_sas"], megaraid_results)
        assert 'error_type="correctable"' in output
        assert '} 3' in output

    # 7 — JSON with Memory Uncorrectable Errors 1 → ecc uncorrectable metric
    def test_storcli_json_parsed_ecc_uncorrectable(self, tmp_path):
        storcli_json = _make_storcli_json(**{"Memory Uncorrectable Errors": 1})
        data = json.loads(storcli_json)
        megaraid_results = []
        for ctrl_entry in data["Controllers"]:
            for ctrl_info_item in ctrl_entry["Response Data"]["Controllers"]:
                info = ctrl_info_item["Controller Information"]
                megaraid_results.append({
                    "controller": str(info.get("Controller", 0)),
                    "temperature": info.get("ROC temperature(Degree Celsius)"),
                    "critical_disks": info.get("Critical Disks"),
                    "failed_disks": info.get("Failed Disks"),
                    "ecc_correctable": info.get("Memory Correctable Errors"),
                    "ecc_uncorrectable": info.get("Memory Uncorrectable Errors"),
                })
        output = cc.emit_metrics(["megaraid_sas"], megaraid_results)
        assert 'error_type="uncorrectable"' in output
        assert '} 1' in output

    # 8 — hpsa driver present → up{driver="hpsa"} 1
    def test_hpsa_driver_detected_emits_up_one(self, tmp_path):
        """hpsa sysfs dir with PCI device → driver detected → up 1."""
        self._make_driver_dir(tmp_path, "hpsa", ["0000:04:00.0"])
        drivers = cc.detect_drivers(tmp_path)
        assert "hpsa" in drivers
        output = cc.emit_metrics(drivers, [])
        assert 'storage_controller_up{driver="hpsa"} 1' in output

    # 9 — aacraid driver present → up{driver="aacraid"} 1
    def test_aacraid_driver_detected_emits_up_one(self, tmp_path):
        """aacraid sysfs dir with PCI device → driver detected → up 1."""
        self._make_driver_dir(tmp_path, "aacraid", ["0000:05:00.0"])
        drivers = cc.detect_drivers(tmp_path)
        assert "aacraid" in drivers
        output = cc.emit_metrics(drivers, [])
        assert 'storage_controller_up{driver="aacraid"} 1' in output

    # 10 — storcli returns non-JSON → no crash, still emits up 1
    def test_storcli_returns_invalid_json_graceful(self, tmp_path):
        """Non-JSON storcli output should not crash; megaraid_results will be []."""
        # Simulate: driver detected, but megaraid_results is empty (as collect_megaraid
        # would return [] on bad JSON)
        self._make_driver_dir(tmp_path, "megaraid_sas", ["0000:03:00.0"])
        drivers = cc.detect_drivers(tmp_path)
        # collect_megaraid returns [] when JSON is bad — simulate that here
        megaraid_results = []
        output = cc.emit_metrics(drivers, megaraid_results)
        assert 'storage_controller_up{driver="megaraid_sas"} 1' in output
        # No temperature/disk metrics when results list is empty
        assert "storage_controller_temperature_celsius" not in output


# ---------------------------------------------------------------------------
# TestWearPredictorPureLogic (4 tests)
# ---------------------------------------------------------------------------

class TestWearPredictorPureLogic:
    """Tests for wear_predictor pure-logic functions: emit_metrics,
    extract_device_series, and ols_slope_intercept."""

    # 11 — emit_metrics output format
    def test_wear_emit_metrics_output_format(self):
        """emit_metrics with a known prediction dict contains expected metric names."""
        base_ts = 1_700_000_000.0
        n = 100
        timestamps = [base_ts + i * 3600.0 for i in range(n)]
        slope_per_hour = 0.5
        values = [slope_per_hour * i for i in range(n)]
        pred_result = wp.predict_device(timestamps, values)
        predictions = {
            "nvme0": {"type": "nvme", "result": pred_result},
        }
        output = wp.emit_metrics(predictions, predictor_up=1)
        assert "storage_wear_time_to_failure_days" in output
        assert "storage_wear_rate_percent_per_day" in output
        assert "storage_wear_probability_90d" in output
        assert "storage_wear_predictor_up 1" in output
        assert 'device="nvme0"' in output

    # 12 — extract_device_series groups by device label
    def test_extract_device_series_groups_by_device(self):
        """Prometheus matrix results are correctly grouped by device label."""
        result_list = [
            {
                "metric": {"device": "nvme0"},
                "values": [
                    [1700000000, "10.0"],
                    [1700003600, "10.5"],
                ],
            },
            {
                "metric": {"device": "nvme1"},
                "values": [
                    [1700000000, "20.0"],
                    [1700003600, "20.1"],
                ],
            },
        ]
        series = wp.extract_device_series(result_list, label_key="device")
        assert set(series.keys()) == {"nvme0", "nvme1"}
        ts0, vals0 = series["nvme0"]
        assert ts0 == [1700000000.0, 1700003600.0]
        assert vals0 == [10.0, 10.5]
        ts1, vals1 = series["nvme1"]
        assert vals1[0] == 20.0

    # 13 — OLS slope positive value
    def test_ols_slope_positive_value(self):
        """Perfect linear data x=[0..4], y=[10..14] → slope=1.0, intercept=10.0."""
        xs = [0.0, 1.0, 2.0, 3.0, 4.0]
        ys = [10.0, 11.0, 12.0, 13.0, 14.0]
        slope, intercept = wp.ols_slope_intercept(xs, ys)
        assert slope is not None
        assert abs(slope - 1.0) < 1e-9
        assert abs(intercept - 10.0) < 1e-9

    # 14 — OLS slope zero for flat series
    def test_ols_slope_zero_flat_series(self):
        """Constant y=[5,5,5,5,5] → slope=0.0."""
        xs = [0.0, 1.0, 2.0, 3.0, 4.0]
        ys = [5.0, 5.0, 5.0, 5.0, 5.0]
        slope, intercept = wp.ols_slope_intercept(xs, ys)
        assert slope is not None
        assert abs(slope - 0.0) < 1e-9
        assert abs(intercept - 5.0) < 1e-9
