# SPDX-License-Identifier: Apache-2.0
import sys
import time
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from dmesg_watcher import parse_dmesg_line, PATTERNS
from event_correlator import EventBuffer, detect_correlations


def _event(event_type: str, ts_offset: float = 0) -> dict:
    return {"event_type": event_type, "timestamp": time.time() + ts_offset,
            "severity": "warning", "message": "test"}


class TestParseDmesgLine:
    def test_edac_ce_matched(self):
        line = "[  123.456789] EDAC MC0: 3 CE"
        e = parse_dmesg_line(line)
        assert e is not None
        assert e["event_type"] == "edac_ce"
        assert e["severity"] == "warning"

    def test_edac_ue_matched(self):
        line = "[  200.000000] EDAC MC1: 1 UE"
        e = parse_dmesg_line(line)
        assert e["event_type"] == "edac_ue"
        assert e["severity"] == "critical"

    def test_pcie_aer_fatal(self):
        line = "[  300.0] pcieport 0000:00:01.0: AER: Uncorrected (Fatal) error"
        e = parse_dmesg_line(line)
        assert e["event_type"] == "pcie_aer_fatal"

    def test_non_matching_line_returns_none(self):
        assert parse_dmesg_line("[  1.0] usb 1-1: new high-speed USB") is None

    def test_no_timestamp_returns_none(self):
        assert parse_dmesg_line("some random dmesg output") is None

    def test_kernel_ts_extracted(self):
        line = "[99.123456] EDAC MC0: 1 CE"
        e = parse_dmesg_line(line)
        assert abs(e["kernel_ts"] - 99.123456) < 1e-5


class TestEventBuffer:
    def test_add_and_count(self):
        buf = EventBuffer(window_s=60)
        buf.add(_event("edac_ce"))
        assert buf.count_by_type()["edac_ce"] == 1

    def test_expiry(self):
        buf = EventBuffer(window_s=1)
        buf.add({"event_type": "edac_ce", "timestamp": time.time() - 2})
        assert buf.count_by_type().get("edac_ce", 0) == 0

    def test_multiple_types(self):
        buf = EventBuffer()
        buf.add(_event("edac_ce"))
        buf.add(_event("mce"))
        assert buf.count_by_type()["edac_ce"] == 1
        assert buf.count_by_type()["mce"] == 1


class TestDetectCorrelations:
    def test_memory_multi_source_detected(self):
        buf = EventBuffer()
        buf.add(_event("edac_ce"))
        buf.add(_event("mce"))
        corrs = detect_correlations(buf)
        assert any(c["type"] == "memory_multi_source" for c in corrs)

    def test_pcie_memory_cascade_detected(self):
        buf = EventBuffer()
        buf.add(_event("pcie_aer_fatal"))
        buf.add(_event("edac_ue"))
        corrs = detect_correlations(buf)
        assert any(c["type"] == "pcie_memory_cascade" for c in corrs)

    def test_event_storm_detected(self):
        buf = EventBuffer()
        for _ in range(3):
            buf.add(_event("edac_ce"))
        corrs = detect_correlations(buf)
        assert any(c["type"] == "event_storm" for c in corrs)

    def test_no_false_positive_single_type(self):
        buf = EventBuffer()
        buf.add(_event("edac_ce"))
        corrs = detect_correlations(buf)
        # No multi-source or cascade; storm requires ≥3
        assert not any(c["type"] in ("memory_multi_source", "pcie_memory_cascade") for c in corrs)

    def test_empty_buffer_no_correlations(self):
        buf = EventBuffer()
        assert detect_correlations(buf) == []
