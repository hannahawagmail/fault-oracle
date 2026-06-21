#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
ml/survival-analysis.py — Kaplan-Meier DIMM lifetime survival curves.

Models DIMM cohort lifetime using the Kaplan-Meier estimator from the
lifelines library. A "failure event" is defined as: first occurrence of
failure_probability_7d > 0.8 for a given mc/csrow. Cohorts are grouped
by DIMM metadata (vendor, rank, age_bucket from SPD when available, or
by node/mc when SPD is absent).

Emits:
    dimm_survival_probability{cohort, days}   — KM survival estimate at day N
    dimm_median_lifetime_days{cohort}          — median lifetime (days)
    dimm_cohort_size{cohort}                   — DIMMs in cohort
    survival_analysis_last_run_timestamp

Usage:
    python3 ml/survival-analysis.py [--events-file PATH] [--output PATH]
"""
import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd
from lifelines import KaplanMeierFitter

DEFAULT_OUTPUT = Path("/var/lib/node_exporter/textfile_collector/dimm_survival.prom")
# Events file: JSONL, one record per DIMM:
#   {"cohort": "samsung_ddr5_rank1", "duration_days": 540, "observed": true}
DEFAULT_EVENTS = Path("/var/lib/hw-fault-ml/dimm-events.jsonl")

EVAL_DAYS = [30, 60, 90, 180, 365, 730]


def load_events(path: Path) -> pd.DataFrame:
    rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    return pd.DataFrame(rows)


def build_synthetic_events() -> pd.DataFrame:
    """
    Synthesize a plausible event table when no real data is available.
    Based on published DRAM field failure studies (Schroeder et al., 2009;
    Google SRE DRAM study, 2015). Used as a prior until fleet data accumulates.
    """
    import random
    random.seed(42)
    rows = []
    cohorts = {
        "ddr4_rank1_lt3yr":  (720, 0.85),   # median 720d, 85% observed
        "ddr4_rank2_lt3yr":  (600, 0.80),
        "ddr5_rank1_new":    (900, 0.70),
        "ddr4_rank1_3to5yr": (480, 0.90),
        "ddr4_rank2_3to5yr": (380, 0.92),
    }
    for cohort, (median_days, obs_rate) in cohorts.items():
        n = random.randint(20, 60)
        for _ in range(n):
            duration = max(1, int(random.expovariate(1 / median_days)))
            observed = random.random() < obs_rate
            rows.append({"cohort": cohort, "duration_days": duration, "observed": observed})
    return pd.DataFrame(rows)


def fit_km(df: pd.DataFrame, cohort: str) -> KaplanMeierFitter:
    sub = df[df["cohort"] == cohort]
    kmf = KaplanMeierFitter(label=cohort)
    kmf.fit(sub["duration_days"], event_observed=sub["observed"])
    return kmf


def median_lifetime(kmf: KaplanMeierFitter) -> float:
    m = kmf.median_survival_time_
    return float(m) if m == m else -1.0   # NaN guard


def emit_metrics(df: pd.DataFrame, now: float, data_source: str = "observed") -> str:
    cohorts = df["cohort"].unique()
    lines = []

    lines += [
        "# HELP dimm_survival_probability KM survival probability at given day for cohort.",
        "# TYPE dimm_survival_probability gauge",
    ]
    for cohort in sorted(cohorts):
        kmf = fit_km(df, cohort)
        for day in EVAL_DAYS:
            prob = float(kmf.survival_function_at_times([day]).iloc[0])
            lines.append(
                f'dimm_survival_probability{{cohort="{cohort}",days="{day}",source="{data_source}"}} {prob:.6f}'
            )

    lines += [
        "# HELP dimm_median_lifetime_days Estimated median DIMM lifetime in days by cohort.",
        "# TYPE dimm_median_lifetime_days gauge",
    ]
    for cohort in sorted(cohorts):
        kmf = fit_km(df, cohort)
        lines.append(f'dimm_median_lifetime_days{{cohort="{cohort}"}} {median_lifetime(kmf):.1f}')

    lines += [
        "# HELP dimm_cohort_size Number of DIMMs in each survival cohort.",
        "# TYPE dimm_cohort_size gauge",
    ]
    for cohort in sorted(cohorts):
        n = int((df["cohort"] == cohort).sum())
        lines.append(f'dimm_cohort_size{{cohort="{cohort}"}} {n}')

    lines += [
        "# HELP survival_analysis_last_run_timestamp Unix timestamp of last survival analysis run.",
        "# TYPE survival_analysis_last_run_timestamp gauge",
        f"survival_analysis_last_run_timestamp {now:.3f}",
    ]
    return "\n".join(lines) + "\n"


def main():  # pragma: no cover
    parser = argparse.ArgumentParser()
    parser.add_argument("--events-file", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--output",      type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--synthetic",   action="store_true",
                        help="Use synthetic prior data (fleet bootstrap mode)")
    args = parser.parse_args()

    if args.synthetic or not args.events_file.exists():
        sys.stderr.write("survival-analysis: using synthetic prior data\n")
        df = build_synthetic_events()
        data_source = "synthetic"
    else:
        df = load_events(args.events_file)
        data_source = "observed"

    now    = time.time()
    output = emit_metrics(df, now, data_source=data_source)

    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        sys.stderr.write(f"survival-analysis: wrote to {args.output}\n")


if __name__ == "__main__":  # pragma: no cover
    main()
