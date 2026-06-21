#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
ml/probability-exporter.py — Load persisted Prophet models and emit
failure probability metrics to node_exporter textfile collector.

Runs every 5 minutes via systemd timer. Reads all *.json metadata files
from MODEL_DIR, emits:
    failure_probability_7d{instance, mc, csrow}   — P(fail in 7 days)
    failure_probability_30d{instance, mc, csrow}  — P(fail in 30 days)
    dimm_risk_tier{instance, mc, csrow, tier}      — low/medium/high/critical
    ce_forecast_next_24h{instance, mc, csrow}      — expected CE count next 24h
    ml_model_age_hours{instance, mc, csrow}        — hours since last retrain
    ml_exporter_last_run_timestamp
"""
import argparse
import hashlib
import json
import logging
import os
import pickle
import sys
import time
from pathlib import Path

log = logging.getLogger("probability-exporter")


def log_error(msg: str):
    log.error(msg)
    sys.stderr.write(msg + "\n")

import pandas as pd

DEFAULT_MODEL_DIR = Path("/var/lib/hw-fault-ml/models")
DEFAULT_OUTPUT    = Path("/var/lib/node_exporter/textfile_collector/dimm_ml.prom")

RISK_TIERS = [
    (0.8, "critical"),
    (0.5, "high"),
    (0.2, "medium"),
    (0.0, "low"),
]


def risk_tier(prob: float) -> str:
    for threshold, tier in RISK_TIERS:
        if prob >= threshold:
            return tier
    return "low"


def load_metadata(model_dir: Path) -> list:
    return [json.loads(p.read_text()) for p in model_dir.glob("*.json")]


def forecast_next_24h(model_path: str, label_key: str) -> float:  # pragma: no cover
    """Load model and compute expected total CE count over next 24h."""
    try:
        with open(model_path, "rb") as f:
            model = pickle.load(f)

        # Integrity check: verify SHA256 sidecar if present (FIX 3 — pickle safety)
        sha_path = model_path.replace('.pkl', '.sha256')
        if os.path.exists(sha_path):
            with open(sha_path) as sf:
                expected = sf.read().strip()
            with open(model_path, 'rb') as mf:
                actual = hashlib.sha256(mf.read()).hexdigest()
            if actual != expected:
                log_error(f"Model integrity check FAILED for {model_path} — skipping")
                return 0.0

        future = model.make_future_dataframe(periods=24, freq="h")
        fc = model.predict(future)
        now = pd.Timestamp.now()
        window = fc[(fc["ds"] >= now) & (fc["ds"] <= now + pd.Timedelta(hours=24))]
        # yhat is rate (CE/s); sum × 3600s per hour → total CEs expected
        return max(0.0, float(window["yhat"].clip(lower=0).sum()) * 3600)
    except Exception:
        return 0.0


def emit_metrics(entries: list, now: float) -> str:
    lines = []

    lines += [
        "# HELP failure_probability_7d Probability of DIMM failure within 7 days (Prophet model).",
        "# TYPE failure_probability_7d gauge",
    ]
    for e in entries:
        k = e["label_key"]
        lines.append(f'failure_probability_7d{{{k}}} {e["failure_probability_7d"]:.6f}')

    lines += [
        "# HELP failure_probability_30d Probability of DIMM failure within 30 days.",
        "# TYPE failure_probability_30d gauge",
    ]
    for e in entries:
        k = e["label_key"]
        lines.append(f'failure_probability_30d{{{k}}} {e["failure_probability_30d"]:.6f}')

    lines += [
        "# HELP dimm_risk_tier DIMM risk tier (1 = active tier, 0 = inactive).",
        "# TYPE dimm_risk_tier gauge",
    ]
    for e in entries:
        k = e["label_key"]
        tier = risk_tier(e["failure_probability_7d"])
        for _, t in RISK_TIERS:
            val = 1 if t == tier else 0
            lines.append(f'dimm_risk_tier{{{k},tier="{t}"}} {val}')

    lines += [
        "# HELP ce_forecast_next_24h Expected correctable error count over next 24 hours.",
        "# TYPE ce_forecast_next_24h gauge",
    ]
    for e in entries:
        k = e["label_key"]
        forecast_ce = forecast_next_24h(e["model_path"], k)
        lines.append(f'ce_forecast_next_24h{{{k}}} {forecast_ce:.2f}')

    lines += [
        "# HELP ml_model_age_hours Hours since this DIMM model was last retrained.",
        "# TYPE ml_model_age_hours gauge",
    ]
    for e in entries:
        k = e["label_key"]
        age_h = (now - e["trained_at"]) / 3600
        lines.append(f'ml_model_age_hours{{{k}}} {age_h:.2f}')

    lines += [
        "# HELP ml_exporter_last_run_timestamp Unix timestamp of last probability exporter run.",
        "# TYPE ml_exporter_last_run_timestamp gauge",
        f"ml_exporter_last_run_timestamp {now:.3f}",
        "# HELP ml_models_loaded_total Number of ML models currently loaded.",
        "# TYPE ml_models_loaded_total gauge",
        f"ml_models_loaded_total {len(entries)}",
    ]
    return "\n".join(lines) + "\n"


def main():  # pragma: no cover
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--output",    type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    if not args.model_dir.exists():
        sys.stderr.write(f"probability-exporter: model dir not found: {args.model_dir}\n")
        # Emit stub so node_exporter doesn't error
        stub = "# HELP ml_models_loaded_total Number of ML models loaded.\n"
        stub += "# TYPE ml_models_loaded_total gauge\n"
        stub += "ml_models_loaded_total 0\n"
        if str(args.output) == "-":
            sys.stdout.write(stub)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(stub)
        return

    entries = load_metadata(args.model_dir)
    now     = time.time()
    output  = emit_metrics(entries, now)

    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        sys.stderr.write(f"probability-exporter: wrote {len(entries)} models to {args.output}\n")


if __name__ == "__main__":  # pragma: no cover
    main()
