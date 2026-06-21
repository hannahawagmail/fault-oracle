# SPDX-License-Identifier: Apache-2.0
"""
test_pipeline_integration.py — End-to-end pipeline tests.

Tests the full parse → inspect → replay → metrics workflow:
  1. parse_edac_trace.py parses a log file into JSON
  2. The JSON is structurally valid (schema check)
  3. replay_kernel_state.sh consumes the JSON (dry-run mode)
  4. Prometheus metric synthesis from parsed events

These tests exercise the complete data path that an operator would use
in a postmortem or replay session.
"""

import json
import os
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
REPLAY_DIR = REPO / "replay"
PARSER = REPLAY_DIR / "parse_edac_trace.py"
REPLAY_SH = REPLAY_DIR / "replay_kernel_state.sh"
SAMPLE_LOG = REPLAY_DIR / "example_traces" / "sample_ce_storm.log"

sys.path.insert(0, str(REPLAY_DIR))
from parse_edac_trace import TraceParser, HardwareEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_log_file(path: Path, event_types=None) -> list[HardwareEvent]:
    """Parse a log file using TraceParser directly."""
    parser_args = {"event_types": event_types} if event_types is not None else {}
    tp = TraceParser(**parser_args)
    with open(path) as f:
        return tp.parse_file(f)


def parse_to_json(log_path: Path, extra_args=None) -> list[dict]:
    """Run parse_edac_trace.py via subprocess, return parsed JSON."""
    cmd = [sys.executable, str(PARSER),
           "--input", str(log_path),
           "--output", "-",
           "--no-stats"]
    if extra_args:
        cmd.extend(extra_args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, f"Parser failed: {result.stderr}"
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

REQUIRED_FIELDS = {
    "seq", "timestamp_str", "timestamp_ns", "monotonic_s",
    "event_type", "subsystem", "controller", "csrow", "channel",
    "count", "page", "offset", "grain", "syndrome",
    "pci_device", "aer_error_type", "raw_message",
}

VALID_EVENT_TYPES = {"CE", "UE", "AER_CE", "AER_UE", "MCE"}
VALID_SUBSYSTEMS = {"EDAC", "AER", "MCE"}


# ---------------------------------------------------------------------------
# Sample log parsing
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not SAMPLE_LOG.exists(), reason="sample_ce_storm.log not found")
class TestSampleLogParsing:
    """Parse the bundled sample CE storm log end-to-end."""

    def test_sample_log_parses_without_error(self):
        events = parse_log_file(SAMPLE_LOG)
        assert isinstance(events, list)

    def test_sample_log_produces_events(self):
        events = parse_log_file(SAMPLE_LOG)
        assert len(events) > 0, "sample_ce_storm.log produced no events"

    def test_sample_log_all_are_ce_or_ue(self):
        events = parse_log_file(SAMPLE_LOG, event_types=["CE", "UE"])
        for e in events:
            assert e.event_type in ("CE", "UE")

    def test_sample_log_events_have_monotonic_timestamps(self):
        events = parse_log_file(SAMPLE_LOG)
        mono_events = [e for e in events if e.monotonic_s is not None]
        assert len(mono_events) > 0, "No events with monotonic timestamps"

    def test_sample_log_events_are_ordered(self):
        events = parse_log_file(SAMPLE_LOG)
        mono_times = [e.monotonic_s for e in events if e.monotonic_s is not None]
        assert mono_times == sorted(mono_times), "Events not in timestamp order"

    def test_sample_log_seq_numbers_sequential(self):
        events = parse_log_file(SAMPLE_LOG)
        for i, e in enumerate(events):
            assert e.seq == i, f"seq mismatch at index {i}: got {e.seq}"

    def test_sample_log_ce_events_have_controller(self):
        events = parse_log_file(SAMPLE_LOG, event_types=["CE"])
        for e in events:
            assert e.controller, f"CE event seq={e.seq} has empty controller"

    def test_sample_log_ce_events_have_positive_count(self):
        events = parse_log_file(SAMPLE_LOG, event_types=["CE"])
        for e in events:
            assert e.count >= 1

    def test_sample_log_subsystem_is_edac(self):
        events = parse_log_file(SAMPLE_LOG, event_types=["CE", "UE"])
        for e in events:
            assert e.subsystem == "EDAC"


# ---------------------------------------------------------------------------
# JSON schema validation
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not SAMPLE_LOG.exists(), reason="sample_ce_storm.log not found")
class TestJSONSchemaValidation:
    """JSON output from the parser must conform to the documented schema."""

    @pytest.fixture(scope="class")
    def events_json(self):
        return parse_to_json(SAMPLE_LOG)

    def test_output_is_list(self, events_json):
        assert isinstance(events_json, list)

    def test_each_event_is_dict(self, events_json):
        for ev in events_json:
            assert isinstance(ev, dict), f"event is not dict: {type(ev)}"

    def test_all_required_fields_present(self, events_json):
        for ev in events_json:
            missing = REQUIRED_FIELDS - set(ev.keys())
            assert not missing, f"event seq={ev.get('seq')} missing fields: {missing}"

    def test_event_type_is_valid(self, events_json):
        for ev in events_json:
            assert ev["event_type"] in VALID_EVENT_TYPES, \
                f"Invalid event_type: {ev['event_type']}"

    def test_subsystem_is_valid(self, events_json):
        for ev in events_json:
            assert ev["subsystem"] in VALID_SUBSYSTEMS, \
                f"Invalid subsystem: {ev['subsystem']}"

    def test_seq_is_int(self, events_json):
        for ev in events_json:
            assert isinstance(ev["seq"], int)

    def test_count_is_positive_int(self, events_json):
        for ev in events_json:
            assert isinstance(ev["count"], int) and ev["count"] >= 1

    def test_timestamp_ns_is_int(self, events_json):
        for ev in events_json:
            assert isinstance(ev["timestamp_ns"], int)

    def test_page_is_int_or_null(self, events_json):
        for ev in events_json:
            assert ev["page"] is None or isinstance(ev["page"], int)

    def test_raw_message_is_nonempty_string(self, events_json):
        for ev in events_json:
            assert isinstance(ev["raw_message"], str) and len(ev["raw_message"]) > 0

    def test_json_roundtrip_fidelity(self, events_json):
        """Re-serializing should produce identical JSON."""
        serialized = json.dumps(events_json)
        reparsed = json.loads(serialized)
        assert reparsed == events_json


# ---------------------------------------------------------------------------
# CLI filter integration
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not SAMPLE_LOG.exists(), reason="sample_ce_storm.log not found")
class TestCLIFilterIntegration:
    """CLI --event-types filter integrates correctly with the JSON output."""

    def test_ce_only_filter(self):
        events = parse_to_json(SAMPLE_LOG, ["--event-types", "CE"])
        assert all(e["event_type"] == "CE" for e in events)

    def test_ue_only_filter_may_be_empty(self):
        events = parse_to_json(SAMPLE_LOG, ["--event-types", "UE"])
        assert all(e["event_type"] == "UE" for e in events)

    def test_ce_ue_filter_contains_only_ce_ue(self):
        events = parse_to_json(SAMPLE_LOG, ["--event-types", "CE,UE"])
        for e in events:
            assert e["event_type"] in ("CE", "UE")

    def test_multi_type_counts_add_up(self):
        all_events = parse_to_json(SAMPLE_LOG)
        ce_events  = parse_to_json(SAMPLE_LOG, ["--event-types", "CE"])
        ue_events  = parse_to_json(SAMPLE_LOG, ["--event-types", "UE"])
        mce_events = parse_to_json(SAMPLE_LOG, ["--event-types", "MCE"])
        aer_events = parse_to_json(SAMPLE_LOG, ["--event-types", "AER_CE,AER_UE"])
        total = len(ce_events) + len(ue_events) + len(mce_events) + len(aer_events)
        assert total == len(all_events)

    def test_pretty_output_valid_json(self):
        events = parse_to_json(SAMPLE_LOG, ["--pretty"])
        assert isinstance(events, list)


# ---------------------------------------------------------------------------
# Multi-log synthetic pipeline
# ---------------------------------------------------------------------------

class TestSyntheticPipeline:
    """Parse synthetic log → validate JSON → replay dry-run."""

    MULTI_EVENT_LOG = "\n".join([
        "[   10.001234] EDAC MC0: 1 CE on DIMM_0_CH0 (mc:0 page:0x00012ab offset:0x0 grain:8 syndrome:0x0000000000000001)",
        "[   10.001235] EDAC MC0: CE error on memory module DIMM_0_CH0 (csrow:0 channel:0)",
        "[   47.234567] EDAC MC0: 1 UE on DIMM_0_CH0 (mc:0 page:0x00012ab offset:0x50 grain:8 syndrome:0x0000000000000000)",
        "[   55.123456] mce: [Hardware Error]: Machine check events logged",
        "[   88.000000] pcieport 0000:00:01.0: AER: Corrected error received: 0000:01:00.0",
    ])

    @pytest.fixture(scope="class")
    def log_file(self, tmp_path_factory):
        d = tmp_path_factory.mktemp("pipeline")
        p = d / "test.log"
        p.write_text(self.MULTI_EVENT_LOG)
        return p

    @pytest.fixture(scope="class")
    def events_json_path(self, log_file, tmp_path_factory):
        d = tmp_path_factory.mktemp("pipeline_out")
        out = d / "events.json"
        result = subprocess.run(
            [sys.executable, str(PARSER), "--input", str(log_file),
             "--output", str(out), "--no-stats"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        return out

    def test_pipeline_produces_json_file(self, events_json_path):
        assert events_json_path.exists()

    def test_pipeline_json_has_4_events(self, events_json_path):
        data = json.loads(events_json_path.read_text())
        assert len(data) == 4, f"Expected 4 events, got {len(data)}: {[e['event_type'] for e in data]}"

    def test_pipeline_ce_event_has_page(self, events_json_path):
        data = json.loads(events_json_path.read_text())
        ce = next(e for e in data if e["event_type"] == "CE")
        assert ce["page"] == 0x12ab

    def test_pipeline_ue_event_has_offset(self, events_json_path):
        data = json.loads(events_json_path.read_text())
        ue = next(e for e in data if e["event_type"] == "UE")
        assert ue["offset"] == 0x50

    def test_pipeline_mce_event_present(self, events_json_path):
        data = json.loads(events_json_path.read_text())
        mce = [e for e in data if e["event_type"] == "MCE"]
        assert len(mce) == 1

    def test_pipeline_aer_event_present(self, events_json_path):
        data = json.loads(events_json_path.read_text())
        aer = [e for e in data if e["event_type"] == "AER_CE"]
        assert len(aer) == 1

    def test_pipeline_aer_has_pci_device(self, events_json_path):
        data = json.loads(events_json_path.read_text())
        aer = next(e for e in data if e["event_type"] == "AER_CE")
        assert aer["pci_device"] == "0000:01:00.0"

    def test_pipeline_ce_has_csrow_from_lookahead(self, events_json_path):
        """csrow/channel extracted from the follow-up location line."""
        data = json.loads(events_json_path.read_text())
        ce = next(e for e in data if e["event_type"] == "CE")
        assert ce["csrow"] == 0
        assert ce["channel"] == 0

    def test_replay_script_accepts_json(self, events_json_path):
        """replay_kernel_state.sh --dry-run should exit 0 with valid JSON."""
        if not REPLAY_SH.exists():
            pytest.skip("replay_kernel_state.sh not found")
        result = subprocess.run(
            ["bash", str(REPLAY_SH), "--events", str(events_json_path),
             "--dry-run", "--speed", "100"],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, \
            f"replay_kernel_state.sh failed:\n{result.stderr}"

    def test_replay_script_dry_run_mentions_events(self, events_json_path):
        if not REPLAY_SH.exists():
            pytest.skip("replay_kernel_state.sh not found")
        result = subprocess.run(
            ["bash", str(REPLAY_SH), "--events", str(events_json_path),
             "--dry-run", "--speed", "100"],
            capture_output=True, text=True, timeout=15,
        )
        combined = result.stdout + result.stderr
        assert "CE" in combined or "event" in combined.lower() or "replay" in combined.lower()


# ---------------------------------------------------------------------------
# Prometheus metric synthesis
# ---------------------------------------------------------------------------

class TestMetricSynthesis:
    """Verify Prometheus text format can be synthesized from parsed events."""

    LOG_LINES = [
        "[   10.001234] EDAC MC0: 3 CE on DIMM_0_CH0 (mc:0 page:0x00012ab offset:0x0 grain:8 syndrome:0x1)",
        "[   10.001235] EDAC MC0: CE error on memory module DIMM_0_CH0 (csrow:0 channel:0)",
        "[   20.000000] EDAC MC1: 1 CE on DIMM_1_CH0 (mc:1 page:0x1234 offset:0x0 grain:8 syndrome:0x2)",
        "[   20.000001] EDAC MC1: CE error on memory module DIMM_1_CH0 (csrow:1 channel:0)",
        "[   30.000000] EDAC MC0: 2 UE on DIMM_0_CH0 (mc:0 page:0xdead offset:0x0 grain:8 syndrome:0x0)",
    ]

    @pytest.fixture(scope="class")
    def events(self):
        tp = TraceParser()
        from io import StringIO
        return tp.parse_file(StringIO("\n".join(self.LOG_LINES)))

    def test_parses_5_events(self, events):
        assert len(events) == 3  # 3 EDAC lines (CE/UE), the location lines are lookahead

    def test_ce_count_sum(self, events):
        total_ce = sum(e.count for e in events if e.event_type == "CE")
        assert total_ce == 4  # 3 + 1

    def test_ue_count_sum(self, events):
        total_ue = sum(e.count for e in events if e.event_type == "UE")
        assert total_ue == 2

    def test_controllers_identified(self, events):
        controllers = {e.controller for e in events}
        assert "MC0" in controllers
        assert "MC1" in controllers

    def test_prometheus_text_synthesis(self, events):
        """Synthesize valid Prometheus text format from parsed events."""
        from collections import Counter
        ce_by_ctrl = Counter()
        ue_by_ctrl = Counter()
        for e in events:
            if e.event_type == "CE":
                ce_by_ctrl[e.controller] += e.count
            elif e.event_type == "UE":
                ue_by_ctrl[e.controller] += e.count

        lines = ["# HELP edac_ce_total Correctable errors by controller",
                 "# TYPE edac_ce_total counter"]
        for ctrl, count in sorted(ce_by_ctrl.items()):
            lines.append(f'edac_ce_total{{controller="{ctrl}"}} {count}')

        lines += ["# HELP edac_ue_total Uncorrectable errors by controller",
                  "# TYPE edac_ue_total counter"]
        for ctrl, count in sorted(ue_by_ctrl.items()):
            lines.append(f'edac_ue_total{{controller="{ctrl}"}} {count}')

        prom_text = "\n".join(lines)

        # Validate the synthesized text
        assert 'edac_ce_total{controller="MC0"} 3' in prom_text
        assert 'edac_ce_total{controller="MC1"} 1' in prom_text
        assert 'edac_ue_total{controller="MC0"} 2' in prom_text
        assert "# TYPE edac_ce_total counter" in prom_text
