#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
ml/ce-forecaster.py — Prophet-based CE rate forecaster for DIMM failure prediction.

Queries 90 days of per-(mc, csrow) CE rate from Prometheus, fits a Facebook
Prophet model per series, and persists models to disk. A companion
probability-exporter.py loads the models every 5 minutes and emits:
    failure_probability_7d{mc, csrow, instance}
    failure_probability_30d{mc, csrow, instance}
    ce_forecast_next_24h{mc, csrow, instance}

Prophet is chosen over LSTM because:
  - Interpretable: trend + weekly/daily seasonality components are human-readable
  - No GPU required — fits on any node in <30s for typical fleet sizes
  - Handles missing data and outliers gracefully (EDAC data has both)
  - Uncertainty intervals map directly to failure probability bands

Usage:
    python3 ml/ce-forecaster.py [--prometheus URL] [--model-dir DIR]
        [--window 90d] [--horizon 30d] [--min-points 14]
"""
import argparse
import hashlib
import json
import logging
import pickle
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

import pandas as pd
from prophet import Prophet

logging.getLogger("prophet").setLevel(logging.WARNING)
logging.getLogger("cmdstanpy").setLevel(logging.WARNING)

DEFAULT_PROMETHEUS = "http://localhost:9090"
DEFAULT_MODEL_DIR  = Path("/var/lib/hw-fault-ml/models")
DEFAULT_WINDOW     = "90d"
DEFAULT_HORIZON    = 30   # days to forecast ahead
DEFAULT_MIN_POINTS = 14   # minimum data points to fit a model
QUERY = "rate(edac_correctable_errors_total[1h])"

log = logging.getLogger("ce-forecaster")


def _window_to_seconds(w: str) -> float:
    """Convert a Prometheus-style duration string (e.g. '90d', '1h') to seconds.

    Supports unit suffixes s/m/h/d/w, which matches the same set accepted by
    Prometheus range queries so callers can pass CLI args directly without
    a separate parsing step.
    """
    return float(w[:-1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[w[-1]]


def fetch_series(prometheus_url: str, window: str) -> dict:
    """
    Return {label_key: pd.DataFrame(ds, y)} suitable for Prophet.
    Step = 1 hour (3600s) for 90d → ~2160 points per series.
    """
    end   = time.time()
    start = end - _window_to_seconds(window)
    params = urllib.parse.urlencode({
        "query": QUERY, "start": start, "end": end, "step": 3600
    })
    url = f"{prometheus_url}/api/v1/query_range?{params}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())

    if data["status"] != "success":
        raise RuntimeError(f"Prometheus: {data.get('error')}")

    series = {}
    for s in data["data"]["result"]:
        mc    = s["metric"].get("mc", "0")
        csrow = s["metric"].get("csrow", "0")
        inst  = s["metric"].get("instance", "unknown")
        key   = f'instance="{inst}",mc="{mc}",csrow="{csrow}"'

        rows = [(pd.Timestamp(float(v[0]), unit="s", tz="UTC"), float(v[1]))
                for v in s["values"] if v[1] not in ("NaN", "Inf", "+Inf")]
        if rows:
            df = pd.DataFrame(rows, columns=["ds", "y"])
            df["ds"] = df["ds"].dt.tz_localize(None)   # Prophet needs tz-naive
            series[key] = df

    return series


def fit_model(df: pd.DataFrame) -> Prophet:
    """Fit a Prophet model to a CE rate time series."""
    m = Prophet(
        yearly_seasonality=False,
        weekly_seasonality=True,
        daily_seasonality=True,
        interval_width=0.95,        # 95% confidence interval
        changepoint_prior_scale=0.05,  # conservative — CE rate rarely has abrupt changes
        seasonality_prior_scale=1.0,
    )
    m.fit(df)
    return m


def forecast(model: Prophet, horizon_days: int) -> pd.DataFrame:
    """Return forecast DataFrame with yhat, yhat_lower, yhat_upper."""
    future = model.make_future_dataframe(periods=horizon_days * 24, freq="h")
    return model.predict(future)


def failure_probability(fc: pd.DataFrame, horizon_days: int,
                        threshold_rate: float = 1e-6) -> float:
    """
    Estimate P(CE rate exceeds threshold within horizon) from the
    Prophet forecast upper confidence bound.

    Uses the fraction of forecast hours within [now, now+horizon] where
    yhat_upper > threshold as a proxy for failure probability.
    This is conservative (upper bound) and interpretable.
    """
    now      = pd.Timestamp.now()
    window   = fc[(fc["ds"] >= now) &
                  (fc["ds"] <= now + pd.Timedelta(days=horizon_days))]
    if window.empty:
        return 0.0
    above    = (window["yhat_upper"] > threshold_rate).sum()
    prob     = float(above) / len(window)
    return min(1.0, prob)


def save_model(model: Prophet, fc: pd.DataFrame, label_key: str,
               model_dir: Path, metadata: dict):
    """Persist model + forecast + metadata to disk."""
    safe_key = label_key.replace('"', "").replace(",", "_").replace("=", "-")
    model_path = model_dir / f"{safe_key}.pkl"
    meta_path  = model_dir / f"{safe_key}.json"

    pkl_path = str(model_path)
    with open(pkl_path, "wb") as f:
        pickle.dump(model, f)

    # Write SHA256 sidecar for integrity verification by probability-exporter (FIX 3)
    with open(pkl_path, 'rb') as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    with open(pkl_path.replace('.pkl', '.sha256'), 'w') as f:
        f.write(digest + '\n')

    with open(meta_path, "w") as f:
        json.dump({
            **metadata,
            "label_key":      label_key,
            "trained_at":     time.time(),
            "forecast_rows":  len(fc),
            "model_path":     str(model_path),
        }, f, indent=2)

    return model_path


def main():
    """Entry point: parse CLI args, fetch CE series from Prometheus, fit and save models.

    Iterates over every (instance, mc, csrow) series returned by the Prometheus
    range query and fits an independent Prophet model per series. Models with
    fewer than --min-points samples are skipped to avoid fitting noise.
    In --dry-run mode, models are fitted but not persisted to disk.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--prometheus",  default=DEFAULT_PROMETHEUS)
    parser.add_argument("--model-dir",   type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--window",      default=DEFAULT_WINDOW)
    parser.add_argument("--horizon",     type=int, default=DEFAULT_HORIZON)
    parser.add_argument("--min-points",  type=int, default=DEFAULT_MIN_POINTS)
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--verbose",     action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    args.model_dir.mkdir(parents=True, exist_ok=True)

    log.info("Fetching CE rate series from Prometheus (%s, window=%s)", args.prometheus, args.window)
    try:
        series_map = fetch_series(args.prometheus, args.window)
    except Exception as exc:
        log.error("Fetch failed: %s", exc)
        sys.exit(1)

    log.info("Got %d series", len(series_map))
    trained = 0
    skipped = 0

    for label_key, df in series_map.items():
        if len(df) < args.min_points:
            log.warning("Skipping %s — only %d points (need %d)", label_key, len(df), args.min_points)
            skipped += 1
            continue

        log.info("Fitting %s (%d points)…", label_key, len(df))
        try:
            model = fit_model(df)
            fc    = forecast(model, args.horizon)
            p7    = failure_probability(fc, 7)
            p30   = failure_probability(fc, 30)
            log.info("  P(fail 7d)=%.3f  P(fail 30d)=%.3f", p7, p30)

            if not args.dry_run:
                save_model(model, fc, label_key, args.model_dir, {
                    "failure_probability_7d":  p7,
                    "failure_probability_30d": p30,
                    "data_points":             len(df),
                })
                trained += 1
        except Exception as exc:
            log.warning("Model fit failed for %s: %s", label_key, exc)

    log.info("Done: %d trained, %d skipped", trained, skipped)


if __name__ == "__main__":
    main()
