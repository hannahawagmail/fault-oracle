# SPDX-License-Identifier: Apache-2.0
"""
test_replay_parse_property.py — Property-based (Hypothesis) tests for the
EDAC trace parser.

Tests use Hypothesis strategies to fuzz the parser with:
  - Arbitrary kernel log lines (valid and garbage)
  - Valid EDAC CE log lines with randomized numeric fields
  - Sequences of CE events with monotonically increasing timestamps
  - Lines with random whitespace, encoding edge cases, and control chars

The core invariant: TraceParser.parse_file() NEVER raises an exception and
ALWAYS returns a list (possibly empty).

Install: pip install hypothesis
"""

import sys
from io import StringIO
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "replay"))

from parse_edac_trace import HardwareEvent, TraceParser

try:
    import hypothesis.strategies as st
    from hypothesis import HealthCheck, assume, given, settings

    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not HYPOTHESIS_AVAILABLE,
    reason="hypothesis not installed — run: pip install hypothesis",
)

# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

# A strategy for generating printable ASCII text (avoids null bytes that
# some parsers mishandle, but includes spaces and punctuation).
printable_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd", "Pc", "Pd", "Zs"),
        whitelist_characters=" .:[]()_-/0123456789abcdefABCDEF",
    ),
    min_size=0,
    max_size=200,
)

# A strategy for kernel monotonic timestamps like "[  1234.567890]"
monotonic_seconds = st.floats(min_value=0.0, max_value=1e7, allow_nan=False, allow_infinity=False)


def format_monotonic(s: float) -> str:
    return f"[{s:12.6f}]"


# A strategy for EDAC controller names
controller_name = st.sampled_from(["MC0", "MC1", "MC2", "MC3"])

# A strategy for hex page addresses
hex_page = st.integers(min_value=0, max_value=0xFFFFFFFF)

# A strategy for small CE counts (typical sysfs values)
ce_count = st.integers(min_value=1, max_value=9999)

# A strategy for grain sizes (power of 2 in practice)
grain_size = st.sampled_from([1, 2, 4, 8, 16, 32, 64, 128])

# A strategy for syndrome hex strings
syndrome_hex = st.integers(min_value=0, max_value=2**64 - 1).map(lambda x: f"{x:016x}")


def make_edac_ce_line(
    ts: float, ctrl: str, count: int, page: int, offset: int, grain: int, syndrome: str
) -> str:
    """Produce a syntactically valid EDAC CE log line."""
    ts_str = format_monotonic(ts)
    return (
        f"{ts_str} EDAC {ctrl}: {count} CE on DIMM_0_CH0 "
        f"(mc:0 page:0x{page:08x} offset:0x{offset:08x} "
        f"grain:{grain} syndrome:0x{syndrome})"
    )


# ---------------------------------------------------------------------------
# Property: parser never raises on arbitrary input
# ---------------------------------------------------------------------------


@given(lines=st.lists(printable_text, min_size=0, max_size=50))
@settings(max_examples=500, suppress_health_check=[HealthCheck.too_slow])
def test_parser_never_raises_on_arbitrary_lines(lines):
    """
    The parser must never raise an exception regardless of input content.
    It should always return a list (possibly empty).
    """
    parser = TraceParser()
    fp = StringIO("\n".join(lines))
    result = parser.parse_file(fp)
    assert isinstance(result, list), f"Expected list, got {type(result)}"


@given(text=st.text(min_size=0, max_size=2000))
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_parser_never_raises_on_arbitrary_blob(text):
    """
    Feed a completely arbitrary unicode string — parser must survive.
    """
    parser = TraceParser()
    fp = StringIO(text)
    result = parser.parse_file(fp)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Property: valid CE lines produce non-negative page fields
# ---------------------------------------------------------------------------


@given(
    ts=monotonic_seconds,
    ctrl=controller_name,
    count=ce_count,
    page=hex_page,
    offset=st.integers(min_value=0, max_value=0xFFFF),
    grain=grain_size,
    syndrome=syndrome_hex,
)
@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
def test_valid_ce_line_page_field_is_non_negative(ts, ctrl, count, page, offset, grain, syndrome):
    """
    For any valid EDAC CE log line, the parsed page field must be a
    non-negative integer when present.
    """
    line = make_edac_ce_line(ts, ctrl, count, page, offset, grain, syndrome)
    parser = TraceParser()
    fp = StringIO(line + "\n")
    events = parser.parse_file(fp)

    for ev in events:
        if ev.page is not None:
            assert isinstance(ev.page, int), f"page must be int, got {type(ev.page)}"
            assert ev.page >= 0, f"page must be non-negative, got {ev.page}"
            assert ev.page == page, f"page mismatch: expected {page}, got {ev.page}"


@given(
    ts=monotonic_seconds,
    ctrl=controller_name,
    count=ce_count,
    page=hex_page,
)
@settings(max_examples=300)
def test_valid_ce_line_count_field_matches(ts, ctrl, count, page):
    """
    For any valid EDAC CE log line, the parsed count must equal the count
    encoded in the line.
    """
    line = (
        f"{format_monotonic(ts)} EDAC {ctrl}: {count} CE on DIMM_0_CH0 "
        f"(mc:0 page:0x{page:08x} offset:0x0 grain:8 syndrome:0x0000000000000001)"
    )
    parser = TraceParser()
    fp = StringIO(line + "\n")
    events = parser.parse_file(fp)

    ce_events = [e for e in events if e.event_type == "CE"]
    for ev in ce_events:
        assert ev.count == count, f"count mismatch: expected {count}, got {ev.count}"


# ---------------------------------------------------------------------------
# Property: output sequence numbers are contiguous starting at 0
# ---------------------------------------------------------------------------


@given(
    timestamps=st.lists(
        monotonic_seconds,
        min_size=1,
        max_size=30,
    ).map(sorted),  # ensure monotonically increasing
    page=hex_page,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_sequence_numbers_contiguous_from_zero(timestamps, page):
    """
    When multiple events are parsed, their seq numbers must be contiguous
    starting at 0 (i.e., 0, 1, 2, ...).
    """
    lines = []
    for i, ts in enumerate(timestamps):
        ctrl = f"MC{i % 4}"
        line = make_edac_ce_line(ts, ctrl, 1, page + i, 0, 8, "0" * 16)
        lines.append(line)

    parser = TraceParser()
    fp = StringIO("\n".join(lines) + "\n")
    events = parser.parse_file(fp)

    # Filter to only CE events (parser may skip some)
    for idx, ev in enumerate(events):
        assert ev.seq == idx, (
            f"seq at position {idx} is {ev.seq}, expected {idx} "
            f"(events must be numbered 0, 1, 2, ...)"
        )


# ---------------------------------------------------------------------------
# Property: whitespace/encoding edge cases never crash parser
# ---------------------------------------------------------------------------


@given(
    prefix=st.text(
        alphabet=st.characters(whitelist_categories=("Zs",)),
        min_size=0,
        max_size=20,
    ),
    suffix=st.text(
        alphabet=st.characters(whitelist_categories=("Zs",)),
        min_size=0,
        max_size=20,
    ),
    ts=monotonic_seconds,
    ctrl=controller_name,
)
@settings(max_examples=200)
def test_whitespace_padding_around_valid_line_does_not_crash(prefix, suffix, ts, ctrl):
    """
    Whitespace before/after a valid CE line must not crash the parser.
    """
    inner = make_edac_ce_line(ts, ctrl, 1, 0x1000, 0, 8, "0" * 16)
    line = prefix + inner + suffix
    parser = TraceParser()
    fp = StringIO(line + "\n")
    result = parser.parse_file(fp)
    assert isinstance(result, list)


@given(
    n_blanks=st.integers(min_value=0, max_value=100),
    ts=monotonic_seconds,
    ctrl=controller_name,
)
@settings(max_examples=100)
def test_many_blank_lines_interspersed(n_blanks, ts, ctrl):
    """
    Many blank lines around a valid CE line must not crash the parser.
    """
    inner = make_edac_ce_line(ts, ctrl, 1, 0x2000, 0, 8, "a" * 16)
    parts = ["\n"] * n_blanks + [inner] + ["\n"] * n_blanks
    parser = TraceParser()
    fp = StringIO("".join(parts))
    result = parser.parse_file(fp)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Property: non-EDAC lines produce no events
# ---------------------------------------------------------------------------


@given(
    lines=st.lists(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz 0123456789:-",
            min_size=1,
            max_size=80,
        ).filter(lambda s: "EDAC" not in s and "AER" not in s and "MCE" not in s),
        min_size=1,
        max_size=50,
    )
)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_non_hardware_lines_produce_no_events(lines):
    """
    Lines that do not match any hardware error pattern must produce no events.
    """
    parser = TraceParser()
    fp = StringIO("\n".join(lines) + "\n")
    events = parser.parse_file(fp)
    assert events == [], (
        f"Expected no events from non-hardware lines, got {len(events)}: "
        f"{[e.event_type for e in events]}"
    )


# ---------------------------------------------------------------------------
# Property: monotonic timestamp extraction is consistent
# ---------------------------------------------------------------------------


@given(
    seconds=st.floats(min_value=0, max_value=9_999_999, allow_nan=False, allow_infinity=False),
    ctrl=controller_name,
)
@settings(max_examples=300)
def test_monotonic_timestamp_roundtrip(seconds, ctrl):
    """
    A CE line with a known monotonic timestamp must parse to a monotonic_s
    value within float rounding tolerance.
    """
    # Format with 6 decimal places as the kernel does
    ts_formatted = f"{seconds:.6f}"
    line = (
        f"[{seconds:12.6f}] EDAC {ctrl}: 1 CE on DIMM_0_CH0 "
        f"(mc:0 page:0x00001000 offset:0x0 grain:8 syndrome:0x0000000000000001)"
    )
    parser = TraceParser()
    fp = StringIO(line + "\n")
    events = parser.parse_file(fp)

    for ev in events:
        if ev.monotonic_s is not None:
            expected = float(ts_formatted)
            assert abs(ev.monotonic_s - expected) < 1e-4, (
                f"monotonic_s {ev.monotonic_s} differs from expected {expected}"
            )


# ---------------------------------------------------------------------------
# Property: event_type is always a known string when events are produced
# ---------------------------------------------------------------------------

KNOWN_EVENT_TYPES = {"CE", "UE", "AER_CE", "AER_UE", "MCE"}


@given(lines=st.lists(printable_text, min_size=0, max_size=30))
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_all_parsed_events_have_known_event_type(lines):
    """
    Every event returned by the parser must have an event_type from the
    known set: CE, UE, AER_CE, AER_UE, MCE.
    """
    parser = TraceParser()
    fp = StringIO("\n".join(lines) + "\n")
    events = parser.parse_file(fp)

    for ev in events:
        assert ev.event_type in KNOWN_EVENT_TYPES, f"Unknown event_type: {ev.event_type!r}"


# ---------------------------------------------------------------------------
# Property: subsystem field is always consistent with event_type
# ---------------------------------------------------------------------------


@given(
    ts=monotonic_seconds,
    ctrl=controller_name,
    count=ce_count,
    page=hex_page,
)
@settings(max_examples=200)
def test_edac_events_have_edac_subsystem(ts, ctrl, count, page):
    """
    Events parsed from EDAC log lines must always have subsystem == "EDAC".
    """
    line = (
        f"{format_monotonic(ts)} EDAC {ctrl}: {count} CE on DIMM_0_CH0 "
        f"(mc:0 page:0x{page:08x} offset:0x0 grain:8 syndrome:0x0000000000000001)"
    )
    parser = TraceParser()
    fp = StringIO(line + "\n")
    events = parser.parse_file(fp)

    for ev in events:
        if ev.event_type in ("CE", "UE"):
            assert ev.subsystem == "EDAC", f"EDAC event has subsystem {ev.subsystem!r}"


# ---------------------------------------------------------------------------
# Property: parse result is a flat list (never nested or None)
# ---------------------------------------------------------------------------


@given(lines=st.lists(st.text(max_size=200), min_size=0, max_size=100))
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_result_is_always_flat_list(lines):
    """
    parse_file() must always return a flat list of HardwareEvent objects —
    never None, never nested, never a dict.
    """
    parser = TraceParser()
    fp = StringIO("\n".join(lines))
    result = parser.parse_file(fp)

    assert result is not None
    assert isinstance(result, list)
    for item in result:
        assert isinstance(item, HardwareEvent), f"Expected HardwareEvent, got {type(item)}"


# ---------------------------------------------------------------------------
# Property: multiple parse runs on the same input produce the same output
# ---------------------------------------------------------------------------


@given(
    lines=st.lists(printable_text, min_size=0, max_size=20),
)
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_deterministic_parsing(lines):
    """
    Parsing the same input twice must produce the same number of events
    (determinism). A new TraceParser is used each time so seq starts at 0.
    """
    text = "\n".join(lines) + "\n"

    parser1 = TraceParser()
    events1 = parser1.parse_file(StringIO(text))

    parser2 = TraceParser()
    events2 = parser2.parse_file(StringIO(text))

    assert len(events1) == len(events2), (
        f"Non-deterministic parse: first={len(events1)}, second={len(events2)}"
    )
    for e1, e2 in zip(events1, events2):
        assert e1.event_type == e2.event_type
        assert e1.controller == e2.controller
        assert e1.count == e2.count
