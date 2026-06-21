#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
aging/dimm-aging-report.py — DIMM aging analysis via linear regression on CE rate.

Queries Prometheus for per-mc/csrow CE counter history and fits a linear
regression to project when the correctable error rate will exceed thresholds.

Output:
    JSON report + Prometheus textfile metrics:
        dimm_aging_ce_rate_slope{mc, csrow}       — CE/day regression slope
        dimm_aging_projected_days_to_threshold{mc} — days until CE rate > warn_threshold
        dimm_aging_health_score{mc, csrow}         — 0.0–1.0 health (1=new, 0=critical)
        dimm_aging_report_timestamp

Usage:
    python3 aging/dimm-aging-report.py [--prometheus URL] [--output PATH]
        [--window 30d] [--warn-threshold 10] [--critical-threshold 100]
"""
import argparse
import json
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

DEFAULT_PROMETHEUS    = "http://localhost:9090"
DEFAULT_OUTPUT        = Path("/var/lib/node_exporter/textfile_collector/dimm_aging.prom")
DEFAULT_WARN_DAYS_OUT = 90      # warn if projected to hit threshold in <90 days
DEFAULT_WARN_THRESHOLD = 10     # CE/day
DEFAULT_CRITICAL_THRESHOLD = 100
DEFAULT_WINDOW = "30d"
DEFAULT_QUERY  = "increase(edac_correctable_errors_total[1d])"


# ---- Linear regression (no numpy dependency) ---------------------------------

def linear_regression(x_vals: list, y_vals: list) -> tuple:
    """OLS linear regression. Returns (slope, intercept, r_squared)."""
    n = len(x_vals)
    if n < 2:
        return 0.0, (y_vals[0] if y_vals else 0.0), 0.0

    sum_x  = sum(x_vals)
    sum_y  = sum(y_vals)
    sum_xx = sum(x * x for x in x_vals)
    sum_xy = sum(x * y for x, y in zip(x_vals, y_vals))

    denom = n * sum_xx - sum_x ** 2
    if denom == 0:
        return 0.0, sum_y / n, 0.0

    slope     = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n

    # R²
    y_mean   = sum_y / n
    ss_tot   = sum((y - y_mean) ** 2 for y in y_vals)
    y_pred   = [slope * x + intercept for x in x_vals]
    ss_res   = sum((y - yp) ** 2 for y, yp in zip(y_vals, y_pred))
    r_sq     = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return slope, intercept, r_sq


def days_to_threshold(current_rate: float, slope: float, threshold: float) -> float:
    """Return days until rate reaches threshold given linear slope (rate/day)."""
    if slope <= 0:
        return float("inf")
    remaining = threshold - current_rate
    if remaining <= 0:
        return 0.0
    return remaining / slope


def health_score(current_rate: float, warn: float, critical: float) -> float:
    """Linear health score: 1.0 at rate=0, 0.0 at rate≥critical."""
    if current_rate >= critical:
        return 0.0
    if current_rate <= 0:
        return 1.0
    return max(0.0, 1.0 - current_rate / critical)


# ---- Prometheus query --------------------------------------------------------

def _window_to_seconds(window: str) -> float:
    """Convert a Prometheus-style duration string (e.g. '30d') to seconds."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    return float(window[:-1]) * units[window[-1]]


def query_range_series(prometheus_url: str, query: str, window: str, step: int = 86400) -> dict:
    """Return {label_key: [(unix_ts, value), ...]}."""
    end   = time.time()
    start = end - _window_to_seconds(window)
    params = urllib.parse.urlencode({
        "query": query, "start": start, "end": end, "step": step
    })
    url = f"{prometheus_url}/api/v1/query_range?{params}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read())
    if data.get("status") != "success":
        raise RuntimeError(f"Prometheus error: {data.get('error')}")

    result = {}
    for series in data["data"]["result"]:
        key = _label_key(series["metric"])
        vals = [(float(v[0]), float(v[1])) for v in series["values"] if v[1] != "NaN"]
        result[key] = vals
    return result


def _label_key(metric: dict) -> str:
    """Build a deterministic Prometheus label string from a Prometheus metric dict."""
    return ",".join(f'{k}="{v}"' for k, v in sorted(metric.items()))


# ---- Emit metrics ------------------------------------------------------------

def emit_prometheus(analyses: list, timestamp: float) -> str:
    """Render Prometheus textfile exposition for DIMM aging analysis results.

    Outputs one sample per (label_key) for each of the four aging metrics plus
    a timestamp gauge. infinity is encoded as 1e308 (the closest float64 to ∞
    that Prometheus accepts) so dashboards do not display NaN for healthy DIMMs.
    """
    lines = []
    lines += [
        "# HELP dimm_aging_ce_rate_slope Linear regression slope of CE rate (errors/day).",
        "# TYPE dimm_aging_ce_rate_slope gauge",
    ]
    for a in analyses:
        lines.append(f'dimm_aging_ce_rate_slope{{{a["labels"]}}} {a["slope"]:.6f}')

    lines += [
        "# HELP dimm_aging_projected_days_to_threshold Projected days until warn threshold.",
        "# TYPE dimm_aging_projected_days_to_threshold gauge",
    ]
    for a in analyses:
        val = a["days_to_threshold"]
        out = f'{val:.1f}' if val != float("inf") else "1e308"
        lines.append(f'dimm_aging_projected_days_to_threshold{{{a["labels"]}}} {out}')

    lines += [
        "# HELP dimm_aging_health_score DIMM health score (1=healthy, 0=critical).",
        "# TYPE dimm_aging_health_score gauge",
    ]
    for a in analyses:
        lines.append(f'dimm_aging_health_score{{{a["labels"]}}} {a["health"]:.4f}')

    lines += [
        "# HELP dimm_aging_r_squared R² of the CE rate linear regression.",
        "# TYPE dimm_aging_r_squared gauge",
    ]
    for a in analyses:
        lines.append(f'dimm_aging_r_squared{{{a["labels"]}}} {a["r_squared"]:.4f}')

    lines += [
        "# HELP dimm_aging_report_timestamp Unix timestamp of last aging report.",
        "# TYPE dimm_aging_report_timestamp gauge",
        f"dimm_aging_report_timestamp {timestamp:.3f}",
    ]
    return "\n".join(lines) + "\n"


def analyze_series(label_key: str, points: list, warn_threshold: float,
                   critical_threshold: float) -> dict:
    """Fit a linear regression to one DIMM's CE rate history and compute aging metrics.

    Args:
        label_key: Prometheus label string identifying this DIMM channel.
        points: list of (unix_timestamp, ce_rate) tuples in chronological order.
        warn_threshold: CE/day rate that triggers a warning (default 10).
        critical_threshold: CE/day rate that makes health_score=0 (default 100).

    Returns:
        Dict with keys: labels, slope, intercept, r_squared, current_rate,
        days_to_threshold, health.
    """
    if not points:
        return {"labels": label_key, "slope": 0.0, "intercept": 0.0,
                "r_squared": 0.0, "current_rate": 0.0,
                "days_to_threshold": float("inf"), "health": 1.0}

    t0      = points[0][0]
    x_days  = [(p[0] - t0) / 86400 for p in points]
    y_rates = [p[1] for p in points]

    slope, intercept, r_sq = linear_regression(x_days, y_rates)
    current_rate = y_rates[-1]
    dthr = days_to_threshold(current_rate, slope, warn_threshold)
    h    = health_score(current_rate, warn_threshold, critical_threshold)

    return {
        "labels":            label_key,
        "slope":             slope,
        "intercept":         intercept,
        "r_squared":         r_sq,
        "current_rate":      current_rate,
        "days_to_threshold": dthr,
        "health":            h,
    }


def main():
    """Entry point: query Prometheus CE history, run aging analysis, write output.

    In --json mode, prints the raw analysis dict to stdout for downstream
    pipeline use (e.g. feeding into survival-analysis.py). Otherwise writes
    Prometheus textfile format to --output for node_exporter pickup.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--prometheus",        default=DEFAULT_PROMETHEUS)
    parser.add_argument("--output",    type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--query",             default=DEFAULT_QUERY)
    parser.add_argument("--window",            default=DEFAULT_WINDOW)
    parser.add_argument("--warn-threshold",    type=float, default=DEFAULT_WARN_THRESHOLD)
    parser.add_argument("--critical-threshold", type=float, default=DEFAULT_CRITICAL_THRESHOLD)
    parser.add_argument("--json",     action="store_true")
    args = parser.parse_args()

    try:
        series_map = query_range_series(args.prometheus, args.query, args.window)
    except Exception as exc:
        sys.stderr.write(f"dimm-aging-report: {exc}\n")
        sys.exit(1)

    analyses = [
        analyze_series(k, v, args.warn_threshold, args.critical_threshold)
        for k, v in series_map.items()
    ]
    ts = time.time()

    if args.json:
        print(json.dumps({"timestamp": ts, "analyses": analyses}, indent=2))
        return

    output = emit_prometheus(analyses, ts)
    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        sys.stderr.write(f"dimm-aging-report: wrote {len(output)} bytes to {args.output}\n")


if __name__ == "__main__":
    main()
