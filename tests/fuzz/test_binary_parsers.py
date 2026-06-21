# SPDX-License-Identifier: Apache-2.0
"""
Fuzz tests for binary and text parsers using Hypothesis.

Goal: no input (valid or garbage) causes an unhandled exception.
Parsers must return a valid result or raise ValueError/struct.error —
never AttributeError, IndexError, KeyError, or similar.
"""
import importlib.util
import csv
import struct
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Helper: load hyphen-named modules
# ---------------------------------------------------------------------------

REPO = Path(__file__).parent.parent.parent

def _import(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Lazy module references — loaded on first use
_bert    = None
_ib      = None
_pmem    = None
_sel_mod = None


def _bert_mod():
    global _bert
    if _bert is None:
        _bert = _import("bert_reader_fuzz", REPO / "apei" / "bert-reader.py")
    return _bert


def _ib_mod():
    global _ib
    if _ib is None:
        _ib = _import("ib_collector_fuzz", REPO / "network" / "ib_collector.py")
    return _ib


def _pmem_mod():
    global _pmem
    if _pmem is None:
        _pmem = _import("pmem_collector_fuzz", REPO / "power_cxl" / "pmem-collector.py")
    return _pmem


def _bmc_sel_mod():
    global _sel_mod
    if _sel_mod is None:
        _sel_mod = _import("ipmi_sel_fuzz", REPO / "bmc" / "ipmi-sel-poller.py")
    return _sel_mod


# ---------------------------------------------------------------------------
# 1. BERT binary parser — parse_cper_records / parse_bert_table
# ---------------------------------------------------------------------------

@given(st.binary(min_size=0, max_size=512))
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_bert_cper_parser_never_crashes(data):
    """parse_cper_records must never raise unexpected exceptions on random bytes."""
    mod = _bert_mod()
    try:
        result = mod.parse_cper_records(data)
        assert isinstance(result, list)
    except (ValueError, struct.error, UnicodeDecodeError):
        pass  # These are acceptable failures for garbage input


@given(st.binary(min_size=0, max_size=512))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_bert_table_parser_never_crashes(data):
    """parse_bert_table must never raise unexpected exceptions on random bytes."""
    mod = _bert_mod()
    try:
        result = mod.parse_bert_table(data)
        assert isinstance(result, tuple)
        assert len(result) == 2
    except (ValueError, struct.error, UnicodeDecodeError):
        pass  # Expected for too-short or garbage input


# ---------------------------------------------------------------------------
# 2. EDAC counter file parser — int(s.strip()) pattern
# ---------------------------------------------------------------------------

@given(st.text(min_size=0, max_size=50))
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_edac_counter_parse_handles_any_string(s):
    """
    Inline the EDAC counter-file parsing pattern (int(s.strip())).
    Must never raise anything other than ValueError / OverflowError.
    """
    try:
        val = int(s.strip())
        assert isinstance(val, int)
    except (ValueError, OverflowError):
        pass  # Expected for non-integer strings


# ---------------------------------------------------------------------------
# 3. IPMI SEL CSV line parser
# ---------------------------------------------------------------------------

@given(st.lists(st.text(max_size=30), min_size=0, max_size=8))
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_sel_csv_parser_never_crashes(fields):
    """parse_sel_csv must never raise unexpected exceptions for any CSV input."""
    mod = _bmc_sel_mod()
    line = ",".join(str(f) for f in fields)
    try:
        result = mod.parse_sel_csv(line + "\n")
        assert isinstance(result, list)
    except (ValueError, OverflowError, UnicodeDecodeError, csv.Error):
        pass  # Acceptable for malformed input (csv.Error for embedded newlines etc.)


# ---------------------------------------------------------------------------
# 4. perfquery output parser — parse_perfquery
# ---------------------------------------------------------------------------

PERFQUERY_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:. \n_-"


@given(st.text(alphabet=PERFQUERY_ALPHABET, max_size=300))
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_perfquery_parser_never_crashes(output):
    """parse_perfquery must always return a dict regardless of input."""
    mod = _ib_mod()
    result = mod.parse_perfquery(output)
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# 5. ndctl JSON parser — parse_health_output
# ---------------------------------------------------------------------------

@given(st.one_of(
    st.just(""),
    st.just("[]"),
    st.just("{}"),
    st.just("[null]"),
    st.text(max_size=100),
    st.binary(max_size=50).map(lambda b: b.decode("utf-8", errors="replace")),
))
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_ndctl_json_parser_handles_garbage(output):
    """parse_health_output must always return a list (never crash)."""
    mod = _pmem_mod()
    result = mod.parse_health_output(output)
    assert isinstance(result, list)
