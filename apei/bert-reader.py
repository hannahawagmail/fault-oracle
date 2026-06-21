#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
apei/bert-reader.py — Parse ACPI BERT (Boot Error Record Table) persistent records.

The BERT region at /sys/firmware/acpi/tables/BERT (or raw sysfs) exposes
hardware errors that the firmware detected prior to OS boot — typically from
the previous boot's fatal error. This reader parses the raw binary using
Python ctypes and emits structured output (JSON or Prometheus textfile).

ACPI BERT layout (ACPI 6.4 §18.3.1):
  - BERT header: 4 bytes signature, 4 bytes length, 1 byte checksum, ...
  - Boot Error Region: length (4B) + offset (8B) + error entries
  - Each entry: CPER (Common Platform Error Record) header

Reference: ACPI 6.4 spec, §18.3

Usage:
    python3 apei/bert-reader.py [--bert PATH] [--output json|text|prom]
"""
import argparse
import json
import struct
import sys
import time
from pathlib import Path

# Default sysfs paths
BERT_REGION_PATH = Path("/sys/firmware/acpi/tables/data/BERT")
BERT_TABLE_PATH  = Path("/sys/firmware/acpi/tables/BERT")

# CPER Section Type GUIDs (abridged — most common)
CPER_SECTION_TYPES = {
    bytes.fromhex("d995e954947c435397c9dc3e8a69240c"): "processor_generic",
    bytes.fromhex("1c15102f5cda4f43b18adca4a3f9360e"): "platform_memory",
    bytes.fromhex("e429faf1a23840dbb10b55ff6aaa68e4"): "pcie",
    bytes.fromhex("81212a9664c843b29ef19f7d2885b2d0"): "firmware_error",
    bytes.fromhex("5b51fe6c76d14e9e8c1dd2b5d4820e83"): "dmar_generic",
}

SEVERITY_MAP = {0: "recoverable", 1: "fatal", 2: "corrected", 3: "informational"}

# BERT table header (common ACPI table header = 36 bytes)
ACPI_TABLE_HEADER_FMT = "<4sIBB6s8sI4sI"
ACPI_TABLE_HEADER_SIZE = struct.calcsize(ACPI_TABLE_HEADER_FMT)

# BERT-specific body: BootErrorRegionLength (4B) + BootErrorRegionOffset (8B)
BERT_BODY_FMT = "<IQ"
BERT_BODY_SIZE = struct.calcsize(BERT_BODY_FMT)

# CPER record header (128 bytes per UEFI 2.9 Appendix N)
CPER_HEADER_FMT = "<16sIHBB16sQIIQQ16sI4sI"
CPER_HEADER_SIZE = struct.calcsize(CPER_HEADER_FMT)

# CPER section descriptor (72 bytes)
CPER_SECTION_DESC_FMT = "<IH16sIIBBHI"
CPER_SECTION_DESC_SIZE = struct.calcsize(CPER_SECTION_DESC_FMT)


def parse_bert_table(data: bytes) -> tuple:
    """Return (region_length, region_offset) from BERT table bytes."""
    if len(data) < ACPI_TABLE_HEADER_SIZE + BERT_BODY_SIZE:
        raise ValueError(f"BERT table too short: {len(data)} bytes")
    body_start = ACPI_TABLE_HEADER_SIZE
    region_length, region_offset = struct.unpack_from(BERT_BODY_FMT, data, body_start)
    return region_length, region_offset


def parse_cper_records(region_data: bytes) -> list:
    """Parse all CPER records from the BERT boot error region."""
    records = []
    offset = 0
    while offset + CPER_HEADER_SIZE <= len(region_data):
        try:
            struct.unpack_from(CPER_HEADER_FMT, region_data, offset)
        except struct.error:
            break
        # CPER header fields (per UEFI 2.9 Appendix N):
        # [0]=SignatureStart(16B guid), [1]=Revision(4B), [2]=SignatureEnd(2B),
        # [3]=SectionCount, [4]=Severity, [5]=ValidationBits(16B), [6]=RecordLength, ...
        # Simplified extraction for the fields we care about:
        # Signature = first 16B (should be "CPER" padded with null)
        sig = region_data[offset:offset+4]
        if sig != b"CPER":
            break   # Not a valid CPER record

        record_length  = struct.unpack_from("<I", region_data, offset + 20)[0]
        severity_raw   = struct.unpack_from("<I", region_data, offset + 24)[0]
        section_count  = struct.unpack_from("<H", region_data, offset + 18)[0]
        severity       = SEVERITY_MAP.get(severity_raw, f"unknown({severity_raw})")

        # Section descriptors start after 128-byte header
        sections = []
        desc_offset = offset + 128
        for _ in range(section_count):
            if desc_offset + CPER_SECTION_DESC_SIZE > len(region_data):
                break
            sect_offset, sect_length = struct.unpack_from("<IH", region_data, desc_offset)
            sect_type_guid = region_data[desc_offset + 4:desc_offset + 20]
            sect_type = CPER_SECTION_TYPES.get(
                bytes(sect_type_guid[:16]), "unknown"
            )
            sections.append({
                "type": sect_type,
                "length": sect_length,
                "offset_in_record": sect_offset,
            })
            desc_offset += CPER_SECTION_DESC_SIZE

        records.append({
            "severity":      severity,
            "section_count": section_count,
            "record_length": record_length,
            "sections":      sections,
        })

        if record_length < CPER_HEADER_SIZE:
            break
        offset += record_length

    return records


def read_bert_region(bert_table_path: Path) -> bytes:
    """Read the raw BERT region data from sysfs."""
    return bert_table_path.read_bytes()


def emit_prometheus(records: list) -> str:
    """Emit Prometheus textfile metrics from parsed BERT records."""
    from collections import Counter
    counts_by_severity: Counter = Counter()
    counts_by_type: Counter = Counter()
    for r in records:
        counts_by_severity[r["severity"]] += 1
        for s in r["sections"]:
            counts_by_type[s["type"]] += 1

    lines = []
    lines.append("# HELP apei_bert_record_total ACPI BERT boot error records by severity.")
    lines.append("# TYPE apei_bert_record_total gauge")
    for sev, cnt in counts_by_severity.items():
        lines.append(f'apei_bert_record_total{{severity="{sev}"}} {cnt}')
    if not counts_by_severity:
        lines.append('apei_bert_record_total{severity="none"} 0')

    lines.append("# HELP apei_bert_section_total ACPI BERT sections by error type.")
    lines.append("# TYPE apei_bert_section_total gauge")
    for etype, cnt in counts_by_type.items():
        lines.append(f'apei_bert_section_total{{error_type="{etype}"}} {cnt}')

    lines.append("# HELP apei_bert_last_read_timestamp Unix timestamp of last BERT read.")
    lines.append("# TYPE apei_bert_last_read_timestamp gauge")
    lines.append(f"apei_bert_last_read_timestamp {time.time():.3f}")

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bert", type=Path, default=BERT_TABLE_PATH,
                        help="Path to BERT table (from sysfs or file)")
    parser.add_argument("--output", choices=["json", "text", "prom"], default="text")
    args = parser.parse_args()

    if not args.bert.exists():
        sys.stderr.write(f"bert-reader: BERT table not found at {args.bert} (APEI not available on this platform)\n")
        if args.output == "prom":
            print("# HELP apei_bert_record_total ACPI BERT boot error records.")
            print("# TYPE apei_bert_record_total gauge")
            print('apei_bert_record_total{severity="none"} 0')
            print(f"apei_bert_last_read_timestamp {time.time():.3f}")
        sys.exit(0)

    raw = read_bert_region(args.bert)

    try:
        region_length, region_offset = parse_bert_table(raw)
    except ValueError as e:
        sys.stderr.write(f"bert-reader: parse error: {e}\n")
        sys.exit(1)

    # For sysfs BERT, region data follows the table header
    region_data = raw[region_offset:region_offset + region_length] if region_offset > 0 else raw[ACPI_TABLE_HEADER_SIZE + BERT_BODY_SIZE:]
    records = parse_cper_records(region_data)

    if args.output == "json":
        print(json.dumps({"records": records, "count": len(records)}, indent=2))
    elif args.output == "prom":
        print(emit_prometheus(records))
    else:
        print(f"BERT records found: {len(records)}")
        for i, r in enumerate(records):
            print(f"  [{i}] severity={r['severity']} sections={r['section_count']}")
            for s in r["sections"]:
                print(f"       type={s['type']} length={s['length']}")


if __name__ == "__main__":
    main()
