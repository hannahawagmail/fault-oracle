"""Validate Grafana dashboard JSON against exporter metric definitions."""

import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_PATH = ROOT / "dashboards" / "fleet-fault-resilience.json"
COLLECTORS_DIR = ROOT / "exporter" / "collectors"
METRIC_REF_PATH = ROOT / "docs" / "metric-reference.md"

# Prometheus built-in/internal names and PromQL functions to ignore
IGNORE_METRICS = {"job", "instance", "up", "time", "vector", "absent", "group"}


def load_dashboard():
    with open(DASHBOARD_PATH) as f:
        return json.load(f)


def extract_exprs_from_panels(obj):
    """Recursively extract all 'expr' values from dashboard JSON."""
    exprs = []
    if isinstance(obj, dict):
        if "expr" in obj and isinstance(obj["expr"], str):
            exprs.append(obj["expr"])
        for v in obj.values():
            exprs.extend(extract_exprs_from_panels(v))
    elif isinstance(obj, list):
        for item in obj:
            exprs.extend(extract_exprs_from_panels(item))
    return exprs


def extract_metrics_from_exprs(exprs):
    """Extract metric names from PromQL expressions."""
    metrics = set()
    for expr in exprs:
        # Match sequences like word_word (prometheus metric naming convention)
        candidates = re.findall(r"[a-z][a-z0-9_]*_[a-z][a-z0-9_]*", expr)
        for c in candidates:
            # Skip template variables, label names, and PromQL keywords
            if c.startswith("__") or c in IGNORE_METRICS:
                continue
            # Skip things that look like PromQL range selectors or label matchers
            if c in ("rate_interval",):
                continue
            metrics.add(c)
    return metrics


def extract_metrics_from_go_sources():
    """Extract metric names defined in Go exporter collector files."""
    metrics = set()
    for go_file in COLLECTORS_DIR.glob("*.go"):
        content = go_file.read_text()
        # Match quoted strings that look like metric names in NewDesc / BuildFQName calls
        for match in re.findall(r'"([a-z][a-z0-9_]*_[a-z][a-z0-9_]*)"', content):
            metrics.add(match)
        # Handle BuildFQName(ns, "", "suffix") — reconstruct full name
        for ns_match in re.finditer(
            r'prometheus\.BuildFQName\(\s*(\w+)\s*,\s*"[^"]*"\s*,\s*"([^"]+)"\s*\)',
            content,
        ):
            ns_var, suffix = ns_match.groups()
            # Find the namespace variable value
            ns_def = re.search(rf'{ns_var}\s*=\s*"([^"]+)"', content)
            if ns_def:
                metrics.add(f"{ns_def.group(1)}_{suffix}")
    return metrics


def extract_metrics_from_metric_reference():
    """Extract metric names from docs/metric-reference.md."""
    metrics = set()
    content = METRIC_REF_PATH.read_text()
    # Metrics appear as `metric_name` in markdown tables
    for match in re.findall(r"`([a-z][a-z0-9_]*_[a-z][a-z0-9_]*)`", content):
        metrics.add(match)
    return metrics


@pytest.fixture(scope="module")
def dashboard():
    return load_dashboard()


@pytest.fixture(scope="module")
def known_metrics():
    go_metrics = extract_metrics_from_go_sources()
    doc_metrics = extract_metrics_from_metric_reference()
    return go_metrics | doc_metrics


class TestDashboardStructure:
    def test_has_title(self, dashboard):
        assert "title" in dashboard and dashboard["title"]

    def test_has_panels(self, dashboard):
        assert "panels" in dashboard and len(dashboard["panels"]) > 0

    def test_has_uid(self, dashboard):
        assert "uid" in dashboard and dashboard["uid"]


class TestDashboardMetrics:
    def test_all_metrics_defined(self, dashboard, known_metrics):
        exprs = extract_exprs_from_panels(dashboard)
        dashboard_metrics = extract_metrics_from_exprs(exprs)

        assert dashboard_metrics, "No metrics found in dashboard expressions"

        undefined = dashboard_metrics - known_metrics
        assert not undefined, (
            f"Dashboard references metrics not found in Go source or metric-reference.md:\n"
            f"  {sorted(undefined)}"
        )
