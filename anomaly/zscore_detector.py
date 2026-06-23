#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
anomaly/zscore-detector.py — Z-score anomaly detection over EDAC CE rate.

Queries a 7-day baseline from Prometheus, computes per-node Z-scores,
and emits Prometheus textfile metrics. Run as a systemd timer (every 5m).

Z-score = (current_value - baseline_mean) / baseline_stddev
Anomaly threshold: |Z| > 3.0 (≈0.3% false-positive rate under Gaussian)

Metrics emitted:
    anomaly_zscore{instance, collector}   — current Z-score
    anomaly_detected{instance, collector} — 1 if |Z| > threshold, else 0
    anomaly_detector_last_run_timestamp   — Unix timestamp
    anomaly_baseline_mean{instance, collector}
    anomaly_baseline_stddev{instance, collector}

Usage:
    python3 anomaly/zscore-detector.py [--prometheus URL] [--output PATH]
        [--query PROMQL] [--window 7d] [--threshold 3.0]
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

DEFAULT_PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
DEFAULT_OUTPUT     = Path("/var/lib/node_exporter/textfile_collector/anomaly.prom")
DEFAULT_QUERY      = "rate(edac_correctable_errors_total[5m])"
DEFAULT_WINDOW     = "7d"
DEFAULT_THRESHOLD  = 3.0


def query_range(prometheus_url: str, query: str, window: str) -> dict:  # pragma: no cover
    """
    Query Prometheus range data over [now-window, now] at 5m step.
    Returns: {label_set_str: [float, ...]}
    """
    end   = time.time()
    start = end - _window_to_seconds(window)
    step  = 300  # 5 minutes

    params = urllib.parse.urlencode({
        "query": query,
        "start": start,
        "end":   end,
        "step":  step,
    })
    url = f"{prometheus_url}/api/v1/query_range?{params}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read())

    if data.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {data.get('error', 'unknown')}")

    result = {}
    for series in data["data"]["result"]:
        key = _label_key(series["metric"])
        result[key] = [float(v[1]) for v in series["values"] if v[1] != "NaN"]
    return result


def query_instant(prometheus_url: str, query: str) -> dict:  # pragma: no cover
    """Query instant values. Returns {label_set_str: float}"""
    params = urllib.parse.urlencode({"query": query})
    url = f"{prometheus_url}/api/v1/query?{params}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())

    result = {}
    for series in data["data"]["result"]:
        key = _label_key(series["metric"])
        try:
            result[key] = float(series["value"][1])
        except (IndexError, ValueError):
            result[key] = 0.0
    return result


def _label_key(metric: dict) -> str:
    return ",".join(f'{k}="{v}"' for k, v in sorted(metric.items()))


def _window_to_seconds(window: str) -> float:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    return float(window[:-1]) * units[window[-1]]


def compute_stats(values: list) -> tuple:
    """Return (mean, stddev, clamped) for a list of floats.

    stddev is clamped to 1.0 when the series is constant or empty to avoid
    division by zero. The third return value indicates whether clamping was applied.
    Returns (0, 1, True) for empty/constant series.
    """
    if not values:
        return 0.0, 1.0, True
    n = len(values)
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / n
    if variance > 0:
        return mean, math.sqrt(variance), False
    # Clamp stddev to 1.0 to avoid division by zero on constant series
    return mean, 1.0, True


def compute_zscore(current: float, mean: float, stddev: float) -> float:
    return (current - mean) / stddev


def emit_metrics(zscores: dict, threshold: float) -> str:
    lines = []
    now = time.time()

    lines.append("# HELP anomaly_zscore Z-score of current error rate vs 7-day baseline.")
    lines.append("# TYPE anomaly_zscore gauge")
    for labels, info in sorted(zscores.items()):
        lines.append(f'anomaly_zscore{{{labels}}} {info["zscore"]:.4f}')

    lines.append("# HELP anomaly_detected 1 if Z-score exceeds threshold (anomaly detected).")
    lines.append("# TYPE anomaly_detected gauge")
    for labels, info in sorted(zscores.items()):
        val = 1 if abs(info["zscore"]) > threshold else 0
        lines.append(f'anomaly_detected{{{labels}}} {val}')

    lines.append("# HELP anomaly_baseline_mean Baseline mean of error rate over window.")
    lines.append("# TYPE anomaly_baseline_mean gauge")
    for labels, info in sorted(zscores.items()):
        lines.append(f'anomaly_baseline_mean{{{labels}}} {info["mean"]:.6f}')

    lines.append("# HELP anomaly_baseline_stddev Baseline stddev of error rate over window.")
    lines.append("# TYPE anomaly_baseline_stddev gauge")
    for labels, info in sorted(zscores.items()):
        lines.append(f'anomaly_baseline_stddev{{{labels}}} {info["stddev"]:.6f}')

    # FIX 6: Emit synthetic_stddev gauge to distinguish clamped (constant) series
    # from real series on dashboards. 1 = stddev was clamped to 1.0, 0 = real stddev.
    lines.append("# HELP anomaly_synthetic_stddev 1 if stddev was clamped to 1.0 (constant or empty baseline).")
    lines.append("# TYPE anomaly_synthetic_stddev gauge")
    for labels, info in sorted(zscores.items()):
        clamped = 1 if info.get("stddev_clamped", False) else 0
        lines.append(f'anomaly_synthetic_stddev{{{labels}}} {clamped}')

    lines.append("# HELP anomaly_detector_last_run_timestamp Unix timestamp of last detector run.")
    lines.append("# TYPE anomaly_detector_last_run_timestamp gauge")
    lines.append(f"anomaly_detector_last_run_timestamp {now:.3f}")

    return "\n".join(lines) + "\n"


def run(prometheus_url: str, query: str, window: str, threshold: float) -> dict:  # pragma: no cover
    """Core logic: returns {label_str: {zscore, mean, stddev, stddev_clamped}}."""
    baseline = query_range(prometheus_url, query, window)
    current  = query_instant(prometheus_url, query)

    result = {}
    for label_str, cur_val in current.items():
        history = baseline.get(label_str, [])
        mean, stddev, clamped = compute_stats(history)
        zscore = compute_zscore(cur_val, mean, stddev)
        result[label_str] = {
            "zscore": zscore, "mean": mean, "stddev": stddev,
            "stddev_clamped": clamped, "current": cur_val,
        }
    return result


def main():  # pragma: no cover
    parser = argparse.ArgumentParser()
    parser.add_argument("--prometheus", default=DEFAULT_PROMETHEUS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--window", default=DEFAULT_WINDOW)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = parser.parse_args()

    try:
        zscores = run(args.prometheus, args.query, args.window, args.threshold)
    except Exception as exc:
        sys.stderr.write(f"anomaly-detector: error: {exc}\n")
        sys.exit(1)

    output = emit_metrics(zscores, args.threshold)
    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        sys.stderr.write(f"anomaly-detector: wrote to {args.output}\n")


if __name__ == "__main__":  # pragma: no cover
    main()
