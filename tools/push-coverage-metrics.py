# SPDX-License-Identifier: Apache-2.0
"""
tools/push-coverage-metrics.py — Push pytest-cov coverage results to Prometheus Pushgateway.

Usage:
  python3 -m pytest ... --cov-report=json:coverage.json
  python3 tools/push-coverage-metrics.py coverage.json

Environment:
  PUSHGATEWAY_URL  — default http://localhost:9091
  JOB_NAME         — default hw-fault-coverage
  CI_COMMIT_SHA    — git commit SHA (label)
"""
import json, os, sys, urllib.request, urllib.error, time

PUSHGATEWAY_URL = os.environ.get("PUSHGATEWAY_URL", "http://localhost:9091")
JOB_NAME = os.environ.get("JOB_NAME", "hw-fault-coverage")
COMMIT_SHA = os.environ.get("CI_COMMIT_SHA", "unknown")[:8]

THRESHOLDS = {
    "ml": 85, "anomaly": 85, "aging": 85, "correlation": 85,
    "gpu": 70, "storage": 70, "bmc": 70, "network": 70,
    "power_cxl": 70, "remediation": 70,
}

def parse_coverage_json(path):
    with open(path) as f:
        data = json.load(f)
    # coverage.json format: {"totals": {...}, "files": {"path": {"summary": {"percent_covered": N}}}}
    totals = data.get("totals", {})
    files = data.get("files", {})

    # Group by package (first path component)
    pkg_stats = {}
    for filepath, info in files.items():
        parts = filepath.replace("\\", "/").split("/")
        pkg = parts[0] if parts else "unknown"
        summary = info.get("summary", {})
        covered = summary.get("covered_lines", 0)
        total = summary.get("num_statements", 0)
        if pkg not in pkg_stats:
            pkg_stats[pkg] = {"covered": 0, "total": 0}
        pkg_stats[pkg]["covered"] += covered
        pkg_stats[pkg]["total"] += total

    results = {}
    for pkg, stats in pkg_stats.items():
        t = stats["total"]
        results[pkg] = (stats["covered"] / t * 100) if t > 0 else 0.0

    overall = totals.get("percent_covered", 0.0)
    return results, overall

def build_prometheus_text(pkg_coverage, overall):
    lines = []
    lines.append("# HELP coverage_percent Test coverage percentage per package")
    lines.append("# TYPE coverage_percent gauge")
    for pkg, pct in pkg_coverage.items():
        lines.append(f'coverage_percent{{package="{pkg}",commit="{COMMIT_SHA}"}} {pct:.1f}')

    lines.append("# HELP coverage_overall Overall test coverage percentage")
    lines.append("# TYPE coverage_overall gauge")
    lines.append(f'coverage_overall{{commit="{COMMIT_SHA}"}} {overall:.1f}')

    lines.append("# HELP coverage_threshold_met 1 if package meets its coverage threshold")
    lines.append("# TYPE coverage_threshold_met gauge")
    for pkg, pct in pkg_coverage.items():
        threshold = THRESHOLDS.get(pkg, 70)
        met = 1 if pct >= threshold else 0
        lines.append(f'coverage_threshold_met{{package="{pkg}",threshold="{threshold}"}} {met}')

    lines.append("# HELP coverage_last_updated Unix timestamp of last coverage push")
    lines.append("# TYPE coverage_last_updated gauge")
    lines.append(f"coverage_last_updated {int(time.time())}")

    return "\n".join(lines) + "\n"

def push_to_gateway(text):
    url = f"{PUSHGATEWAY_URL}/metrics/job/{JOB_NAME}"
    data = text.encode("utf-8")
    req = urllib.request.Request(url, data=data, method="PUT",
                                  headers={"Content-Type": "text/plain"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"Pushed to {url}: HTTP {r.status}")
    except urllib.error.URLError as e:
        print(f"WARNING: Push failed ({e}) — metrics not sent", file=sys.stderr)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: push-coverage-metrics.py <coverage.json>", file=sys.stderr)
        sys.exit(1)
    pkg_cov, overall = parse_coverage_json(sys.argv[1])
    text = build_prometheus_text(pkg_cov, overall)
    print(text)
    push_to_gateway(text)
