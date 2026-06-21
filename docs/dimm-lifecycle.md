# DIMM Lifecycle Management

## Overview

DRAM DIMMs age: as charge retention degrades, correctable errors (CE) accumulate. A DIMM approaching end-of-life shows an *accelerating* CE rate — the derivative goes positive. Catching this early allows planned replacement before an uncorrectable error (UE) causes data loss or downtime.

## Methodology

The aging reporter fits a **linear regression** to the daily CE rate over a configurable window (default 30 days). This gives:

- **Slope** (errors/day²): positive = worsening, zero = stable, negative = recovering
- **Projected days to threshold**: when the CE rate will exceed the warning threshold
- **Health score**: 1.0 (new DIMM) → 0.0 (critical, replace now)

```
CE rate
   │          /  ← observed, slope > 0
   │        /
   │------/-------  ← warn threshold (10 CE/day)
   │    /
   │  /
   │/
   └──────────────── time (days)
       ↑ now    ↑ projected crossing
```

## Metrics

| Metric | Description |
|--------|-------------|
| `dimm_aging_ce_rate_slope{mc,csrow}` | Linear regression slope (CE/day) |
| `dimm_aging_projected_days_to_threshold{mc}` | Days until warn threshold hit |
| `dimm_aging_health_score{mc,csrow}` | Health 0–1 |
| `dimm_aging_r_squared{mc,csrow}` | Regression fit quality |

## Thresholds

| Threshold | Default | Action |
|-----------|---------|--------|
| Warn CE/day | 10 | Alert, schedule replacement within 90 days |
| Critical CE/day | 100 | Alert, replace immediately |
| Days-to-threshold warn | 90 | Page if projected crossing < 90 days |

## Usage

```bash
# Run once
python3 aging/dimm-aging-report.py --output -

# JSON output for integration
python3 aging/dimm-aging-report.py --json

# Custom thresholds
python3 aging/dimm-aging-report.py --warn-threshold 5 --critical-threshold 50 --window 14d
```

## Grafana

Import `deploy/grafana/dimm-aging-dashboard.json`. The main panel overlays observed CE rate (blue line) with the regression line (dashed), letting operators see visually whether a DIMM is trending worse.
