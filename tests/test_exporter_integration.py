# SPDX-License-Identifier: Apache-2.0
"""
test_exporter_integration.py — Integration tests for the hw-fault-exporter binary.

These tests:
  1. Skip automatically if the hw-fault-exporter binary is not built.
  2. Launch the exporter with --sysfs-root pointing at a fresh mock sysfs tree
     and --listen-addr :19100 (non-standard port to avoid conflicts).
  3. Wait up to 3 seconds for the port to become available.
  4. Scrape http://localhost:19100/metrics and parse the Prometheus text format.
  5. Assert that expected metrics are present and /healthz returns HTTP 200.
  6. Clean up the subprocess in teardown.

Prerequisites:
  - Build the binary: cd exporter && go build -o ../bin/hw-fault-exporter .
  - The binary must accept --sysfs-root and --listen-addr flags.

Run:
  pytest tests/test_exporter_integration.py -v
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from pathlib import Path
from typing import Optional

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))

# Binary locations to search (relative to repo root, then $PATH)
BINARY_CANDIDATES = [
    REPO_ROOT / "bin" / "hw-fault-exporter",
    REPO_ROOT / "exporter" / "hw-fault-exporter",
    Path(shutil.which("hw-fault-exporter") or "/nonexistent"),
]

LISTEN_PORT = 19100
LISTEN_ADDR = f":{LISTEN_PORT}"
STARTUP_TIMEOUT_SECS = 5.0
POLL_INTERVAL_SECS = 0.1


def _find_binary() -> Path | None:
    """Return the path to the hw-fault-exporter binary, or None if not runnable."""
    for candidate in BINARY_CANDIDATES:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            # Verify it's actually executable on this platform (not cross-compiled)
            try:
                subprocess.run(
                    [str(candidate), "--help"],
                    capture_output=True,
                    timeout=5,
                )
                return candidate
            except (OSError, subprocess.TimeoutExpired):
                continue
    return None


BINARY = _find_binary()
BINARY_MISSING = BINARY is None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mock_sysfs_for_integration(tmp_path_factory) -> Path:
    """
    Module-scoped mock sysfs tree for integration tests.
    Built once and shared across all tests in this module.
    """
    from conftest import build_mock_sysfs

    root = tmp_path_factory.mktemp("integration-sysfs")
    build_mock_sysfs(root)
    return root


@pytest.fixture(scope="module")
def exporter_process(mock_sysfs_for_integration) -> Generator[subprocess.Popen, None, None]:
    """
    Launch the exporter binary and yield the process.
    The process is terminated in teardown.

    Skips the test if the binary is not available.
    """
    if BINARY_MISSING:
        pytest.skip(
            f"hw-fault-exporter binary not found. "
            f"Build it with: cd {REPO_ROOT}/exporter && go build -o ../bin/hw-fault-exporter ."
        )

    sysfs_root = str(mock_sysfs_for_integration)
    cmd = [
        str(BINARY),
        "--sysfs-root",
        sysfs_root,
        "--listen-addr",
        LISTEN_ADDR,
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Wait for the port to become available
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECS
    port_open = False
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", LISTEN_PORT), timeout=0.5):
                port_open = True
                break
        except (ConnectionRefusedError, OSError):
            time.sleep(POLL_INTERVAL_SECS)

    if not port_open:
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=3)
        pytest.fail(
            f"Exporter did not start within {STARTUP_TIMEOUT_SECS}s.\n"
            f"stdout: {stdout[:500]}\nstderr: {stderr[:500]}"
        )

    yield proc

    # Teardown: terminate and wait
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# Helper: fetch a URL with retries
# ---------------------------------------------------------------------------


def _fetch(url: str, timeout: float = 5.0) -> str:
    """Fetch a URL and return the response body as a string."""
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _fetch_status(url: str, timeout: float = 5.0) -> int:
    """Return the HTTP status code for a URL."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


# ---------------------------------------------------------------------------
# Helper: parse Prometheus text format into a dict of metric_name → [samples]
# ---------------------------------------------------------------------------


def parse_prometheus_text(text: str) -> dict:
    """
    Minimal Prometheus text format parser.

    Returns a dict: metric_name → list of (labels_dict, value) tuples.
    Comments (#) and blank lines are ignored.
    Labels are parsed from the {key="value",...} syntax.

    This is intentionally minimal — just enough to assert presence and values
    of specific metrics without depending on a Prometheus client library.
    """
    import re

    result: dict = {}
    label_re = re.compile(r'(\w+)="([^"]*)"')

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Split on the first space not inside braces to get "name{labels} value ts"
        # Handle metrics with labels: name{...} value
        # Handle metrics without labels: name value
        brace_open = line.find("{")
        brace_close = line.find("}")

        if brace_open != -1 and brace_close != -1:
            metric_name = line[:brace_open]
            labels_str = line[brace_open + 1 : brace_close]
            rest = line[brace_close + 1 :].strip()
        else:
            # No labels
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            metric_name = parts[0]
            labels_str = ""
            rest = parts[1].strip()

        # rest may be "value timestamp" — take only the value part
        value_parts = rest.split()
        if not value_parts:
            continue
        try:
            value = float(value_parts[0])
        except ValueError:
            continue

        # Parse labels
        labels: dict = {}
        for m in label_re.finditer(labels_str):
            labels[m.group(1)] = m.group(2)

        if metric_name not in result:
            result[metric_name] = []
        result[metric_name].append((labels, value))

    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(BINARY_MISSING, reason="hw-fault-exporter binary not built")
class TestExporterMetricsEndpoint:
    """Tests for the /metrics endpoint of the live exporter."""

    def test_metrics_endpoint_returns_200(self, exporter_process):
        """GET /metrics must return HTTP 200."""
        status = _fetch_status(f"http://localhost:{LISTEN_PORT}/metrics")
        assert status == 200, f"/metrics returned HTTP {status}"

    def test_edac_correctable_errors_total_present(self, exporter_process):
        """edac_correctable_errors_total must be present in /metrics output."""
        body = _fetch(f"http://localhost:{LISTEN_PORT}/metrics")
        metrics = parse_prometheus_text(body)
        assert "edac_correctable_errors_total" in metrics, (
            "edac_correctable_errors_total not found in /metrics output\n"
            f"Available metrics: {sorted(metrics.keys())}"
        )

    def test_edac_controller_ce_total_present(self, exporter_process):
        """edac_controller_ce_total must be present in /metrics output."""
        body = _fetch(f"http://localhost:{LISTEN_PORT}/metrics")
        metrics = parse_prometheus_text(body)
        assert "edac_controller_ce_total" in metrics, (
            "edac_controller_ce_total not found in /metrics output\n"
            f"Available metrics: {sorted(metrics.keys())}"
        )

    def test_hw_fault_exporter_build_info_present(self, exporter_process):
        """hw_fault_exporter_build_info must be present and have value 1."""
        body = _fetch(f"http://localhost:{LISTEN_PORT}/metrics")
        metrics = parse_prometheus_text(body)
        assert "hw_fault_exporter_build_info" in metrics, (
            "hw_fault_exporter_build_info not found in /metrics output\n"
            f"Available metrics: {sorted(metrics.keys())}"
        )
        # build_info value must always be 1 (Prometheus info metric convention)
        for labels, value in metrics["hw_fault_exporter_build_info"]:
            assert value == 1.0, (
                f"hw_fault_exporter_build_info must be 1, got {value} (labels={labels})"
            )

    def test_edac_controller_ce_total_has_mc0(self, exporter_process):
        """edac_controller_ce_total must include a sample for controller=mc0."""
        body = _fetch(f"http://localhost:{LISTEN_PORT}/metrics")
        metrics = parse_prometheus_text(body)
        assert "edac_controller_ce_total" in metrics, "edac_controller_ce_total not found"
        controllers = [
            labels.get("controller", "") for labels, _ in metrics["edac_controller_ce_total"]
        ]
        assert "mc0" in controllers, (
            f"controller=mc0 not found in edac_controller_ce_total samples. "
            f"Got controllers: {controllers}"
        )

    def test_edac_controller_ce_total_mc0_value(self, exporter_process):
        """edac_controller_ce_total{controller=mc0} should be 4 (from mock topology)."""
        body = _fetch(f"http://localhost:{LISTEN_PORT}/metrics")
        metrics = parse_prometheus_text(body)

        # From MOCK_TOPOLOGY: mc0 ce_counts = [[3,0],[0,1]] → total = 4
        for labels, value in metrics.get("edac_controller_ce_total", []):
            if labels.get("controller") == "mc0":
                assert value == 4.0, (
                    f"edac_controller_ce_total{{controller=mc0}}: expected 4, got {value}"
                )
                return
        pytest.fail("edac_controller_ce_total{controller=mc0} not found")

    def test_pcie_aer_correctable_total_present(self, exporter_process):
        """pcie_aer_correctable_total must appear in /metrics output."""
        body = _fetch(f"http://localhost:{LISTEN_PORT}/metrics")
        metrics = parse_prometheus_text(body)
        assert "pcie_aer_correctable_total" in metrics, (
            "pcie_aer_correctable_total not found in /metrics output\n"
            f"Available metrics: {sorted(metrics.keys())}"
        )

    def test_mce_available_present(self, exporter_process):
        """mce_available must be present in /metrics output."""
        body = _fetch(f"http://localhost:{LISTEN_PORT}/metrics")
        metrics = parse_prometheus_text(body)
        assert "mce_available" in metrics, (
            "mce_available not found in /metrics output\n"
            f"Available metrics: {sorted(metrics.keys())}"
        )

    def test_scrape_duration_present(self, exporter_process):
        """hw_fault_exporter_scrape_duration_seconds must be present."""
        body = _fetch(f"http://localhost:{LISTEN_PORT}/metrics")
        metrics = parse_prometheus_text(body)
        assert "hw_fault_exporter_scrape_duration_seconds" in metrics, (
            "hw_fault_exporter_scrape_duration_seconds not found in /metrics output"
        )

    def test_scrape_duration_is_non_negative(self, exporter_process):
        """Scrape duration values must be >= 0."""
        body = _fetch(f"http://localhost:{LISTEN_PORT}/metrics")
        metrics = parse_prometheus_text(body)
        for labels, value in metrics.get("hw_fault_exporter_scrape_duration_seconds", []):
            assert value >= 0, f"scrape_duration negative: {value} (labels={labels})"

    def test_metrics_output_is_valid_prometheus_text(self, exporter_process):
        """
        /metrics output must be parseable as Prometheus text format.
        Specifically: no line should cause a parse error, and there must be
        at least 5 distinct metric families.
        """
        body = _fetch(f"http://localhost:{LISTEN_PORT}/metrics")
        metrics = parse_prometheus_text(body)
        assert len(metrics) >= 5, (
            f"Expected at least 5 metric families, got {len(metrics)}: {sorted(metrics.keys())}"
        )


@pytest.mark.skipif(BINARY_MISSING, reason="hw-fault-exporter binary not built")
class TestExporterHealthzEndpoint:
    """Tests for the /healthz endpoint."""

    def test_healthz_returns_200(self, exporter_process):
        """GET /healthz must return HTTP 200."""
        status = _fetch_status(f"http://localhost:{LISTEN_PORT}/healthz")
        assert status == 200, f"/healthz returned HTTP {status}"

    def test_healthz_body_non_empty(self, exporter_process):
        """GET /healthz must return a non-empty body."""
        body = _fetch(f"http://localhost:{LISTEN_PORT}/healthz")
        assert body.strip(), "/healthz returned empty body"

    def test_healthz_responds_quickly(self, exporter_process):
        """GET /healthz must respond within 2 seconds."""
        import time

        start = time.monotonic()
        _fetch(f"http://localhost:{LISTEN_PORT}/healthz")
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"/healthz took {elapsed:.2f}s (expected < 2s)"


# ---------------------------------------------------------------------------
# Standalone smoke test: parse_prometheus_text helper
# ---------------------------------------------------------------------------


class TestParsePrometheusText:
    """Unit tests for the local Prometheus text format parser."""

    def test_parses_simple_metric(self):
        text = "# HELP foo A counter\n# TYPE foo counter\nfoo 42\n"
        result = parse_prometheus_text(text)
        assert "foo" in result
        assert result["foo"][0][1] == 42.0

    def test_parses_metric_with_labels(self):
        text = 'bar{controller="mc0",csrow="0"} 7\n'
        result = parse_prometheus_text(text)
        assert "bar" in result
        labels, value = result["bar"][0]
        assert labels["controller"] == "mc0"
        assert labels["csrow"] == "0"
        assert value == 7.0

    def test_ignores_comment_lines(self):
        text = "# HELP foo A gauge\n# TYPE foo gauge\nfoo 1\n"
        result = parse_prometheus_text(text)
        assert list(result.keys()) == ["foo"]

    def test_ignores_blank_lines(self):
        text = "\nfoo 1\n\nbar 2\n"
        result = parse_prometheus_text(text)
        assert "foo" in result
        assert "bar" in result

    def test_multiple_samples_same_metric(self):
        text = (
            'edac_controller_ce_total{controller="mc0"} 4\n'
            'edac_controller_ce_total{controller="mc1"} 0\n'
        )
        result = parse_prometheus_text(text)
        assert len(result["edac_controller_ce_total"]) == 2

    def test_float_values_parsed(self):
        text = "duration_seconds 0.001234\n"
        result = parse_prometheus_text(text)
        assert abs(result["duration_seconds"][0][1] - 0.001234) < 1e-9

    def test_info_metric_value_one(self):
        text = 'build_info{version="1.0.0"} 1\n'
        result = parse_prometheus_text(text)
        assert result["build_info"][0][1] == 1.0
        assert result["build_info"][0][0]["version"] == "1.0.0"
