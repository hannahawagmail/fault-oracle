# SPDX-License-Identifier: Apache-2.0
"""
tools/benchmark_replay.py — Performance benchmark for parse_edac_trace.py.

Generates synthetic EDAC logs of increasing size, times the parser, and
asserts that parse time stays below the SLO: <1 second per 10,000 events.

Usage:
    python3 tools/benchmark_replay.py [--output PATH] [--verbose]

Output (JSON):
    {
      "slo_p95_s_per_10k": 1.0,
      "results": [
        {"events": 100,   "runs": 5, "mean_s": 0.003, "p95_s": 0.004, ...},
        ...
      ],
      "passed": true
    }
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from statistics import mean, quantiles

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SLO_P95_S_PER_10K = 1.0   # 95th-percentile parse time must be < 1s per 10k events
WARMUP_RUNS = 1
BENCH_RUNS = 5

EVENT_SIZES = [100, 1_000, 10_000, 100_000]

ROOT = Path(__file__).parent.parent
GENERATE_PY = ROOT / "tools" / "generate_edac_log.py"
PARSE_PY = ROOT / "replay" / "parse_edac_trace.py"


# ---------------------------------------------------------------------------
# Log generation
# ---------------------------------------------------------------------------

def generate_log(n_events: int, tmpdir: str) -> str:
    """Generate a synthetic EDAC log with n_events lines. Returns file path."""
    log_path = os.path.join(tmpdir, f"bench_{n_events}.log")
    result = subprocess.run(
        [
            sys.executable, str(GENERATE_PY),
            "--events", str(n_events),
            "--seed", "42",
            "--output", log_path,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"generate_edac_log.py failed for n={n_events}:\n{result.stderr}"
        )
    return log_path


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

def time_parse(log_path: str) -> float:
    """Run parse_edac_trace.py on log_path and return elapsed seconds."""
    start = time.perf_counter()
    result = subprocess.run(
        [
            sys.executable, str(PARSE_PY),
            "--input", log_path,
            "--output", os.devnull,
            "--no-stats",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    elapsed = time.perf_counter() - start
    if result.returncode not in (0, 1):
        # exit 1 is acceptable (empty input / no matching events)
        raise RuntimeError(
            f"parse_edac_trace.py failed (rc={result.returncode}):\n{result.stderr}"
        )
    return elapsed


def benchmark_size(n_events: int, log_path: str, verbose: bool) -> dict:
    """Run BENCH_RUNS timed parses and compute statistics."""
    # Warmup
    for _ in range(WARMUP_RUNS):
        time_parse(log_path)

    samples = []
    for i in range(BENCH_RUNS):
        t = time_parse(log_path)
        samples.append(t)
        if verbose:
            print(f"  run {i+1}/{BENCH_RUNS}: {t:.4f}s")

    p95 = quantiles(samples, n=20)[18] if len(samples) >= 2 else samples[0]  # ~95th
    result = {
        "events": n_events,
        "runs": BENCH_RUNS,
        "mean_s": round(mean(samples), 4),
        "min_s": round(min(samples), 4),
        "max_s": round(max(samples), 4),
        "p95_s": round(p95, 4),
        "p95_s_per_10k": round(p95 / n_events * 10_000, 4),
        "slo_passed": (p95 / n_events * 10_000) < SLO_P95_S_PER_10K,
    }
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark parse_edac_trace.py")
    parser.add_argument(
        "--output", "-o",
        default="tools/benchmark_results.json",
        help="Path to write JSON results (default: tools/benchmark_results.json)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print per-run timings",
    )
    parser.add_argument(
        "--sizes",
        default=",".join(str(s) for s in EVENT_SIZES),
        help=f"Comma-separated event counts to benchmark (default: {EVENT_SIZES})",
    )
    args = parser.parse_args()

    sizes = [int(s) for s in args.sizes.split(",")]

    print(f"Benchmarking parse_edac_trace.py — SLO: p95 < {SLO_P95_S_PER_10K}s per 10k events")
    print(f"Sizes: {sizes}")
    print()

    results = []
    all_passed = True

    with tempfile.TemporaryDirectory(prefix="bench_replay_") as tmpdir:
        for n in sizes:
            print(f"[{n:>7,} events] generating log ... ", end="", flush=True)
            log_path = generate_log(n, tmpdir)
            size_kb = os.path.getsize(log_path) / 1024
            print(f"{size_kb:.0f} KB  timing ... ", end="", flush=True)

            try:
                r = benchmark_size(n, log_path, args.verbose)
            except Exception as e:
                print(f"ERROR: {e}")
                all_passed = False
                continue

            status = "PASS" if r["slo_passed"] else "FAIL"
            print(
                f"mean={r['mean_s']:.3f}s  p95={r['p95_s']:.3f}s  "
                f"p95/10k={r['p95_s_per_10k']:.3f}s  [{status}]"
            )
            if not r["slo_passed"]:
                all_passed = False
            results.append(r)

    output = {
        "slo_p95_s_per_10k": SLO_P95_S_PER_10K,
        "results": results,
        "passed": all_passed,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\nResults written to {out_path}")
    print(f"\nOverall: {'PASSED' if all_passed else 'FAILED'}")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
