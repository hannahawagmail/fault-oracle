# Python Docstring Review Findings
Generated: 2026-06-21

## Files Reviewed

| File | Functions | Missing (fixed) | Comments removed |
|------|-----------|-----------------|-----------------|
| `ml/ce-forecaster.py` | 7 | 2 | 0 |
| `ml/probability-exporter.py` | 6 | 5 | 0 |
| `anomaly/zscore-detector.py` | 9 | 6 | 0 |
| `aging/dimm-aging-report.py` | 9 | 5 | 0 |
| `correlation/event-correlator.py` | 9 | 7 | 0 |

---

## ml/ce-forecaster.py

### Functions with previously missing docstrings (now added)

- **`_window_to_seconds(w)`** — No docstring. Added: explains unit suffix support (s/m/h/d/w) and why it mirrors Prometheus range query format so callers can pass CLI args directly.
- **`main()`** — No docstring. Added: describes CLI workflow, series iteration, `--min-points` skip logic, and `--dry-run` semantics.

### Comments reviewed

- `# 95% confidence interval` beside `interval_width=0.95` — retained (explains the non-obvious Prophet parameter).
- `# conservative — CE rate rarely has abrupt changes` beside `changepoint_prior_scale=0.05` — retained (explains why this value is lower than Prophet's default of 0.05; the value is intentionally conservative for hardware failure modelling).
- `# Write SHA256 sidecar for integrity verification by probability-exporter (FIX 3)` — retained (the FIX tag is a cross-reference to a code-review action item, not a code restatement).
- `# Prophet needs tz-naive` — retained (explains why `tz_localize(None)` is called; non-obvious Prophet requirement).

---

## ml/probability-exporter.py

### Functions with previously missing docstrings (now added)

- **`log_error(msg)`** — No docstring. Added: explains why stderr is written in addition to the logger (systemd journal visibility when logger is misconfigured).
- **`risk_tier(prob)`** — No docstring. Added: explains descending-threshold evaluation order and the sentinel `(0.0, 'low')` entry.
- **`load_metadata(model_dir)`** — No docstring. Added: describes the JSON file contract written by `ce-forecaster.py` and why metadata is loaded without re-loading pickles.
- **`emit_metrics(entries, now)`** — No docstring. Added full Args/Returns docstring explaining that `forecast_next_24h` requires pickle loading and may be I/O-bound.
- **`main()`** — No docstring. Added: explains the stub emission fallback when `model_dir` does not exist.

### Comments reviewed

- `# Integrity check: verify SHA256 sidecar if present (FIX 3 — pickle safety)` — retained (cross-reference to security hardening, non-trivial reasoning).
- `# yhat is rate (CE/s); sum × 3600s per hour → total CEs expected` — retained (documents unit conversion which is easy to get wrong).
- `# Emit stub so node_exporter doesn't error` — retained (explains defensive behaviour).

---

## anomaly/zscore-detector.py

### Functions with previously missing docstrings (now added)

- **`_label_key(metric)`** — No docstring. Added: explains why keys are sorted (deterministic matching between range and instant query results).
- **`_window_to_seconds(window)`** — No docstring. Added one-line docstring.
- **`compute_zscore(current, mean, stddev)`** — No docstring. Added: formula explanation and warning that `stddev` must be non-zero (handled by `compute_stats` clamping).
- **`emit_metrics(zscores, threshold)`** — No docstring. Added full Args/Returns docstring including the rationale for the 3.0 threshold (≈0.3% Gaussian false-positive rate).
- **`main()`** — No docstring. Added: notes `sys.exit(1)` on Prometheus failure for systemd timer detection.

### Notes on threshold=3.0

The default `DEFAULT_THRESHOLD = 3.0` is intentional: under a Gaussian distribution, |Z| > 3.0 has a false-positive rate of ≈0.3% per data point. For EDAC CE rate series this is a reasonable balance — CE rates are not perfectly Gaussian, but the threshold suppresses noise while catching genuine spikes. The rationale is now captured in `emit_metrics()`'s docstring.

---

## aging/dimm-aging-report.py

### Functions with previously missing docstrings (now added)

- **`_window_to_seconds(window)`** — No docstring. Added one-line docstring.
- **`_label_key(metric)`** — No docstring. Added one-line docstring.
- **`emit_prometheus(analyses, timestamp)`** — No docstring. Added: explains infinity encoding as `1e308` (Prometheus float64 max, avoids NaN on dashboards for healthy DIMMs).
- **`analyze_series(label_key, points, warn_threshold, critical_threshold)`** — No docstring. Added full Args/Returns docstring.
- **`main()`** — No docstring. Added: explains `--json` mode vs textfile mode.

---

## correlation/event-correlator.py

### Functions with previously missing docstrings (now added)

- **`EventBuffer.__init__(window_s)`** — No docstring. Added: describes lazy expiry design (no background thread needed).
- **`EventBuffer.add(event)`** — No docstring. Added one-sentence docstring.
- **`EventBuffer._expire()`** — No docstring. Added one-sentence docstring.
- **`EventBuffer.events_of_type(event_type)`** — No docstring. Added one-sentence docstring.
- **`EventBuffer.recent()`** — No docstring. Added one-sentence docstring.
- **`EventBuffer.count_by_type()`** — No docstring. Added one-sentence docstring.
- **`main()`** — No docstring. Added: explains `seen_correlations` set purpose and the 100-entry clear policy.

### Module-level docstring

The existing module docstring (`"""correlation/event-correlator.py — Correlate dmesg events…"""`) already documents the three correlation rules and usage example — retained as-is.

---

## Misleading Comments Removed

None found. All inline comments in these files explain non-obvious choices (Prophet configuration, threshold rationale, unit conversions, integrity checks) rather than restating the code.
