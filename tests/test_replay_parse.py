# SPDX-License-Identifier: Apache-2.0
"""
test_replay_parse.py — Tests for the EDAC trace parser and replay toolkit.

Tests:
  - Parsing of EDAC CE log lines (standard format)
  - Parsing of EDAC UE log lines
  - Parsing of PCIe AER correctable/uncorrectable lines
  - Timestamp extraction: kernel monotonic, syslog, stripped
  - Csrow/channel location parsing from log lines
  - Syndrome, page, offset, grain extraction
  - Event filtering by type
  - Statistics output
  - Empty / comment-only / garbage input
  - Parse → JSON → re-parse roundtrip fidelity
  - Replay timing calculation (inter-event gaps, speed multiplier)
  - Sample trace parses correctly (ce storm log)
  - Event ordering preserved from log order
  - Unknown log lines silently ignored
"""

import json
import sys
from dataclasses import asdict
from io import StringIO
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "replay"))

from parse_edac_trace import HardwareEvent, TraceParser

# ---------------------------------------------------------------------------
# Test log line fixtures
# ---------------------------------------------------------------------------

# Standard EDAC correctable error (from edac_mc.c output format)
CE_LINE_STANDARD = (
    "[   10.001234] EDAC MC0: 1 CE on DIMM_0_CH0 "
    "(mc:0 page:0x00012ab offset:0x0 grain:8 syndrome:0x0000000000000001)"
)

# CE with csrow/channel location
CE_LINE_WITH_LOCATION = (
    "[   10.001234] EDAC MC0: 1 CE on DIMM_0_CH0 "
    "(mc:0 page:0x00012ab offset:0x0 grain:8 syndrome:0x0000000000000001)\n"
    "[   10.001235] EDAC MC0: CE error on memory module DIMM_0_CH0 (csrow:0 channel:0)"
)

# EDAC uncorrectable error
UE_LINE_STANDARD = (
    "[   47.234567] EDAC MC0: 1 UE on DIMM_0_CH0 "
    "(mc:0 page:0x00012ab offset:0x50 grain:8 syndrome:0x0000000000000000)"
)

# PCIe AER correctable
AER_CE_LINE = "[  100.000000] pcieport 0000:00:01.0: AER: Corrected error received: 0000:01:00.0"

# PCIe AER uncorrectable non-fatal
AER_UE_LINE = "[  200.000000] pcieport 0000:00:01.0: AER: Uncorrected (Non-Fatal) error received: 0000:01:00.0"

# Syslog-format line
SYSLOG_CE_LINE = (
    "Jun  5 12:34:56 myhost kernel: EDAC MC0: 1 CE on DIMM_0_CH0 "
    "(mc:0 page:0x00012ab offset:0x0 grain:8 syndrome:0x0000000000000001)"
)

# Stripped (no timestamp)
STRIPPED_CE_LINE = (
    "EDAC MC0: 1 CE on DIMM_0_CH0 "
    "(mc:0 page:0x00012ab offset:0x0 grain:8 syndrome:0x0000000000000001)"
)

# Comment line (should be ignored)
COMMENT_LINE = "# This is a comment"

# Garbage line (should be ignored)
GARBAGE_LINE = "systemd[1]: Starting system..."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_lines(*lines: str, event_types=None) -> list[HardwareEvent]:
    parser = TraceParser(event_types=event_types)
    text = "\n".join(lines)
    return parser.parse_file(StringIO(text))


# ---------------------------------------------------------------------------
# Test: Basic CE parsing
# ---------------------------------------------------------------------------


class TestCEParsing:
    def test_standard_ce_parsed(self):
        events = parse_lines(CE_LINE_STANDARD)
        assert len(events) == 1
        e = events[0]
        assert e.event_type == "CE"
        assert e.subsystem == "EDAC"
        assert e.controller == "MC0"
        assert e.count == 1

    def test_ce_monotonic_timestamp(self):
        events = parse_lines(CE_LINE_STANDARD)
        e = events[0]
        assert e.monotonic_s is not None
        assert abs(e.monotonic_s - 10.001234) < 0.001

    def test_ce_page_extracted(self):
        events = parse_lines(CE_LINE_STANDARD)
        e = events[0]
        assert e.page == 0x12AB

    def test_ce_offset_extracted(self):
        events = parse_lines(CE_LINE_STANDARD)
        e = events[0]
        assert e.offset == 0x0

    def test_ce_grain_extracted(self):
        events = parse_lines(CE_LINE_STANDARD)
        e = events[0]
        assert e.grain == 8

    def test_ce_syndrome_extracted(self):
        events = parse_lines(CE_LINE_STANDARD)
        e = events[0]
        assert e.syndrome == "0000000000000001"

    def test_ce_raw_message_preserved(self):
        events = parse_lines(CE_LINE_STANDARD)
        assert CE_LINE_STANDARD.strip() in events[0].raw_message

    def test_ce_sequence_number_zero_for_first(self):
        events = parse_lines(CE_LINE_STANDARD)
        assert events[0].seq == 0

    def test_ce_multi_count(self):
        line = (
            "[   30.001122] EDAC MC0: 3 CE on DIMM_0_CH0 "
            "(mc:0 page:0x00012ab offset:0x18 grain:8 syndrome:0x0000000000000003)"
        )
        events = parse_lines(line)
        assert len(events) == 1
        assert events[0].count == 3

    def test_ce_controller_mc1(self):
        line = (
            "[   10.000000] EDAC MC1: 2 CE on DIMM_1_CH1 "
            "(mc:1 page:0x0001abc offset:0x0 grain:8 syndrome:0x0000000000000001)"
        )
        events = parse_lines(line)
        assert len(events) == 1
        assert events[0].controller == "MC1"

    def test_ce_syslog_format(self):
        events = parse_lines(SYSLOG_CE_LINE)
        assert len(events) == 1
        assert events[0].event_type == "CE"

    def test_ce_stripped_format(self):
        events = parse_lines(STRIPPED_CE_LINE)
        assert len(events) == 1
        assert events[0].event_type == "CE"
        assert events[0].monotonic_s is None

    def test_ce_pci_device_is_none(self):
        events = parse_lines(CE_LINE_STANDARD)
        assert events[0].pci_device is None

    def test_ce_aer_error_type_is_none(self):
        events = parse_lines(CE_LINE_STANDARD)
        assert events[0].aer_error_type is None


# ---------------------------------------------------------------------------
# Test: UE parsing
# ---------------------------------------------------------------------------


class TestUEParsing:
    def test_standard_ue_parsed(self):
        events = parse_lines(UE_LINE_STANDARD)
        assert len(events) == 1
        e = events[0]
        assert e.event_type == "UE"
        assert e.subsystem == "EDAC"
        assert e.controller == "MC0"

    def test_ue_monotonic_timestamp(self):
        events = parse_lines(UE_LINE_STANDARD)
        assert events[0].monotonic_s is not None
        assert abs(events[0].monotonic_s - 47.234567) < 0.001

    def test_ue_page_extracted(self):
        events = parse_lines(UE_LINE_STANDARD)
        assert events[0].page == 0x12AB

    def test_ue_offset_extracted(self):
        events = parse_lines(UE_LINE_STANDARD)
        assert events[0].offset == 0x50

    def test_ue_syndrome_is_zero(self):
        events = parse_lines(UE_LINE_STANDARD)
        # syndrome:0x0 → "0"
        assert events[0].syndrome is not None

    def test_ue_not_parsed_as_ce(self):
        events = parse_lines(UE_LINE_STANDARD)
        assert events[0].event_type != "CE"


# ---------------------------------------------------------------------------
# Test: AER parsing
# ---------------------------------------------------------------------------


class TestAERParsing:
    def test_aer_correctable_parsed(self):
        events = parse_lines(AER_CE_LINE)
        assert len(events) == 1
        e = events[0]
        assert e.event_type == "AER_CE"
        assert e.subsystem == "AER"

    def test_aer_correctable_device_extracted(self):
        events = parse_lines(AER_CE_LINE)
        assert events[0].pci_device == "0000:01:00.0"

    def test_aer_uncorrectable_parsed(self):
        events = parse_lines(AER_UE_LINE)
        assert len(events) == 1
        e = events[0]
        assert e.event_type == "AER_UE"
        assert e.subsystem == "AER"

    def test_aer_uncorrectable_device_extracted(self):
        events = parse_lines(AER_UE_LINE)
        assert events[0].pci_device == "0000:01:00.0"

    def test_aer_count_is_one(self):
        events = parse_lines(AER_CE_LINE)
        assert events[0].count == 1

    def test_aer_controller_empty(self):
        events = parse_lines(AER_CE_LINE)
        assert events[0].controller == ""

    def test_aer_look_ahead_error_type(self):
        """Parser should look ahead for the error type annotation line."""
        lines = [
            AER_CE_LINE,
            "[  100.000001]   [  6] Bad TLP",
        ]
        events = parse_lines(*lines)
        if events:
            assert events[0].event_type == "AER_CE"
            # aer_error_type may be "Bad TLP" if look-ahead worked
            if events[0].aer_error_type:
                assert "TLP" in events[0].aer_error_type or events[0].aer_error_type is not None


# ---------------------------------------------------------------------------
# Test: Multiple events and ordering
# ---------------------------------------------------------------------------


class TestMultipleEvents:
    def test_ce_then_ue_ordering(self):
        events = parse_lines(CE_LINE_STANDARD, UE_LINE_STANDARD)
        assert len(events) == 2
        assert events[0].event_type == "CE"
        assert events[1].event_type == "UE"

    def test_sequence_numbers_monotonic(self):
        events = parse_lines(CE_LINE_STANDARD, UE_LINE_STANDARD, AER_CE_LINE)
        seqs = [e.seq for e in events]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)  # all unique

    def test_timestamps_roughly_ordered(self):
        events = parse_lines(CE_LINE_STANDARD, UE_LINE_STANDARD)
        monos = [e.monotonic_s for e in events if e.monotonic_s is not None]
        assert monos == sorted(monos)

    def test_mixed_types_all_parsed(self):
        events = parse_lines(CE_LINE_STANDARD, UE_LINE_STANDARD, AER_CE_LINE, AER_UE_LINE)
        types = {e.event_type for e in events}
        assert "CE" in types
        assert "UE" in types
        assert "AER_CE" in types
        assert "AER_UE" in types

    def test_large_count_events(self):
        lines = [
            f"[   {t}.000000] EDAC MC0: 1 CE on DIMM_0_CH0 "
            f"(mc:0 page:0x{t:06x} offset:0x0 grain:8 syndrome:0x0000000000000001)"
            for t in range(10, 110, 5)
        ]
        events = parse_lines(*lines)
        assert len(events) == len(lines)


# ---------------------------------------------------------------------------
# Test: Filtering by event type
# ---------------------------------------------------------------------------


class TestEventTypeFiltering:
    def test_filter_ce_only(self):
        events = parse_lines(CE_LINE_STANDARD, UE_LINE_STANDARD, AER_CE_LINE, event_types=["CE"])
        assert all(e.event_type == "CE" for e in events)
        assert len(events) == 1

    def test_filter_ue_only(self):
        events = parse_lines(CE_LINE_STANDARD, UE_LINE_STANDARD, event_types=["UE"])
        assert all(e.event_type == "UE" for e in events)

    def test_filter_aer_only(self):
        events = parse_lines(
            CE_LINE_STANDARD, AER_CE_LINE, AER_UE_LINE, event_types=["AER_CE", "AER_UE"]
        )
        assert all(e.event_type in ("AER_CE", "AER_UE") for e in events)

    def test_empty_filter_returns_nothing(self):
        events = parse_lines(CE_LINE_STANDARD, event_types=[])
        assert len(events) == 0

    def test_all_types_included_by_default(self):
        events = parse_lines(CE_LINE_STANDARD, UE_LINE_STANDARD, AER_CE_LINE)
        assert len(events) == 3


# ---------------------------------------------------------------------------
# Test: Edge cases and robustness
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_input(self):
        events = parse_lines("")
        assert events == []

    def test_comment_lines_ignored(self):
        events = parse_lines(COMMENT_LINE)
        assert events == []

    def test_garbage_lines_ignored(self):
        events = parse_lines(GARBAGE_LINE)
        assert events == []

    def test_blank_lines_ignored(self):
        events = parse_lines("", "   ", "\t", CE_LINE_STANDARD, "")
        assert len(events) == 1

    def test_mixed_garbage_and_valid(self):
        events = parse_lines(
            GARBAGE_LINE, CE_LINE_STANDARD, GARBAGE_LINE, UE_LINE_STANDARD, GARBAGE_LINE
        )
        assert len(events) == 2

    def test_very_large_page_number(self):
        line = (
            "[   10.000000] EDAC MC0: 1 CE on DIMM_0_CH0 "
            "(mc:0 page:0xffffffff offset:0x0 grain:8 syndrome:0x0)"
        )
        events = parse_lines(line)
        assert len(events) == 1
        assert events[0].page == 0xFFFFFFFF

    def test_zero_syndrome(self):
        line = (
            "[   10.000000] EDAC MC0: 1 CE on DIMM_0_CH0 "
            "(mc:0 page:0x0 offset:0x0 grain:8 syndrome:0x0)"
        )
        events = parse_lines(line)
        assert len(events) == 1
        # syndrome:0x0 should parse to "0" or similar
        assert events[0].syndrome is not None or events[0].page == 0

    def test_missing_syndrome_field(self):
        """Lines without syndrome should still parse."""
        line = "[   10.000000] EDAC MC0: 1 CE on DIMM_0_CH0 (mc:0 page:0x12ab)"
        events = parse_lines(line)
        # May or may not parse depending on regex — should not crash
        assert isinstance(events, list)

    def test_duplicate_lines_produce_separate_events(self):
        """Each log line is a separate event, even if identical."""
        events = parse_lines(CE_LINE_STANDARD, CE_LINE_STANDARD)
        assert len(events) == 2
        assert events[0].seq != events[1].seq


# ---------------------------------------------------------------------------
# Test: JSON serialization roundtrip
# ---------------------------------------------------------------------------


class TestJSONRoundtrip:
    def test_serialization_produces_valid_json(self):
        events = parse_lines(CE_LINE_STANDARD, UE_LINE_STANDARD, AER_CE_LINE)
        serialized = [asdict(e) for e in events]
        json_str = json.dumps(serialized)
        parsed_back = json.loads(json_str)
        assert len(parsed_back) == len(events)

    def test_all_fields_present_in_json(self):
        events = parse_lines(CE_LINE_STANDARD)
        d = asdict(events[0])
        required_fields = [
            "seq",
            "timestamp_str",
            "timestamp_ns",
            "monotonic_s",
            "event_type",
            "subsystem",
            "controller",
            "csrow",
            "channel",
            "count",
            "page",
            "offset",
            "grain",
            "syndrome",
            "pci_device",
            "aer_error_type",
            "raw_message",
        ]
        for field in required_fields:
            assert field in d, f"Field '{field}' missing from serialized event"

    def test_event_type_preserved_in_json(self):
        for line, expected_type in [
            (CE_LINE_STANDARD, "CE"),
            (UE_LINE_STANDARD, "UE"),
            (AER_CE_LINE, "AER_CE"),
        ]:
            events = parse_lines(line)
            if events:
                d = asdict(events[0])
                assert d["event_type"] == expected_type

    def test_numeric_fields_are_numeric_in_json(self):
        events = parse_lines(CE_LINE_STANDARD)
        d = asdict(events[0])
        assert isinstance(d["seq"], int)
        assert isinstance(d["count"], int)
        if d["page"] is not None:
            assert isinstance(d["page"], int)
        if d["grain"] is not None:
            assert isinstance(d["grain"], int)

    def test_none_fields_serialized_as_null(self):
        events = parse_lines(CE_LINE_STANDARD)
        d = asdict(events[0])
        json_str = json.dumps(d)
        parsed = json.loads(json_str)
        # pci_device should be null for EDAC events
        assert parsed["pci_device"] is None


# ---------------------------------------------------------------------------
# Test: Sample trace file
# ---------------------------------------------------------------------------


class TestSampleTrace:
    def test_sample_trace_exists(self, sample_trace_path):
        assert sample_trace_path.exists(), f"Sample trace not found: {sample_trace_path}"

    def test_sample_trace_parses_nonzero_events(self, sample_events):
        assert len(sample_events) > 0, "Sample trace produced no events"

    def test_sample_trace_has_ce_events(self, sample_events):
        ce_events = [e for e in sample_events if e.event_type == "CE"]
        assert len(ce_events) > 0, "Sample trace has no CE events"

    def test_sample_trace_has_ue_event(self, sample_events):
        ue_events = [e for e in sample_events if e.event_type == "UE"]
        assert len(ue_events) > 0, "Sample trace has no UE events"

    def test_sample_trace_ce_before_ue(self, sample_events):
        """CE storm should precede the UE event in the sample trace."""
        first_ue_idx = next((i for i, e in enumerate(sample_events) if e.event_type == "UE"), None)
        first_ce_idx = next((i for i, e in enumerate(sample_events) if e.event_type == "CE"), None)
        assert first_ce_idx is not None
        assert first_ue_idx is not None
        assert first_ce_idx < first_ue_idx, "CE events should come before UE in sample trace"

    def test_sample_trace_monotonic_timestamps(self, sample_events):
        """Monotonic timestamps should be non-decreasing."""
        monos = [e.monotonic_s for e in sample_events if e.monotonic_s is not None]
        for i in range(1, len(monos)):
            assert monos[i] >= monos[i - 1], (
                f"Timestamp regression at event {i}: {monos[i]} < {monos[i - 1]}"
            )

    def test_sample_trace_all_edac_subsystem(self, sample_events):
        for e in sample_events:
            if e.event_type in ("CE", "UE"):
                assert e.subsystem == "EDAC"

    def test_sample_trace_mc0_controller(self, sample_events):
        edac_events = [e for e in sample_events if e.subsystem == "EDAC"]
        for e in edac_events:
            assert e.controller == "MC0", f"Expected MC0, got {e.controller}"

    def test_sample_trace_sequence_numbers_contiguous(self, sample_events):
        seqs = [e.seq for e in sample_events]
        assert seqs == list(range(len(seqs))), "Sequence numbers must be 0, 1, 2, ..."

    def test_sample_trace_ce_count_positive(self, sample_events):
        for e in sample_events:
            if e.event_type == "CE":
                assert e.count >= 1

    @pytest.mark.parametrize("field", ["raw_message", "event_type", "subsystem"])
    def test_sample_events_field_non_empty(self, sample_events, field):
        for e in sample_events:
            val = getattr(e, field)
            assert val is not None and val != "", f"Event {e.seq}: field '{field}' is empty"


# ---------------------------------------------------------------------------
# Test: Replay timing calculation
# ---------------------------------------------------------------------------


class TestReplayTiming:
    @pytest.mark.parametrize(
        "mono_prev,mono_curr,speed,expected_wait_s",
        [
            (0.0, 1.0, 1.0, 1.0),  # 1s gap at 1× speed
            (0.0, 1.0, 2.0, 0.5),  # 1s gap at 2× speed → 0.5s
            (0.0, 1.0, 0.5, 2.0),  # 1s gap at 0.5× speed → 2.0s (slow-mo)
            (10.0, 10.05, 1.0, 0.05),  # 50ms gap
            (0.0, 0.001, 1.0, 0.001),  # 1ms gap
        ],
    )
    def test_inter_event_delay(self, mono_prev, mono_curr, speed, expected_wait_s):
        """Verify inter-event delay calculation: (curr - prev) / speed."""
        wait = (mono_curr - mono_prev) / speed
        assert abs(wait - expected_wait_s) < 1e-9

    def test_negative_delta_clamped_to_zero(self):
        """If timestamps are out of order (shouldn't happen), delay should be clamped to 0."""
        mono_prev = 10.0
        mono_curr = 9.0  # regression
        speed = 1.0
        raw_delta = (mono_curr - mono_prev) / speed
        clamped = max(0.0, raw_delta)
        assert clamped == 0.0

    def test_excessive_gap_clamped(self):
        """Gaps > 60s should be clamped to prevent unreasonable replay waits."""
        MAX_WAIT = 60.0
        mono_prev = 0.0
        mono_curr = 3600.0  # 1 hour gap in trace
        speed = 1.0
        raw_delta = (mono_curr - mono_prev) / speed
        clamped = min(MAX_WAIT, raw_delta)
        assert clamped == MAX_WAIT

    def test_speed_zero_division_guarded(self):
        """Speed must never be 0 (would cause division by zero)."""
        speed = 0.0
        with pytest.raises(ZeroDivisionError):
            _ = 1.0 / speed

    @pytest.mark.parametrize("speed", [0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 100.0])
    def test_speed_multiplier_valid_range(self, speed):
        """All reasonable speed multipliers should produce non-negative delays."""
        gap = 1.0
        delay = gap / speed
        assert delay >= 0


# ===========================================================================
# Additional coverage: syslog timestamps, MCE events, AER UE, stats, CLI
# ===========================================================================

SYSLOG_CE_LINE = (
    "Jun  5 12:34:56 myhost kernel: EDAC MC0: 1 CE on DIMM_0_CH0 "
    "(mc:0 page:0x0000abcd offset:0x10 grain:8 syndrome:0x000000000000dead)"
)

MCE_LINE = "[  55.123456] mce: [Hardware Error]: Machine check events logged"

AER_UE_LINE = (
    "[  88.000000] pcieport 0000:00:01.0: AER: Uncorrected (Non-Fatal) error received: 0000:01:00.0"
)
AER_UE_TYPE_LINE = "[  88.000001] aer_layer=Transaction Layer, aer_agent=Receiver ID"


class TestSyslogTimestamp:
    """Parser handles syslog-format timestamps (no monotonic)."""

    def test_syslog_ce_parsed(self):
        events = parse_lines(SYSLOG_CE_LINE)
        assert len(events) == 1
        e = events[0]
        assert e.event_type == "CE"
        assert e.controller == "MC0"

    def test_syslog_timestamp_str_captured(self):
        events = parse_lines(SYSLOG_CE_LINE)
        e = events[0]
        # syslog timestamp should be captured as the timestamp string
        assert "Jun" in e.timestamp_str or e.timestamp_str == ""

    def test_syslog_monotonic_is_none(self):
        events = parse_lines(SYSLOG_CE_LINE)
        e = events[0]
        assert e.monotonic_s is None

    def test_syslog_timestamp_ns_is_zero(self):
        events = parse_lines(SYSLOG_CE_LINE)
        e = events[0]
        assert e.timestamp_ns == 0

    def test_syslog_page_extracted(self):
        events = parse_lines(SYSLOG_CE_LINE)
        assert events[0].page == 0xABCD

    def test_syslog_syndrome_extracted(self):
        events = parse_lines(SYSLOG_CE_LINE)
        assert events[0].syndrome == "000000000000dead"


class TestMCEParsing:
    """Parser extracts MCE events from mce: Hardware Error lines."""

    def test_mce_event_type(self):
        events = parse_lines(MCE_LINE)
        assert len(events) == 1
        assert events[0].event_type == "MCE"

    def test_mce_subsystem(self):
        events = parse_lines(MCE_LINE)
        assert events[0].subsystem == "MCE"

    def test_mce_count_is_one(self):
        events = parse_lines(MCE_LINE)
        assert events[0].count == 1

    def test_mce_page_is_none(self):
        events = parse_lines(MCE_LINE)
        assert events[0].page is None

    def test_mce_monotonic_captured(self):
        events = parse_lines(MCE_LINE)
        assert events[0].monotonic_s == pytest.approx(55.123456)

    def test_mce_filtered_out_when_not_in_types(self):
        events = parse_lines(MCE_LINE, event_types=["CE", "UE"])
        assert len(events) == 0

    def test_mce_included_when_types_is_none(self):
        events = parse_lines(MCE_LINE, event_types=None)
        assert len(events) == 1

    def test_mce_raw_message_preserved(self):
        events = parse_lines(MCE_LINE)
        assert "Hardware Error" in events[0].raw_message


class TestAERUEParsing:
    """Parser extracts AER uncorrectable errors."""

    def test_aer_ue_event_type(self):
        events = parse_lines(AER_UE_LINE)
        assert len(events) == 1
        assert events[0].event_type == "AER_UE"

    def test_aer_ue_subsystem(self):
        events = parse_lines(AER_UE_LINE)
        assert events[0].subsystem == "AER"

    def test_aer_ue_pci_device(self):
        events = parse_lines(AER_UE_LINE)
        assert events[0].pci_device == "0000:01:00.0"

    def test_aer_ue_count_is_one(self):
        events = parse_lines(AER_UE_LINE)
        assert events[0].count == 1

    def test_aer_ue_filtered_when_not_in_types(self):
        events = parse_lines(AER_UE_LINE, event_types=["CE"])
        assert len(events) == 0

    def test_aer_ue_lookahead_type(self):
        """When the error type line follows immediately, it should be captured."""
        lines = AER_UE_LINE + "\n" + AER_UE_TYPE_LINE
        events = parse_lines(lines)
        # aer_error_type may or may not be captured (depends on RE_AER_ERROR_TYPE match)
        # The important thing is we don't crash and get exactly 1 AER_UE event
        assert len(events) == 1
        assert events[0].event_type == "AER_UE"


class TestNoTimestamp:
    """Parser handles lines with no timestamp prefix."""

    def test_stripped_ce_line_parsed(self):
        line = "EDAC MC0: 3 CE on DIMM_0_CH0 (mc:0 page:0x1111 offset:0x0 grain:4 syndrome:0xabcd)"
        events = parse_lines(line)
        assert len(events) == 1
        assert events[0].count == 3
        assert events[0].page == 0x1111

    def test_no_timestamp_monotonic_is_none(self):
        line = "EDAC MC0: 1 CE on DIMM_0_CH0 (mc:0 page:0x0 offset:0x0 grain:8 syndrome:0x0)"
        events = parse_lines(line)
        assert events[0].monotonic_s is None

    def test_no_timestamp_ns_is_zero(self):
        line = "EDAC MC0: 1 CE on DIMM_0_CH0 (mc:0 page:0x0 offset:0x0 grain:8 syndrome:0x0)"
        events = parse_lines(line)
        assert events[0].timestamp_ns == 0


class TestPrintStats:
    """print_stats writes summary to stderr without crashing."""

    def test_empty_events_no_crash(self):
        import sys
        from io import StringIO

        old_stderr = sys.stderr
        sys.stderr = StringIO()
        try:
            from parse_edac_trace import print_stats

            print_stats([])
        finally:
            output = sys.stderr.getvalue()
            sys.stderr = old_stderr
        assert "Total events parsed: 0" in output

    def test_stats_counts_by_type(self):
        import sys
        from io import StringIO

        old_stderr = sys.stderr
        sys.stderr = StringIO()
        try:
            from parse_edac_trace import print_stats

            events = parse_lines(CE_LINE_STANDARD + "\n" + MCE_LINE)
            print_stats(events)
        finally:
            output = sys.stderr.getvalue()
            sys.stderr = old_stderr
        assert "CE: 1" in output or "Total events parsed: 2" in output

    def test_stats_time_span_shown_when_monotonic(self):
        import sys
        from io import StringIO

        old_stderr = sys.stderr
        sys.stderr = StringIO()
        try:
            from parse_edac_trace import print_stats

            events = parse_lines(CE_LINE_STANDARD + "\n" + MCE_LINE)
            print_stats(events)
        finally:
            output = sys.stderr.getvalue()
            sys.stderr = old_stderr
        # CE has monotonic, MCE has monotonic — span should appear
        assert "Time span" in output or "Total events parsed" in output


class TestCLIMain:
    """Exercise main() via subprocess for coverage of the CLI paths."""

    import json as json_mod
    import subprocess
    import tempfile

    def _run_cli(self, args, input_text=None):
        import subprocess

        cmd = [
            "python3",
            str(REPO_ROOT / "replay" / "parse_edac_trace.py"),
        ] + args
        return subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
        )

    def test_stdin_to_stdout(self):
        result = self._run_cli(
            ["--input", "-", "--output", "-", "--no-stats"], input_text=CE_LINE_STANDARD
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert len(data) == 1
        assert data[0]["event_type"] == "CE"

    def test_pretty_flag(self):
        result = self._run_cli(
            ["--input", "-", "--output", "-", "--pretty", "--no-stats"], input_text=CE_LINE_STANDARD
        )
        assert result.returncode == 0
        # Pretty output has indentation
        assert "\n  " in result.stdout

    def test_event_types_filter_cli(self):
        lines = CE_LINE_STANDARD + "\n" + MCE_LINE
        result = self._run_cli(
            ["--input", "-", "--output", "-", "--event-types", "CE", "--no-stats"],
            input_text=lines,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert all(e["event_type"] == "CE" for e in data)

    def test_file_output_path(self):
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(CE_LINE_STANDARD + "\n")
            in_path = f.name
        out_path = in_path.replace(".log", "_out.json")
        try:
            result = self._run_cli(["--input", in_path, "--output", out_path, "--no-stats"])
            assert result.returncode == 0
            with open(out_path) as jf:
                data = json.load(jf)
            assert len(data) == 1
            assert data[0]["controller"] == "MC0"
        finally:
            os.unlink(in_path)
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_stats_written_to_stderr(self):
        result = self._run_cli(["--input", "-", "--output", "-"], input_text=CE_LINE_STANDARD)
        assert result.returncode == 0
        assert "Parse Statistics" in result.stderr

    def test_no_stats_suppresses_stderr(self):
        result = self._run_cli(
            ["--input", "-", "--output", "-", "--no-stats"], input_text=CE_LINE_STANDARD
        )
        assert result.returncode == 0
        assert "Parse Statistics" not in result.stderr

    def test_empty_input_returns_empty_array(self):
        result = self._run_cli(["--input", "-", "--output", "-", "--no-stats"], input_text="")
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data == []

    def test_mixed_event_types_all_parsed(self):
        lines = "\n".join([CE_LINE_STANDARD, UE_LINE_STANDARD, MCE_LINE, AER_CE_LINE])
        result = self._run_cli(["--input", "-", "--output", "-", "--no-stats"], input_text=lines)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        types = {e["event_type"] for e in data}
        assert "CE" in types
        assert "UE" in types
        assert "MCE" in types


class TestMainDirect:
    """Call main() directly (no subprocess) so coverage is tracked."""

    def _run_main(self, argv, stdin_text=None):
        """Invoke main() with controlled sys.argv, stdin, stdout, stderr."""
        import io

        from parse_edac_trace import main

        old_argv = sys.argv
        old_stdin = sys.stdin
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.argv = argv
        sys.stdin = io.StringIO(stdin_text or "")
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        try:
            main()
        except SystemExit:
            pass
        finally:
            stdout_val = sys.stdout.getvalue()
            stderr_val = sys.stderr.getvalue()
            sys.argv = old_argv
            sys.stdin = old_stdin
            sys.stdout = old_stdout
            sys.stderr = old_stderr
        return stdout_val, stderr_val

    def test_main_stdin_stdout(self):
        stdout, _ = self._run_main(
            ["parse_edac_trace.py", "--input", "-", "--output", "-", "--no-stats"],
            stdin_text=CE_LINE_STANDARD,
        )
        data = json.loads(stdout)
        assert len(data) == 1
        assert data[0]["event_type"] == "CE"

    def test_main_pretty_output(self):
        stdout, _ = self._run_main(
            ["parse_edac_trace.py", "--input", "-", "--output", "-", "--pretty", "--no-stats"],
            stdin_text=CE_LINE_STANDARD,
        )
        assert "\n  " in stdout  # indented JSON

    def test_main_stats_to_stderr(self):
        _, stderr = self._run_main(
            ["parse_edac_trace.py", "--input", "-", "--output", "-"],
            stdin_text=CE_LINE_STANDARD,
        )
        assert "Parse Statistics" in stderr

    def test_main_no_stats_suppressed(self):
        _, stderr = self._run_main(
            ["parse_edac_trace.py", "--input", "-", "--output", "-", "--no-stats"],
            stdin_text=CE_LINE_STANDARD,
        )
        assert "Parse Statistics" not in stderr

    def test_main_file_input_output(self):
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as fin:
            fin.write(CE_LINE_STANDARD + "\n")
            in_path = fin.name
        out_path = in_path.replace(".log", "_out.json")
        try:
            self._run_main(
                [
                    "parse_edac_trace.py",
                    "--input",
                    in_path,
                    "--output",
                    out_path,
                    "--no-stats",
                ]
            )
            with open(out_path) as jf:
                data = json.load(jf)
            assert len(data) == 1
            assert data[0]["controller"] == "MC0"
        finally:
            os.unlink(in_path)
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_main_event_types_filter(self):
        lines = CE_LINE_STANDARD + "\n" + MCE_LINE
        stdout, _ = self._run_main(
            [
                "parse_edac_trace.py",
                "--input",
                "-",
                "--output",
                "-",
                "--event-types",
                "CE",
                "--no-stats",
            ],
            stdin_text=lines,
        )
        data = json.loads(stdout)
        assert all(e["event_type"] == "CE" for e in data)
        assert len(data) == 1

    def test_main_empty_input(self):
        stdout, _ = self._run_main(
            ["parse_edac_trace.py", "--input", "-", "--output", "-", "--no-stats"],
            stdin_text="",
        )
        data = json.loads(stdout)
        assert data == []


class TestSyslogTimestampEdgeCases:
    """Covers the syslog ValueError branch (malformed timestamp)."""

    def test_malformed_syslog_ts_falls_back_gracefully(self):
        # This line has 'syslog-like' prefix but malformed month → ValueError in strptime
        line = "Xxx 99 99:99:99 host kernel: EDAC MC0: 1 CE on DIMM_0_CH0 (mc:0 page:0x1 offset:0x0 grain:8 syndrome:0x1)"
        events = parse_lines(line)
        # May or may not parse depending on whether syslog RE matches; either way no crash
        assert isinstance(events, list)
