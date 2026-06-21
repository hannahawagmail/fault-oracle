#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
storage/wear_predictor.py — Python-importable shim for wear-predictor.py.

Identical implementation; exists so tests can do `import wear_predictor`
(Python identifiers cannot contain hyphens).

See wear-predictor.py for full documentation.
"""

import argparse
import sys
import time
import urllib.request
import urllib.parse
import json
from pathlib import Path

DEFAULT_OUTPUT         = Path("/var/lib/node_exporter/textfile_collector/wear.prom")
DEFAULT_PROMETHEUS_URL = "http://localhost:9090"

# Minimum hours of history required before we attempt prediction
MIN_HISTORY_HOURS = 72

# Cap prediction at 10 years
MAX_DAYS = 3650

# Prometheus query range: 30 days, 1h step
RANGE_SECONDS = 30 * 24 * 3600
STEP_SECONDS  = 3600  # 1 hour

NVME_QUERY = "nvme_percentage_used"
SATA_QUERY = 'sata_smart_value{attribute="percentage_used"}'


# ---------------------------------------------------------------------------
# OLS linear regression (pure Python, no scipy/numpy dependency)
# ---------------------------------------------------------------------------

def ols_slope_intercept(xs: list, ys: list):
    """
    Compute OLS slope and intercept for y = slope * x + intercept.

    xs, ys: parallel lists of floats.
    Returns (slope, intercept) or (None, None) if computation is impossible.
    """
    n = len(xs)
    if n < 2:
        return None, None

    sum_x  = sum(xs)
    sum_y  = sum(ys)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    sum_xx = sum(x * x for x in xs)

    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return None, None

    slope     = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


def predict_device(timestamps: list, values: list):
    """
    Fit OLS on (timestamps, values) and return a prediction dict.

    Parameters
    ----------
    timestamps : list of float — Unix epoch seconds
    values     : list of float — wear percentage at each timestamp

    Returns
    -------
    dict with keys:
        "insufficient_data" : bool  — True if < MIN_HISTORY_HOURS of data
        "slope_per_hour"    : float|None
        "hours_remaining"   : float|None  — None if slope <= 0
        "days_remaining"    : float|None  — capped at MAX_DAYS; None if slope <= 0
        "rate_pct_per_day"  : float|None
        "prob_90d"          : float|None  — 1.0 or 0.0
        "current_value"     : float|None
    """
    result = {
        "insufficient_data": False,
        "slope_per_hour":    None,
        "hours_remaining":   None,
        "days_remaining":    None,
        "rate_pct_per_day":  None,
        "prob_90d":          None,
        "current_value":     None,
    }

    if not timestamps or not values or len(timestamps) != len(values):
        result["insufficient_data"] = True
        return result

    t0 = timestamps[0]
    hours = [(t - t0) / 3600.0 for t in timestamps]
    span_hours = hours[-1] - hours[0]

    if span_hours < MIN_HISTORY_HOURS:
        result["insufficient_data"] = True
        return result

    slope, intercept = ols_slope_intercept(hours, values)
    if slope is None:
        result["insufficient_data"] = True
        return result

    result["current_value"] = values[-1]
    result["slope_per_hour"] = slope

    if slope <= 0:
        # Not wearing — no prediction
        return result

    current_value = values[-1]
    rate_pct_per_day = slope * 24.0
    result["rate_pct_per_day"] = rate_pct_per_day

    hours_remaining = (100.0 - current_value) / slope
    if hours_remaining < 0:
        hours_remaining = 0.0

    days_remaining = min(hours_remaining / 24.0, float(MAX_DAYS))
    result["hours_remaining"] = hours_remaining
    result["days_remaining"]  = days_remaining
    result["prob_90d"]        = 1.0 if hours_remaining < 90 * 24 else 0.0

    return result


# ---------------------------------------------------------------------------
# Prometheus query helpers
# ---------------------------------------------------------------------------

def query_prometheus_range(base_url: str, query: str, start: float, end: float, step: int):  # pragma: no cover
    """
    Query Prometheus /api/v1/query_range.

    Returns list of {"metric": {...}, "values": [[ts, val], ...]} dicts.
    Returns empty list on any failure.
    """
    params = urllib.parse.urlencode({
        "query": query,
        "start": f"{start:.0f}",
        "end":   f"{end:.0f}",
        "step":  str(step),
    })
    url = f"{base_url.rstrip('/')}/api/v1/query_range?{params}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if data.get("status") != "success":
            return []
        return data.get("data", {}).get("result", [])
    except Exception:
        return []


def extract_device_series(result_list: list, label_key: str = "device") -> dict:
    """
    Convert Prometheus range_query results into {device: (timestamps, values)}.
    """
    series = {}
    for r in result_list:
        device = r.get("metric", {}).get(label_key, "unknown")
        raw_values = r.get("values", [])
        timestamps = []
        values     = []
        for ts_str, val_str in raw_values:
            try:
                timestamps.append(float(ts_str))
                values.append(float(val_str))
            except (TypeError, ValueError):
                continue
        if timestamps:
            series[device] = (timestamps, values)
    return series


# ---------------------------------------------------------------------------
# Metric emission
# ---------------------------------------------------------------------------

def emit_metrics(predictions: dict, predictor_up: int) -> str:
    """
    Render wear prediction results as Prometheus textfile format.

    predictions: {device: {"type": str, "result": predict_device(...)}}
    """
    lines = []
    now = time.time()

    # --- time to failure ---
    lines.append(
        "# HELP storage_wear_time_to_failure_days "
        "Projected days until device reaches 100% wear (OLS extrapolation)."
    )
    lines.append("# TYPE storage_wear_time_to_failure_days gauge")
    for device, info in sorted(predictions.items()):
        r = info["result"]
        dtype = info["type"]
        if r["days_remaining"] is not None:
            lines.append(
                f'storage_wear_time_to_failure_days{{device="{device}",type="{dtype}"}} '
                f'{r["days_remaining"]:.2f}'
            )

    # --- wear rate ---
    lines.append(
        "# HELP storage_wear_rate_percent_per_day "
        "Wear percentage consumed per day (OLS slope * 24)."
    )
    lines.append("# TYPE storage_wear_rate_percent_per_day gauge")
    for device, info in sorted(predictions.items()):
        r = info["result"]
        dtype = info["type"]
        if r["rate_pct_per_day"] is not None:
            lines.append(
                f'storage_wear_rate_percent_per_day{{device="{device}",type="{dtype}"}} '
                f'{r["rate_pct_per_day"]:.6f}'
            )

    # --- 90-day failure probability ---
    lines.append(
        "# HELP storage_wear_probability_90d "
        "1.0 if device projected to reach 100% wear within 90 days, else 0.0."
    )
    lines.append("# TYPE storage_wear_probability_90d gauge")
    for device, info in sorted(predictions.items()):
        r = info["result"]
        dtype = info["type"]
        if r["prob_90d"] is not None:
            lines.append(
                f'storage_wear_probability_90d{{device="{device}",type="{dtype}"}} '
                f'{r["prob_90d"]:.1f}'
            )

    # --- insufficient data ---
    lines.append(
        "# HELP storage_wear_predictor_insufficient_data "
        "1 if device has less than 72 hours of wear history."
    )
    lines.append("# TYPE storage_wear_predictor_insufficient_data gauge")
    for device, info in sorted(predictions.items()):
        r = info["result"]
        if r["insufficient_data"]:
            lines.append(
                f'storage_wear_predictor_insufficient_data{{device="{device}"}} 1'
            )

    # --- predictor up ---
    lines.append(
        "# HELP storage_wear_predictor_up 1 if wear predictor ran successfully."
    )
    lines.append("# TYPE storage_wear_predictor_up gauge")
    lines.append(f"storage_wear_predictor_up {predictor_up}")

    # --- last run timestamp ---
    lines.append(
        "# HELP storage_wear_predictor_last_run_timestamp "
        "Unix timestamp of last wear predictor run."
    )
    lines.append("# TYPE storage_wear_predictor_last_run_timestamp gauge")
    lines.append(f"storage_wear_predictor_last_run_timestamp {now:.3f}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():  # pragma: no cover
    parser = argparse.ArgumentParser(
        description="Predict storage wear-out using OLS regression on Prometheus data."
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help="Output .prom file path (- for stdout)"
    )
    parser.add_argument(
        "--prometheus-url", default=DEFAULT_PROMETHEUS_URL,
        help="Prometheus base URL"
    )
    args = parser.parse_args()

    now   = time.time()
    start = now - RANGE_SECONDS

    predictions = {}
    predictor_up = 1

    # Query NVMe
    try:
        nvme_results = query_prometheus_range(
            args.prometheus_url, NVME_QUERY, start, now, STEP_SECONDS
        )
        nvme_series = extract_device_series(nvme_results)
        for device, (ts, vals) in nvme_series.items():
            predictions[device] = {
                "type":   "nvme",
                "result": predict_device(ts, vals),
            }
    except Exception as exc:
        sys.stderr.write(f"wear-predictor: NVMe query error: {exc}\n")
        predictor_up = 0

    # Query SATA
    try:
        sata_results = query_prometheus_range(
            args.prometheus_url, SATA_QUERY, start, now, STEP_SECONDS
        )
        sata_series = extract_device_series(sata_results)
        for device, (ts, vals) in sata_series.items():
            key = f"sata_{device}" if device in predictions else device
            predictions[key] = {
                "type":   "sata",
                "result": predict_device(ts, vals),
            }
    except Exception as exc:
        sys.stderr.write(f"wear-predictor: SATA query error: {exc}\n")
        predictor_up = 0

    if not predictions:
        sys.stderr.write("wear-predictor: no device wear data found in Prometheus\n")

    output = emit_metrics(predictions, predictor_up=predictor_up)

    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        sys.stderr.write(f"wear-predictor: wrote {len(output)} bytes to {args.output}\n")


if __name__ == "__main__":  # pragma: no cover
    main()
