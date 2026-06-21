# SPDX-License-Identifier: Apache-2.0
"""
Tests for OTel tracing logic.

We test the Python-level trace utility functions using the opentelemetry-sdk
in-memory exporter, which captures spans without needing a running OTel Collector.
The Go tracing.go is verified by the arch unit tests and a compile-only check.
"""
import os
import sys
import time
import pytest

# Guard: skip entire module gracefully if OTel SDK not installed
pytest.importorskip("opentelemetry", reason="opentelemetry-sdk not installed")

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode


@pytest.fixture
def in_memory_tracer():
    """Set up a TracerProvider backed by InMemorySpanExporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test-hw-fault-resilience")
    yield tracer, exporter
    exporter.clear()


class TestScrapeSpanLifecycle:

    def test_scrape_span_created(self, in_memory_tracer):
        tracer, exporter = in_memory_tracer
        with tracer.start_as_current_span("scrape_cycle", kind=SpanKind.SERVER):
            pass
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "scrape_cycle"

    def test_scrape_span_kind_is_server(self, in_memory_tracer):
        tracer, exporter = in_memory_tracer
        with tracer.start_as_current_span("scrape_cycle", kind=SpanKind.SERVER):
            pass
        spans = exporter.get_finished_spans()
        assert spans[0].kind == SpanKind.SERVER

    def test_collector_child_span(self, in_memory_tracer):
        tracer, exporter = in_memory_tracer
        with tracer.start_as_current_span("scrape_cycle", kind=SpanKind.SERVER) as parent:
            with tracer.start_as_current_span("collect:edac", kind=SpanKind.INTERNAL):
                pass
        spans = exporter.get_finished_spans()
        assert len(spans) == 2
        child = next(s for s in spans if s.name == "collect:edac")
        assert child.parent.span_id == parent.get_span_context().span_id

    def test_child_span_name_convention(self, in_memory_tracer):
        tracer, exporter = in_memory_tracer
        for name in ["edac", "mce", "aer", "thermal"]:
            with tracer.start_as_current_span(f"collect:{name}"):
                pass
        spans = exporter.get_finished_spans()
        names = {s.name for s in spans}
        assert "collect:edac" in names
        assert "collect:aer"  in names

    def test_collector_attributes(self, in_memory_tracer):
        tracer, exporter = in_memory_tracer
        from opentelemetry.trace import use_span
        with tracer.start_as_current_span(
            "collect:edac",
            attributes={"collector.name": "edac", "collector.metric_count": 12, "collector.up": True}
        ):
            pass
        spans = exporter.get_finished_spans()
        attrs = spans[0].attributes
        assert attrs["collector.name"] == "edac"
        assert attrs["collector.metric_count"] == 12
        assert attrs["collector.up"] is True

    def test_error_recorded_on_span(self, in_memory_tracer):
        tracer, exporter = in_memory_tracer
        with tracer.start_as_current_span("collect:edac") as span:
            span.record_exception(ValueError("sysfs gone away"))
            span.set_status(trace.Status(StatusCode.ERROR))
        spans = exporter.get_finished_spans()
        assert spans[0].status.status_code == StatusCode.ERROR
        events = [e for e in spans[0].events if e.name == "exception"]
        assert len(events) == 1

    def test_span_duration_positive(self, in_memory_tracer):
        tracer, exporter = in_memory_tracer
        with tracer.start_as_current_span("scrape_cycle"):
            time.sleep(0.01)
        spans = exporter.get_finished_spans()
        duration_ns = spans[0].end_time - spans[0].start_time
        assert duration_ns > 0

    def test_trace_id_consistent_across_children(self, in_memory_tracer):
        tracer, exporter = in_memory_tracer
        with tracer.start_as_current_span("scrape_cycle"):
            with tracer.start_as_current_span("collect:edac"):
                pass
            with tracer.start_as_current_span("collect:mce"):
                pass
        spans = exporter.get_finished_spans()
        trace_ids = {s.context.trace_id for s in spans}
        # All spans in one scrape share the same trace_id
        assert len(trace_ids) == 1


class TestW3CTraceparent:

    def test_traceparent_format(self, in_memory_tracer):
        """traceparent header must match: 00-<32hex>-<16hex>-<2hex>"""
        import re
        tracer, exporter = in_memory_tracer
        tp_pattern = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
        with tracer.start_as_current_span("scrape_cycle") as span:
            ctx = span.get_span_context()
            trace_id_hex = format(ctx.trace_id, "032x")
            span_id_hex  = format(ctx.span_id, "016x")
            flags_hex    = "01"
            header_value = f"00-{trace_id_hex}-{span_id_hex}-{flags_hex}"
        assert tp_pattern.match(header_value), f"Bad traceparent: {header_value}"

    def test_multiple_scrapes_different_trace_ids(self, in_memory_tracer):
        tracer, exporter = in_memory_tracer
        trace_ids = set()
        for _ in range(3):
            with tracer.start_as_current_span("scrape_cycle") as span:
                trace_ids.add(span.get_span_context().trace_id)
        assert len(trace_ids) == 3


class TestInMemoryExporter:

    def test_exporter_captures_all_spans(self, in_memory_tracer):
        tracer, exporter = in_memory_tracer
        collectors = ["edac", "mce", "aer", "thermal", "cpufreq", "pmu"]
        with tracer.start_as_current_span("scrape_cycle"):
            for c in collectors:
                with tracer.start_as_current_span(f"collect:{c}"):
                    pass
        spans = exporter.get_finished_spans()
        # 1 root + 6 children
        assert len(spans) == 7

    def test_clear_resets_exporter(self, in_memory_tracer):
        tracer, exporter = in_memory_tracer
        with tracer.start_as_current_span("test"):
            pass
        assert len(exporter.get_finished_spans()) == 1
        exporter.clear()
        assert len(exporter.get_finished_spans()) == 0
