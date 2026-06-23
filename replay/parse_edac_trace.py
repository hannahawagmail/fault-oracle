#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
parse_edac_trace.py — Parse kernel EDAC / AER log traces into structured JSON.

Reads a kernel ring buffer log (dmesg output, journald export, or a captured
log file) and extracts hardware error events into a structured JSON format
suitable for postmortem analysis and replay via replay_kernel_state.sh.

Supported log formats:
  - Raw dmesg:      "[123456.789012] EDAC MC0: 1 CE ..."
  - Journald -o short-monotonic: "[  1234.567890] EDAC ..."
  - syslog with timestamps: "Jun  5 12:34:56 host kernel: EDAC MC0: ..."
  - Stripped (no timestamp): "EDAC MC0: 1 CE ..."

Output JSON schema (one object per event):
  {
    "seq": <int>,                  # event sequence number (0-based)
    "timestamp_str": "<string>",   # original timestamp string
    "timestamp_ns": <int>,         # monotonic nanoseconds (monotonic_s × 1e9), or 0
                                   # NOTE: kernel-monotonic since boot, NOT epoch.
    "monotonic_s": <float>,        # kernel monotonic time in seconds (if available)
    "event_type": "CE"|"UE"|"AER_CE"|"AER_UE"|"MCE",
    "subsystem": "EDAC"|"AER"|"MCE",
    "controller": "<string>",      # e.g., "MC0"
    "csrow": <int>|null,
    "channel": <int>|null,
    "count": <int>,                # number of errors in this event
    "page": <int>|null,            # physical page number (hex → int)
    "offset": <int>|null,          # offset within page
    "grain": <int>|null,           # ECC grain size in bytes
    "syndrome": "<string>"|null,   # syndrome word (hex string)
    "pci_device": "<string>"|null, # BDF for AER events
    "aer_error_type": "<string>"|null,
    "raw_message": "<string>"      # original log line
  }

Usage:
  python3 parse_edac_trace.py --input dmesg.log --output events.json
  python3 parse_edac_trace.py --input dmesg.log --output events.json --pretty
  python3 parse_edac_trace.py --input dmesg.log --event-types CE,UE --output events.json
  dmesg | python3 parse_edac_trace.py --input - --output events.json

Author: Hanna Hawa (github.com/hanna-hawa)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class HardwareEvent:
    seq: int
    timestamp_str: str
    # Monotonic nanoseconds since boot (monotonic_s × 1e9), NOT epoch time.
    # 0 when no kernel monotonic timestamp was present in the source line.
    timestamp_ns: int
    monotonic_s: float | None
    event_type: str
    subsystem: str
    controller: str
    csrow: int | None
    channel: int | None
    count: int
    page: int | None
    offset: int | None
    grain: int | None
    syndrome: str | None
    pci_device: str | None
    aer_error_type: str | None
    raw_message: str


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Kernel monotonic timestamp: "[123456.789012]" or "[  1234.567890]"
RE_MONOTONIC = re.compile(r"^\[\s*(\d+\.\d+)\]")

# syslog timestamp: "Jun  5 12:34:56"
RE_SYSLOG_TS = re.compile(r"^(\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2})\s")

# EDAC correctable/uncorrectable — match controller and count only.
# Details (page/offset/grain/syndrome) are extracted separately via RE_EDAC_DETAILS
# because the lazy .*? between the type keyword and the parenthesised detail block
# causes all optional groups to be skipped by the regex engine.
RE_EDAC_CE = re.compile(r"EDAC\s+(MC\d+):\s+(\d+)\s+CE\b")
RE_EDAC_UE = re.compile(r"EDAC\s+(MC\d+):\s+(\d+)\s+UE\b")

# Detail block inside parentheses:
# "(mc:0 page:0x00012ab offset:0x0 grain:8 syndrome:0x0000000000000001)"
RE_EDAC_DETAILS = re.compile(
    r"\("
    r"(?:mc:\d+\s+)?"
    r"(?:page:0x([0-9a-fA-F]+)\s*)?"
    r"(?:offset:0x([0-9a-fA-F]+)\s*)?"
    r"(?:grain:(\d+)\s*)?"
    r"(?:syndrome:0x([0-9a-fA-F]+))?"
)

# EDAC csrow/channel from "DIMM location" string (may appear on next line):
# "(csrow:0 channel:1)" or "csrow:0 channel:1"
RE_EDAC_LOCATION = re.compile(r"csrow:(\d+)\s+channel:(\d+)")

# AER correctable error:
# "pcieport 0000:00:01.0: AER: Corrected error received: 0000:01:00.0"
RE_AER_CORRECTED = re.compile(r"AER:\s+Corrected error received:\s+([\da-fA-F:.]+)")

# AER uncorrectable error:
# "pcieport 0000:00:01.0: AER: Uncorrected (Non-Fatal) error received: 0000:01:00.0"
RE_AER_UNCORRECTED = re.compile(
    r"AER:\s+Uncorrected\s+\(([\w-]+)\)\s+error received:\s+([\da-fA-F:.]+)"
)

# AER specific error type (follows the device line):
# "  [  6] Bad TLP"
RE_AER_ERROR_TYPE = re.compile(r"\[\s*\d+\]\s+(.+)")

# MCE (machine check):
# "mce: [Hardware Error]: Machine check events logged"
RE_MCE = re.compile(r"(?:mce:|Machine check|MCE\s+\d+).*Hardware Error", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class TraceParser:
    def __init__(self, event_types=None):
        # Use `is not None` so that an explicit empty list [] means "no types"
        # rather than falling back to the default set of all types.
        if event_types is not None:
            self.event_types = set(event_types)
        else:
            self.event_types = {"CE", "UE", "AER_CE", "AER_UE", "MCE"}
        self.seq = 0
        self._pending_aer_device = None
        self._pending_aer_severity = None

    def parse_file(self, fp) -> list[HardwareEvent]:
        events = []
        lines = fp.readlines()

        for i, line in enumerate(lines):
            line = line.rstrip("\n")
            if not line.strip():
                continue

            event = self._parse_line(line, lines, i)
            if event and event.event_type in self.event_types:
                events.append(event)

        return events

    def _parse_line(self, line: str, all_lines: list, line_idx: int) -> HardwareEvent | None:
        # Extract timestamp
        monotonic_s, timestamp_str = self._extract_timestamp(line)
        # Strip timestamp for pattern matching
        clean = re.sub(r"^\[[\s\d.]+\]\s*", "", line)
        clean = re.sub(r"^\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\S+\s+kernel:\s*", "", clean)

        # Try each pattern
        if "EDAC" in clean:
            # CE — guard against "N UE" appearing before "CE" (shouldn't happen but be safe)
            m = RE_EDAC_CE.search(clean)
            if m and "UE" not in clean[: m.start()]:
                # Location info may be on the NEXT line in some kernel versions
                loc_line = self._look_ahead_edac_location(all_lines, line_idx)
                combined = clean + " " + loc_line
                return self._make_edac_event("CE", m, combined, line, monotonic_s, timestamp_str)
            # UE
            m = RE_EDAC_UE.search(clean)
            if m:
                loc_line = self._look_ahead_edac_location(all_lines, line_idx)
                combined = clean + " " + loc_line
                return self._make_edac_event("UE", m, combined, line, monotonic_s, timestamp_str)

        if "AER" in clean:
            # AER correctable
            m = RE_AER_CORRECTED.search(clean)
            if m:
                self._pending_aer_device = m.group(1)
                self._pending_aer_severity = "AER_CE"
                aer_type = self._look_ahead_aer_type(all_lines, line_idx)
                return self._make_aer_event(
                    "AER_CE", m.group(1), aer_type, line, monotonic_s, timestamp_str
                )
            # AER uncorrectable
            m = RE_AER_UNCORRECTED.search(clean)
            if m:
                aer_type = self._look_ahead_aer_type(all_lines, line_idx)
                return self._make_aer_event(
                    "AER_UE", m.group(2), aer_type, line, monotonic_s, timestamp_str
                )

        if RE_MCE.search(clean):
            return self._make_mce_event(clean, line, monotonic_s, timestamp_str)

        return None

    def _extract_timestamp(self, line: str):
        m = RE_MONOTONIC.match(line)
        if m:
            mono = float(m.group(1))
            return mono, f"[{m.group(1)}]"

        m = RE_SYSLOG_TS.match(line)
        if m:
            ts_str = m.group(1)
            try:
                time.strptime(ts_str + f" {time.localtime().tm_year}", "%b %d %H:%M:%S %Y")
                return None, ts_str
            except ValueError:
                return None, ts_str

        return None, ""

    def _look_ahead_aer_type(self, lines: list, idx: int, lookahead: int = 5) -> str | None:
        """Look ahead up to `lookahead` lines for an AER error type annotation."""
        for j in range(idx + 1, min(idx + lookahead + 1, len(lines))):
            m = RE_AER_ERROR_TYPE.search(lines[j])
            if m:
                return m.group(1).strip()
        return None

    def _look_ahead_edac_location(self, lines: list, idx: int, lookahead: int = 3) -> str:
        """Look ahead up to `lookahead` lines for csrow/channel location info.

        Some kernel EDAC drivers emit the DIMM location on a separate follow-up
        line rather than inline.  Return the raw line text (stripped) so the
        caller can append it to `clean` before regex matching.
        """
        for j in range(idx + 1, min(idx + lookahead + 1, len(lines))):
            next_line = lines[j].rstrip("\n")
            if RE_EDAC_LOCATION.search(next_line):
                return next_line
        return ""

    def _make_edac_event(
        self, event_type: str, m, clean: str, raw: str, monotonic_s, timestamp_str
    ) -> HardwareEvent:
        controller = m.group(1)  # e.g., "MC0"
        count = int(m.group(2))

        # Extract detail fields from the parenthesised block via RE_EDAC_DETAILS
        dm = RE_EDAC_DETAILS.search(clean)
        page = int(dm.group(1), 16) if dm and dm.group(1) else None
        offset = int(dm.group(2), 16) if dm and dm.group(2) else None
        grain = int(dm.group(3)) if dm and dm.group(3) else None
        syndrome = dm.group(4) if dm and dm.group(4) else None

        # Extract csrow/channel (inline or from look-ahead line appended to clean)
        csrow, channel = None, None
        loc_m = RE_EDAC_LOCATION.search(clean)
        if loc_m:
            csrow = int(loc_m.group(1))
            channel = int(loc_m.group(2))

        ev = HardwareEvent(
            seq=self.seq,
            timestamp_str=timestamp_str,
            timestamp_ns=int(monotonic_s * 1e9) if monotonic_s else 0,
            monotonic_s=monotonic_s,
            event_type=event_type,
            subsystem="EDAC",
            controller=controller,
            csrow=csrow,
            channel=channel,
            count=count,
            page=page,
            offset=offset,
            grain=grain,
            syndrome=syndrome,
            pci_device=None,
            aer_error_type=None,
            raw_message=raw.strip(),
        )
        self.seq += 1
        return ev

    def _make_aer_event(
        self,
        event_type: str,
        device: str,
        aer_type: str | None,
        raw: str,
        monotonic_s,
        timestamp_str,
    ) -> HardwareEvent:
        ev = HardwareEvent(
            seq=self.seq,
            timestamp_str=timestamp_str,
            timestamp_ns=int(monotonic_s * 1e9) if monotonic_s else 0,
            monotonic_s=monotonic_s,
            event_type=event_type,
            subsystem="AER",
            controller="",
            csrow=None,
            channel=None,
            count=1,
            page=None,
            offset=None,
            grain=None,
            syndrome=None,
            pci_device=device,
            aer_error_type=aer_type,
            raw_message=raw.strip(),
        )
        self.seq += 1
        return ev

    def _make_mce_event(self, clean: str, raw: str, monotonic_s, timestamp_str) -> HardwareEvent:
        ev = HardwareEvent(
            seq=self.seq,
            timestamp_str=timestamp_str,
            timestamp_ns=int(monotonic_s * 1e9) if monotonic_s else 0,
            monotonic_s=monotonic_s,
            event_type="MCE",
            subsystem="MCE",
            controller="",
            csrow=None,
            channel=None,
            count=1,
            page=None,
            offset=None,
            grain=None,
            syndrome=None,
            pci_device=None,
            aer_error_type=None,
            raw_message=raw.strip(),
        )
        self.seq += 1
        return ev


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def print_stats(events: list[HardwareEvent]) -> None:
    from collections import Counter

    type_counts = Counter(e.event_type for e in events)
    controller_counts = Counter(e.controller for e in events if e.controller)

    print("\n=== Parse Statistics ===", file=sys.stderr)
    print(f"Total events parsed: {len(events)}", file=sys.stderr)
    for etype, count in sorted(type_counts.items()):
        print(f"  {etype}: {count}", file=sys.stderr)
    if controller_counts:
        print("By controller:", file=sys.stderr)
        for ctrl, count in sorted(controller_counts.items()):
            print(f"  {ctrl}: {count}", file=sys.stderr)

    if events:
        first_mono = next((e.monotonic_s for e in events if e.monotonic_s), None)
        last_mono = next((e.monotonic_s for e in reversed(events) if e.monotonic_s), None)
        if first_mono and last_mono:
            span = last_mono - first_mono
            print(
                f"Time span: {span:.3f}s ({first_mono:.3f}s → {last_mono:.3f}s monotonic)",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input", "-i", default="-", help="Input log file (- for stdin, default: -)"
    )
    parser.add_argument(
        "--output", "-o", default="-", help="Output JSON file (- for stdout, default: -)"
    )
    parser.add_argument(
        "--event-types",
        default="CE,UE,AER_CE,AER_UE,MCE",
        help="Comma-separated event types to include (default: all)",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser.add_argument(
        "--stats",
        action="store_true",
        default=True,
        help="Print parse statistics to stderr (default: true)",
    )
    parser.add_argument("--no-stats", dest="stats", action="store_false")
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: WARNING)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='{"time": "%(asctime)s", "level": "%(levelname)s", "msg": %(message)s}',
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )
    _log.info('"parse_edac_trace starting"')

    event_types = [t.strip() for t in args.event_types.split(",")]
    parser_obj = TraceParser(event_types=event_types)

    # Read input
    if args.input == "-":
        events = parser_obj.parse_file(sys.stdin)
    else:
        with open(args.input) as f:
            events = parser_obj.parse_file(f)

    if args.stats:
        print_stats(events)

    # Serialize to JSON
    serialized = [asdict(e) for e in events]
    indent = 2 if args.pretty else None

    if args.output == "-":
        json.dump(serialized, sys.stdout, indent=indent)
        if indent:
            print()  # trailing newline for pretty output
    else:
        with open(args.output, "w") as f:
            json.dump(serialized, f, indent=indent)
        _log.info(f'"Written {len(events)} events to {args.output}"')


if __name__ == "__main__":
    main()
