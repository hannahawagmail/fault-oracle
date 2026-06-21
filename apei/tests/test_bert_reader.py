# SPDX-License-Identifier: Apache-2.0
"""Tests for apei/bert-reader.py."""
import json
import struct
import sys
import time
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from bert_reader import (
    parse_bert_table, parse_cper_records, emit_prometheus, BERT_BODY_SIZE,
    ACPI_TABLE_HEADER_SIZE, SEVERITY_MAP
)

# Build a minimal ACPI table header (36 bytes)
def acpi_header(total_len: int) -> bytes:
    sig = b"BERT"
    checksum = 0
    oem_id = b"TESTCO"
    oem_table_id = b"TESTTEST"
    oem_rev = 1
    creator_id = b"TEST"
    creator_rev = 1
    # <4sIBB6s8sI4sI = 36 bytes
    return struct.pack("<4sIBB6s8sI4sI",
        sig, total_len, checksum, 0,
        oem_id, oem_table_id, oem_rev,
        creator_id, creator_rev,
    )

def make_bert_table(region_length: int, region_offset: int = 0) -> bytes:
    hdr = acpi_header(ACPI_TABLE_HEADER_SIZE + 12)
    body = struct.pack("<IQ", region_length, region_offset)
    return hdr + body


def make_cper_record(severity: int = 2, section_count: int = 1, length: int = 256) -> bytes:
    """Build a minimal CPER record header (first 128 bytes)."""
    rec = bytearray(length)
    # Signature "CPER" at offset 0
    rec[0:4] = b"CPER"
    # Revision at offset 4 (2B)
    struct.pack_into("<H", rec, 4, 0x0204)
    # SignatureEnd at offset 6 (4B)
    struct.pack_into("<I", rec, 6, 0xFFFFFFFF)
    # SectionCount at offset 10 (2B) — Note: actual CPER has different layout;
    # our reader uses offset 18 for section_count
    struct.pack_into("<H", rec, 18, section_count)
    # RecordLength at offset 20 (4B)
    struct.pack_into("<I", rec, 20, length)
    # Severity at offset 24 (4B)
    struct.pack_into("<I", rec, 24, severity)
    return bytes(rec)


class TestParseBERTTable:
    def test_parses_region_length(self):
        data = make_bert_table(region_length=4096, region_offset=0)
        length, offset = parse_bert_table(data)
        assert length == 4096

    def test_parses_region_offset(self):
        data = make_bert_table(region_length=4096, region_offset=512)
        _, offset = parse_bert_table(data)
        assert offset == 512

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="too short"):
            parse_bert_table(b"\x00" * 10)


class TestParseCPERRecords:
    def test_single_corrected_record(self):
        region = make_cper_record(severity=2, length=256)
        records = parse_cper_records(region)
        assert len(records) == 1
        assert records[0]["severity"] == "corrected"

    def test_multiple_records(self):
        region = make_cper_record(severity=0, length=256) + make_cper_record(severity=1, length=256)
        records = parse_cper_records(region)
        assert len(records) == 2
        assert records[0]["severity"] == "recoverable"
        assert records[1]["severity"] == "fatal"

    def test_invalid_signature_stops_parsing(self):
        bad = bytearray(256)
        bad[0:4] = b"NOPE"
        records = parse_cper_records(bytes(bad))
        assert records == []

    def test_empty_region(self):
        assert parse_cper_records(b"") == []

    def test_section_count_in_record(self):
        region = make_cper_record(severity=2, section_count=3, length=512)
        records = parse_cper_records(region)
        assert records[0]["section_count"] == 3


class TestEmitPrometheus:
    def test_has_required_help_lines(self):
        out = emit_prometheus([])
        assert "# HELP apei_bert_record_total" in out
        assert "# TYPE apei_bert_record_total gauge" in out

    def test_zero_records_emits_none(self):
        out = emit_prometheus([])
        assert 'severity="none"' in out
        assert "} 0" in out

    def test_counts_by_severity(self):
        records = [
            {"severity": "corrected", "section_count": 1, "record_length": 256, "sections": []},
            {"severity": "corrected", "section_count": 1, "record_length": 256, "sections": []},
            {"severity": "fatal",     "section_count": 1, "record_length": 256, "sections": []},
        ]
        out = emit_prometheus(records)
        assert 'severity="corrected"} 2' in out
        assert 'severity="fatal"} 1' in out

    def test_timestamp_present(self):
        out = emit_prometheus([])
        assert "apei_bert_last_read_timestamp" in out
