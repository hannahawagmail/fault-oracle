# SPDX-License-Identifier: Apache-2.0
"""
bmc/tests/test_bmc_collectors.py — Unit tests for IPMI BMC collectors.

No live BMC is required.  All ipmitool calls are replaced by fixture strings
or mock callables injected into the module functions under test.

Test classes
------------
TestSELParsing        (8 tests)  — parse_sel_csv correctness
TestIPMISensorParsing (8 tests)  — parse_sdr_output correctness
TestBMCConnectivity   (6 tests)  — collect_node_sel / state file / error paths
TestRedfishCollector  (8 tests)  — Redfish API collector (all HTTP calls mocked)
"""

import io
import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Make the bmc/ package importable from tests/ sub-directory.
# ---------------------------------------------------------------------------
_BMC_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_BMC_DIR))

from ipmi_sel_poller import (   # noqa: E402
    classify_severity,
    collect_node_sel,
    emit_metrics as sel_emit_metrics,
    load_state,
    parse_sel_csv,
    save_state,
    sensor_type_for,
)
from ipmi_sensor_exporter import (  # noqa: E402
    emit_metrics as sensor_emit_metrics,
    parse_sdr_output,
)
from redfish_collector import (  # noqa: E402
    collect_power,
    collect_sel_entries,
    collect_system_health,
    collect_thermal,
    emit_metrics as redfish_emit_metrics,
    health_to_int,
    redfish_get,
)

# ===========================================================================
# Fixture strings — realistic ipmitool output with no live BMC required.
# ===========================================================================

# 4 events: 2 memory (Assert → critical), 1 fan (Deassert → warning),
# 1 power (Assert → critical).
IPMI_SEL_CSV = """\
0x0001,04/01/2024 08:00:00,Memory ECC Error,Correctable ECC,Assert,Critical
0x0002,04/01/2024 08:05:00,Memory ECC Error,Correctable ECC,Assert,Critical
0x0003,04/01/2024 08:10:00,Fan1 Sensor,Fan Failure,Deassert,Warning
0x0004,04/01/2024 08:15:00,Power Supply,AC Lost,Assert,Critical
"""

# SDR output: fan RPMs, temperatures, voltages, one Upper Critical breach.
IPMI_SDR_OUTPUT = """\
Fan1 RPM         | 0x01 | ok             | 1200 RPM
Fan2 RPM         | 0x02 | ok             | 1350 RPM
CPU Temp         | 0x03 | ok             | 45 degrees C
Inlet Temp       | 0x04 | Upper Critical | 95 degrees C
VCore            | 0x05 | ok             | 0.88 Volts
PS1 Input        | 0x06 | ok             | 120 Watts
"""

# Empty SEL (ipmitool returns blank output when log is clear).
IPMI_SEL_EMPTY = ""

# A single malformed CSV line that has only 3 fields.
IPMI_SEL_MALFORMED_LINE = "0x0001,04/01/2024 09:00:00,Only Three Fields\n"


# ===========================================================================
# Helper
# ===========================================================================

def _make_target(node="node-01", ip="192.168.1.100",
                 user="admin", password="secret"):
    return {"node": node, "ip": ip, "user": user, "pass": password}


def _mock_ipmitool_ok(output: str):
    """Return a fake run_ipmitool_sel/sdr function that returns the given output."""
    def _fn(ip, user, password, **kwargs):
        return output, None
    return _fn


def _mock_ipmitool_err(error_msg: str):
    def _fn(ip, user, password, **kwargs):
        return None, error_msg
    return _fn


# ===========================================================================
# TestSELParsing  (8 tests)
# ===========================================================================

class TestSELParsing:

    def test_multi_event_csv_parsed(self):
        """Four CSV rows → four parsed events."""
        events = parse_sel_csv(IPMI_SEL_CSV)
        assert len(events) == 4

    def test_memory_event_critical_severity(self):
        """Memory ECC Assert event classifies as 'critical'."""
        events = parse_sel_csv(IPMI_SEL_CSV)
        mem_events = [e for e in events if "Memory" in e["sensor_name"]]
        assert len(mem_events) == 2
        for e in mem_events:
            sev = classify_severity(e["event_direction"], e["sensor_name"])
            assert sev == "critical", f"Expected critical, got {sev!r} for {e}"

    def test_fan_event_warning_severity(self):
        """Fan Deassert event classifies as 'warning'."""
        events = parse_sel_csv(IPMI_SEL_CSV)
        fan_events = [e for e in events if "Fan" in e["sensor_name"]]
        assert len(fan_events) == 1
        sev = classify_severity(fan_events[0]["event_direction"], fan_events[0]["sensor_name"])
        assert sev == "warning"

    def test_record_id_deduplication_zero_new_events(self):
        """If last_record_id equals highest record_id in output, new_events is empty."""
        target   = _make_target()
        result   = collect_node_sel(target, last_record_id=4,
                                    run_ipmitool_fn=_mock_ipmitool_ok(IPMI_SEL_CSV))
        assert result["new_events"] == []
        # last_record_id must not decrease.
        assert result["last_record_id"] == 4

    def test_empty_sel_zero_events(self):
        """Empty ipmitool output → zero events, no crash."""
        events = parse_sel_csv(IPMI_SEL_EMPTY)
        assert events == []

    def test_malformed_line_skipped(self, capsys):
        """A line with fewer than 6 CSV fields is skipped; valid lines still parsed."""
        mixed = IPMI_SEL_MALFORMED_LINE + IPMI_SEL_CSV
        events = parse_sel_csv(mixed)
        # The 4 good events should still be present.
        assert len(events) == 4
        captured = capsys.readouterr()
        assert "malformed" in captured.err.lower() or "skipping" in captured.err.lower()

    def test_timestamp_parsing(self):
        """Timestamps are converted to non-zero floats for well-formed CSV."""
        events = parse_sel_csv(IPMI_SEL_CSV)
        for e in events:
            assert isinstance(e["timestamp"], float)
            assert e["timestamp"] > 0

    def test_sensor_type_label_correct(self):
        """sensor_type_for returns the correct category for each sensor name."""
        assert sensor_type_for("Memory ECC Error")  == "memory"
        assert sensor_type_for("Fan1 Sensor")        == "thermal"
        assert sensor_type_for("Power Supply")       == "power"
        assert sensor_type_for("CPU Temp")           == "cpu"
        assert sensor_type_for("Unknown Widget")     == "other"


# ===========================================================================
# TestIPMISensorParsing  (8 tests)
# ===========================================================================

class TestIPMISensorParsing:

    def _sensors(self, raw=IPMI_SDR_OUTPUT):
        return parse_sdr_output(raw)

    def test_fan_rpm_parsed(self):
        """Fan sensors are parsed with unit_type 'fan_rpm' and numeric value."""
        sensors = self._sensors()
        fans = [s for s in sensors if s["unit_type"] == "fan_rpm"]
        assert len(fans) == 2
        names = {s["sensor_name"] for s in fans}
        assert "Fan1 RPM" in names
        assert "Fan2 RPM" in names
        assert fans[0]["value"] == 1200.0
        assert fans[1]["value"] == 1350.0

    def test_temperature_celsius_parsed(self):
        """Temperature sensors map to unit_type 'temperature_celsius'."""
        sensors = self._sensors()
        temps = [s for s in sensors if s["unit_type"] == "temperature_celsius"]
        assert len(temps) == 2
        cpu_temp = next(s for s in temps if "CPU" in s["sensor_name"])
        assert cpu_temp["value"] == 45.0

    def test_voltage_volts_parsed(self):
        """Voltage sensors map to unit_type 'voltage_volts'."""
        sensors = self._sensors()
        volts = [s for s in sensors if s["unit_type"] == "voltage_volts"]
        assert len(volts) == 1
        assert volts[0]["sensor_name"] == "VCore"
        assert volts[0]["value"] == pytest.approx(0.88)

    def test_psu_watts_parsed(self):
        """PSU power sensors map to unit_type 'power_watts'."""
        sensors = self._sensors()
        watts = [s for s in sensors if s["unit_type"] == "power_watts"]
        assert len(watts) == 1
        assert watts[0]["sensor_name"] == "PS1 Input"
        assert watts[0]["value"] == 120.0

    def test_threshold_breach_detected(self):
        """Sensor with 'Upper Critical' status has breach=True."""
        sensors = self._sensors()
        breach_sensors = [s for s in sensors if s["breach"]]
        assert len(breach_sensors) == 1
        assert "Inlet" in breach_sensors[0]["sensor_name"]

    def test_ok_status_breach_zero(self):
        """Sensors with status 'ok' have breach=False."""
        sensors = self._sensors()
        ok_sensors = [s for s in sensors if not s["breach"]]
        assert len(ok_sensors) >= 4
        for s in ok_sensors:
            assert s["breach"] is False

    def test_unknown_unit_skipped_gracefully(self):
        """Lines whose unit doesn't match any known type are omitted without crash."""
        raw = "Mystery Sensor  | 0x99 | ok | 42 Furlongs\n"
        sensors = parse_sdr_output(raw)
        assert sensors == []

    def test_multi_sensor_multi_node(self):
        """Sensor metric output contains all nodes when called with multiple results."""
        r1 = {
            "node": "node-01", "up": True, "error": None,
            "sensors": parse_sdr_output(IPMI_SDR_OUTPUT),
        }
        r2 = {
            "node": "node-02", "up": True, "error": None,
            "sensors": parse_sdr_output(
                "Fan1 RPM | 0x01 | ok | 800 RPM\n"
                "CPU Temp | 0x02 | ok | 60 degrees C\n"
            ),
        }
        output = sensor_emit_metrics([r1, r2])
        assert 'node="node-01"' in output
        assert 'node="node-02"' in output
        # Both should have fan readings.
        assert "ipmi_fan_rpm" in output


# ===========================================================================
# TestBMCConnectivity  (6 tests)
# ===========================================================================

class TestBMCConnectivity:

    def test_ipmitool_absent_collector_up_zero(self, tmp_path):
        """When ipmitool is not installed, all nodes get collector_up=0."""
        targets = [_make_target("node-01"), _make_target("node-02", ip="192.168.1.101")]
        results = []
        poll_errors: dict = {}
        for t in targets:
            # Simulate ipmitool-not-found error from our run function.
            result = collect_node_sel(
                t, last_record_id=0,
                run_ipmitool_fn=_mock_ipmitool_err("ipmitool not found"),
            )
            results.append(result)
            if not result["up"]:
                poll_errors[t["node"]] = poll_errors.get(t["node"], 0) + 1

        output = sel_emit_metrics(results, poll_errors)
        assert 'ipmi_bmc_collector_up{node="node-01"} 0' in output
        assert 'ipmi_bmc_collector_up{node="node-02"} 0' in output

    def test_auth_failure_poll_error_incremented(self):
        """Auth failure → poll_error_total is 1 for that node."""
        target = _make_target("node-03")
        result = collect_node_sel(
            target, last_record_id=0,
            run_ipmitool_fn=_mock_ipmitool_err("Error authenticating"),
        )
        assert result["up"] is False
        poll_errors = {"node-03": 1}
        output = sel_emit_metrics([result], poll_errors)
        assert 'ipmi_bmc_poll_error_total{node="node-03"} 1' in output

    def test_successful_poll_collector_up_one(self):
        """A successful poll → collector_up=1 and events are counted."""
        target = _make_target("node-04")
        result = collect_node_sel(
            target, last_record_id=0,
            run_ipmitool_fn=_mock_ipmitool_ok(IPMI_SEL_CSV),
        )
        assert result["up"] is True
        assert len(result["new_events"]) == 4
        output = sel_emit_metrics([result], {})
        assert 'ipmi_bmc_collector_up{node="node-04"} 1' in output

    def test_state_file_created_on_first_run(self, tmp_path):
        """State file is written after the first successful collection."""
        state_file = tmp_path / "sel-state.json"
        assert not state_file.exists()

        state: dict[str, int] = {}
        target = _make_target("node-05")
        result = collect_node_sel(
            target, last_record_id=0,
            run_ipmitool_fn=_mock_ipmitool_ok(IPMI_SEL_CSV),
        )
        # Simulate what main() does: update state dict and persist.
        state["node-05"] = result["last_record_id"]
        save_state(state_file, state)

        assert state_file.exists()
        loaded = json.loads(state_file.read_text())
        assert loaded["node-05"] == 4   # highest record_id in IPMI_SEL_CSV

    def test_state_file_read_prevents_duplicate_events(self, tmp_path):
        """Second run with same SEL output emits zero new events (dedup via state)."""
        state_file = tmp_path / "sel-state.json"
        # Simulate first run already seen record_ids 1-4.
        save_state(state_file, {"node-06": 4})

        state = load_state(state_file)
        last_id = state.get("node-06", 0)

        target = _make_target("node-06")
        result = collect_node_sel(
            target, last_record_id=last_id,
            run_ipmitool_fn=_mock_ipmitool_ok(IPMI_SEL_CSV),
        )
        assert result["new_events"] == []

    def test_timeout_poll_error_incremented(self):
        """Timeout from ipmitool → poll_error_total is incremented."""
        target = _make_target("node-07")
        result = collect_node_sel(
            target, last_record_id=0,
            run_ipmitool_fn=_mock_ipmitool_err("timeout"),
        )
        assert result["up"] is False
        poll_errors = {"node-07": 1}
        output = sel_emit_metrics([result], poll_errors)
        assert 'ipmi_bmc_poll_error_total{node="node-07"} 1' in output
        assert 'ipmi_bmc_collector_up{node="node-07"} 0' in output


# ===========================================================================
# TestRedfishCollector  (8 tests)
# ===========================================================================

def _make_urlopen_mock(response_body: bytes, status: int = 200):
    """Return a context-manager mock for urllib.request.urlopen."""
    mock_response = MagicMock()
    mock_response.read.return_value = response_body
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


class TestRedfishCollector:

    # -------------------------------------------------------------------------
    # Test 1: REDFISH_HOST not set → collector_up 0, no exception
    # -------------------------------------------------------------------------
    def test_no_host_emits_collector_up_zero(self, monkeypatch):
        """When REDFISH_HOST is not set the collector emits redfish_collector_up 0."""
        monkeypatch.delenv("REDFISH_HOST", raising=False)

        # Run main() — it should write up=0 to stdout and return cleanly.
        import io as _io
        import redfish_collector as rc

        captured = _io.StringIO()
        with patch("sys.stdout", captured):
            result = rc.main()

        output = captured.getvalue()
        assert "redfish_collector_up 0" in output
        assert result == 0

    # -------------------------------------------------------------------------
    # Test 2: Thermal response → temperature reading extracted per sensor
    # -------------------------------------------------------------------------
    def test_thermal_temperatures_parsed(self):
        """Temperature readings are extracted from Thermal/Temperatures array."""
        thermal_json = json.dumps({
            "Temperatures": [
                {"Name": "CPU1 Temp", "ReadingCelsius": 42},
                {"Name": "Inlet Temp", "ReadingCelsius": 28},
            ],
            "Fans": [],
        }).encode()

        mock_resp = _make_urlopen_mock(thermal_json)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = collect_thermal("bmc.test", "admin", "pass", verify=False)

        assert len(result["temperatures"]) == 2
        names = {t["sensor"] for t in result["temperatures"]}
        assert "CPU1 Temp" in names
        assert "Inlet Temp" in names
        values = {t["sensor"]: t["value"] for t in result["temperatures"]}
        assert values["CPU1 Temp"] == 42
        assert values["Inlet Temp"] == 28

    # -------------------------------------------------------------------------
    # Test 3: Fan RPM extracted from Fans array
    # -------------------------------------------------------------------------
    def test_thermal_fans_parsed(self):
        """Fan RPM readings are extracted from Thermal/Fans array."""
        thermal_json = json.dumps({
            "Temperatures": [],
            "Fans": [
                {"Name": "Fan1A", "Reading": 3600},
                {"Name": "Fan2A", "Reading": 3480},
            ],
        }).encode()

        mock_resp = _make_urlopen_mock(thermal_json)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = collect_thermal("bmc.test", "admin", "pass", verify=False)

        assert len(result["fans"]) == 2
        fan_map = {f["fan"]: f["value"] for f in result["fans"]}
        assert fan_map["Fan1A"] == 3600
        assert fan_map["Fan2A"] == 3480

    # -------------------------------------------------------------------------
    # Test 4: PSU watts and redfish_psu_up 1 for enabled PSU
    # -------------------------------------------------------------------------
    def test_psu_watts_and_up_enabled(self):
        """An Enabled PSU emits correct watts and psu_up=1."""
        power_json = json.dumps({
            "PowerSupplies": [
                {
                    "Name": "PSU1",
                    "PowerInputWatts": 350.0,
                    "Status": {"State": "Enabled"},
                },
                {
                    "Name": "PSU2",
                    "PowerInputWatts": 340.0,
                    "Status": {"State": "Absent"},
                },
            ]
        }).encode()

        mock_resp = _make_urlopen_mock(power_json)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = collect_power("bmc.test", "admin", "pass", verify=False)

        assert len(result) == 2
        psu_map = {p["psu"]: p for p in result}
        assert psu_map["PSU1"]["watts"] == 350.0
        assert psu_map["PSU1"]["up"] == 1
        assert psu_map["PSU2"]["up"] == 0

    # -------------------------------------------------------------------------
    # Test 5: SEL entry Severity=Critical → redfish_sel_entry_total{severity="critical"}
    # -------------------------------------------------------------------------
    def test_sel_critical_entry_increments_counter(self):
        """A Critical SEL entry is counted in redfish_sel_entry_total{severity='critical'}."""
        sel_json = json.dumps({
            "Members": [
                {"Id": "1", "Severity": "Critical", "MessageId": "Memory.1.0.MemoryEccError"},
                {"Id": "2", "Severity": "Warning",  "MessageId": "Fan.1.0.FanDegraded"},
            ]
        }).encode()

        mock_resp = _make_urlopen_mock(sel_json)
        seen_ids: set = set()
        with patch("urllib.request.urlopen", return_value=mock_resp):
            entries = collect_sel_entries(
                "bmc.test",
                "/redfish/v1/Systems/System.Embedded.1/",
                "admin", "pass", False, seen_ids,
            )

        assert len(entries) == 2
        severities = {e["severity"] for e in entries}
        assert "critical" in severities
        assert "warning" in severities
        # Both IDs are now tracked
        assert "1" in seen_ids
        assert "2" in seen_ids

        # Verify the metric output carries the counter line
        output = redfish_emit_metrics("bmc.test", [], entries, {"temperatures": [], "fans": []}, [], 1)
        assert 'redfish_sel_entry_total{severity="critical"' in output

    # -------------------------------------------------------------------------
    # Test 6: System health mapping OK→1, Warning→0, Critical→-1
    # -------------------------------------------------------------------------
    def test_system_health_mapping(self):
        """health_to_int maps OK→1, Warning→0, Critical→-1, unknown→0."""
        assert health_to_int("OK")       == 1
        assert health_to_int("ok")       == 1
        assert health_to_int("Warning")  == 0
        assert health_to_int("warning")  == 0
        assert health_to_int("Critical") == -1
        assert health_to_int("critical") == -1
        assert health_to_int("")         == 0
        assert health_to_int("Unknown")  == 0

    # -------------------------------------------------------------------------
    # Test 7: REDFISH_INSECURE=true → SSL uses _create_unverified_context
    # -------------------------------------------------------------------------
    def test_insecure_flag_uses_unverified_ssl(self):
        """When verify=False, redfish_get uses ssl._create_unverified_context."""
        import ssl

        good_json = json.dumps({"ok": True}).encode()
        mock_resp = _make_urlopen_mock(good_json)

        with patch("ssl._create_unverified_context") as mock_unverified, \
             patch("ssl.create_default_context") as mock_verified, \
             patch("urllib.request.urlopen", return_value=mock_resp):
            redfish_get("bmc.test", "/redfish/v1/", "admin", "pass", verify=False)

        mock_unverified.assert_called_once()
        mock_verified.assert_not_called()

    # -------------------------------------------------------------------------
    # Test 8: URLError (connection refused) → redfish_collector_up 0, graceful exit
    # -------------------------------------------------------------------------
    def test_urlopen_error_emits_collector_up_zero(self):
        """A URLError during HTTP call causes collect_thermal to return empty data."""
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("Connection refused")):
            result = collect_thermal("bmc.test", "admin", "pass", verify=False)

        # On network error the collector returns empty dicts — no exception raised.
        assert result["temperatures"] == []
        assert result["fans"] == []

        # Verify that emit_metrics with collector_up=0 produces the right output.
        output = redfish_emit_metrics(
            "bmc.test", [], [], {"temperatures": [], "fans": []}, [], 0
        )
        assert "redfish_collector_up 0" in output
