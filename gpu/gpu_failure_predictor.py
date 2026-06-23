#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
gpu/gpu-failure-predictor.py — Prophet-based GPU ECC failure predictor.

Queries Prometheus for gpu_ecc_dbe_total over 7 days, fits a Prophet model
per GPU index, and emits Prometheus textfile metrics to stdout or --output.

Metrics emitted:
    gpu_failure_probability_7d{gpu_index}    — probability of failure within 7 days (0.0–1.0)
    gpu_forecast_next_24h_dbe_rate{gpu_index}— predicted mean DBE rate over next 24 h
    gpu_predictor_up                         — gauge (1 if Prometheus reachable, 0 otherwise)

Methodology:
    - Resample raw DBE cumulative counter to hourly DBE rate (diff → /hour)
    - Fit Prophet with daily_seasonality=False, weekly_seasonality=True,
      changepoint_prior_scale=0.05  (same conservative tuning as ce-forecaster.py)
    - Failure probability uses scipy.stats.norm.cdf on
          (threshold - yhat) / ((yhat_upper - yhat) / 1.96)
      where threshold = 1 DBE/hour.  CDF gives P(actual ≥ threshold).
    - Gracefully returns 0.0 when <24 h of history is available.

Usage:
    python3 gpu/gpu-failure-predictor.py [--prometheus URL] [--output PATH]
"""
import argparse
import json
import logging
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

import pandas as pd
from prophet import Prophet
from scipy.stats import norm

logging.getLogger("prophet").setLevel(logging.WARNING)
logging.getLogger("cmdstanpy").setLevel(logging.WARNING)

DEFAULT_PROMETHEUS  = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
DEFAULT_OUTPUT      = Path("/var/lib/node_exporter/textfile_collector/gpu_predictor.prom")
DBE_QUERY           = "gpu_ecc_dbe_total"
WINDOW_SECONDS      = 7 * 24 * 3600   # 7 days
STEP_SECONDS        = 3600            # 1 hour resolution
MIN_HOURS_HISTORY   = 24             # require at least 24 h to fit
THRESHOLD_DBE_RATE  = 1.0            # 1 DBE/hour triggers alarm
FORECAST_HOURS      = 7 * 24         # forecast horizon: 7 days

log = logging.getLogger("gpu-failure-predictor")


# ---------------------------------------------------------------------------
# Prometheus helpers
# ---------------------------------------------------------------------------

def fetch_dbe_series(prometheus_url: str) -> dict:  # pragma: no cover
    """
    Query Prometheus for gpu_ecc_dbe_total over the past 7 days.
    Returns {gpu_index: pd.DataFrame(ds, y)} where y is hourly DBE rate.
    Raises on connection error or non-success Prometheus status.
    """
    end   = time.time()
    start = end - WINDOW_SECONDS
    params = urllib.parse.urlencode({
        "query": DBE_QUERY,
        "start": start,
        "end":   end,
        "step":  STEP_SECONDS,
    })
    url = f"{prometheus_url}/api/v1/query_range?{params}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())

    if data["status"] != "success":
        raise RuntimeError(f"Prometheus: {data.get('error')}")

    series = {}
    for s in data["data"]["result"]:
        gpu_index = s["metric"].get("gpu_index", "0")

        rows = []
        for v in s["values"]:
            ts = float(v[0])
            raw = v[1]
            if raw in ("NaN", "Inf", "+Inf", "-Inf"):
                continue
            rows.append((pd.Timestamp(ts, unit="s", tz="UTC"), float(raw)))

        if not rows:
            continue

        df = pd.DataFrame(rows, columns=["ds", "raw"])
        df["ds"] = df["ds"].dt.tz_localize(None)
        df = df.set_index("ds").sort_index()

        # Convert cumulative counter → hourly rate (first diff, then per-hour)
        df["y"] = df["raw"].diff().clip(lower=0)
        df = df.dropna(subset=["y"])

        if len(df) < MIN_HOURS_HISTORY:
            log.info("GPU %s: only %d hours of data, skipping fit", gpu_index, len(df))
            series[gpu_index] = None   # sentinel: insufficient history
            continue

        df = df.reset_index()[["ds", "y"]]
        series[gpu_index] = df

    return series


# ---------------------------------------------------------------------------
# Model fit and probability
# ---------------------------------------------------------------------------

def fit_prophet(df: pd.DataFrame) -> Prophet:
    """Fit a Prophet model to an hourly DBE rate series."""
    m = Prophet(
        yearly_seasonality=False,
        weekly_seasonality=True,
        daily_seasonality=False,
        interval_width=0.95,
        changepoint_prior_scale=0.05,
        seasonality_prior_scale=1.0,
    )
    m.fit(df)
    return m


def make_forecast(model: Prophet, horizon_hours: int) -> pd.DataFrame:
    """Return a forecast DataFrame covering horizon_hours into the future."""
    future = model.make_future_dataframe(periods=horizon_hours, freq="h")
    return model.predict(future)


def compute_failure_probability(fc: pd.DataFrame) -> float:
    """
    Compute P(DBE rate ≥ THRESHOLD_DBE_RATE) using Normal CDF over the
    7-day forecast window.

    For each future hour, the predicted distribution is approximately:
        N(yhat, sigma) where sigma = (yhat_upper - yhat) / 1.96

    P(X ≥ threshold) = 1 - CDF((threshold - yhat) / sigma)
               = norm.sf((threshold - yhat) / sigma)

    We return the mean exceedance probability across all forecast hours.
    """
    now = pd.Timestamp.now()
    horizon_end = now + pd.Timedelta(hours=FORECAST_HOURS)
    future_mask = (fc["ds"] >= now) & (fc["ds"] <= horizon_end)
    window = fc[future_mask].copy()

    if window.empty:
        return 0.0

    sigma = (window["yhat_upper"] - window["yhat"]) / 1.96
    # Avoid division by zero on rows with zero uncertainty
    sigma = sigma.clip(lower=1e-9)

    z = (THRESHOLD_DBE_RATE - window["yhat"]) / sigma
    prob_per_hour = norm.sf(z.values)   # sf = 1 - CDF = P(X >= threshold)

    return float(prob_per_hour.mean())


def mean_next_24h_rate(fc: pd.DataFrame) -> float:
    """Return mean predicted DBE rate over the next 24 hours."""
    now = pd.Timestamp.now()
    window = fc[(fc["ds"] >= now) & (fc["ds"] <= now + pd.Timedelta(hours=24))]
    if window.empty:
        return 0.0
    return float(window["yhat"].clip(lower=0).mean())


# ---------------------------------------------------------------------------
# Metric emission
# ---------------------------------------------------------------------------

def emit_metrics(
    predictor_up: bool,
    probabilities: dict,   # {gpu_index: float}
    next24h_rates: dict,   # {gpu_index: float}
    now: float,
) -> str:
    lines = []

    lines += [
        "# HELP gpu_predictor_up 1 if Prometheus reachable and predictor operational.",
        "# TYPE gpu_predictor_up gauge",
        f"gpu_predictor_up {1 if predictor_up else 0}",
    ]

    if probabilities:
        lines += [
            "# HELP gpu_failure_probability_7d Estimated probability of GPU failure within 7 days.",
            "# TYPE gpu_failure_probability_7d gauge",
        ]
        for gpu_index, prob in sorted(probabilities.items()):
            lines.append(
                f'gpu_failure_probability_7d{{gpu_index="{gpu_index}"}} {prob:.6f}'
            )

    if next24h_rates:
        lines += [
            "# HELP gpu_forecast_next_24h_dbe_rate Predicted mean DBE rate over the next 24 h.",
            "# TYPE gpu_forecast_next_24h_dbe_rate gauge",
        ]
        for gpu_index, rate in sorted(next24h_rates.items()):
            lines.append(
                f'gpu_forecast_next_24h_dbe_rate{{gpu_index="{gpu_index}"}} {rate:.6f}'
            )

    lines += [
        "# HELP gpu_predictor_last_run_timestamp Unix timestamp of the last predictor run.",
        "# TYPE gpu_predictor_last_run_timestamp gauge",
        f"gpu_predictor_last_run_timestamp {now:.3f}",
    ]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():  # pragma: no cover
    parser = argparse.ArgumentParser(
        description="Prophet-based GPU ECC DBE failure predictor."
    )
    parser.add_argument(
        "--prometheus",
        default=DEFAULT_PROMETHEUS,
        help="Prometheus base URL",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output .prom file (- for stdout)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    now = time.time()

    try:
        series_map = fetch_dbe_series(args.prometheus)
    except Exception as exc:
        log.error("Failed to fetch from Prometheus: %s", exc)
        output = emit_metrics(False, {}, {}, now)
        if str(args.output) == "-":
            sys.stdout.write(output)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output)
        sys.exit(0)  # non-fatal: write collector_up=0 and exit cleanly

    if not series_map:
        log.info("No DBE series returned from Prometheus.")
        output = emit_metrics(True, {}, {}, now)
        if str(args.output) == "-":
            sys.stdout.write(output)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output)
        return

    probabilities = {}
    next24h_rates = {}

    for gpu_index, df in series_map.items():
        if df is None:
            # Insufficient history — emit 0.0 probability
            log.info("GPU %s: insufficient history, reporting probability=0.0", gpu_index)
            probabilities[gpu_index] = 0.0
            next24h_rates[gpu_index] = 0.0
            continue

        log.info("GPU %s: fitting Prophet on %d hourly samples", gpu_index, len(df))
        try:
            model = fit_prophet(df)
            fc    = make_forecast(model, FORECAST_HOURS)
            prob  = compute_failure_probability(fc)
            rate  = mean_next_24h_rate(fc)
            log.info("  GPU %s: failure_probability_7d=%.4f  next_24h_rate=%.4f",
                     gpu_index, prob, rate)
            probabilities[gpu_index] = prob
            next24h_rates[gpu_index] = rate
        except Exception as exc:
            log.warning("Model fit failed for GPU %s: %s", gpu_index, exc)

    output = emit_metrics(True, probabilities, next24h_rates, now)

    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        log.info("gpu-failure-predictor: wrote to %s", args.output)


if __name__ == "__main__":  # pragma: no cover
    main()
