# ML Pipeline Code-Review Fixes

Applied 2026-06-20. All 77 tests pass after these changes.

---

## FIX 1 — Silent API failure (BLOCKER)

**Files:** `ml/ai-runbook.py`, `ml/ai_runbook.py`

**Function:** `generate_runbook()`

`generate_runbook()` previously made a single unguarded `urllib.request.urlopen` call. Any transient
network error, timeout, or rate-limit response would propagate as an unhandled exception, crashing
the webhook handler and leaving no runbook on disk.

**Change:** Wrapped the API call in a retry loop (3 attempts, backoffs 2 s / 4 s / 8 s).

- On `urllib.error.HTTPError` with code 429, the loop reads `Retry-After` from the response header
  (`int(e.headers.get('retry-after', backoff))`) and sleeps that many seconds before retrying.
- On any other exception the loop sleeps the scheduled backoff and retries.
- After all three attempts fail, a fallback stub is written to the output path:

  ```
  # Runbook generation failed

  **Alert:** <alertname>
  **Node:** <instance>

  Fall back to `docs/runbook.md` for manual procedures.
  ```

- The error is logged via `log.error` but the function never raises. The caller always gets a
  usable string back.

`generate_runbook()` signature changed from `(context, alertname, api_key)` to
`(context, alertname, instance, api_key, out_path=None)`. `main()` updated accordingly.

---

## FIX 2 — Label anonymization before external API call (SECURITY)

**Files:** `ml/ai-runbook.py`, `ml/ai_runbook.py`

**Function:** `build_context()`

The real hostname (`instance`) was interpolated directly into the prompt string sent to the
Anthropic API, potentially violating data-residency requirements.

**Change:** At the top of `build_context()`, before any string interpolation used in the prompt,
the instance is replaced with a stable pseudonym:

```python
# Anonymize hostname before sending to external API — data residency
anon_instance = "node-" + hashlib.sha256(instance.encode()).hexdigest()[:8]
```

The real `instance` label is still used for Prometheus queries (which stay within the cluster).
Only `anon_instance` appears in the context string sent to Claude. `hashlib` was already imported.

The docstring was updated with a data-residency note explaining the approach.

The corresponding test `test_build_context_contains_instance` in `ml/tests/test_ml_pipeline.py`
was updated to assert the anonymized form (`node-<sha256[:8]>`) rather than the raw hostname.

---

## FIX 3 — Pickle integrity check (BLOCKER)

**Files:** `ml/probability-exporter.py`, `ml/probability_exporter.py`, `ml/ce-forecaster.py`

**Functions:** `forecast_next_24h()` (exporter), `save_model()` (forecaster)

A tampered or corrupted `.pkl` file could be loaded silently, producing wrong predictions or
executing arbitrary code via pickle deserialization.

**Change — writer side (`ce-forecaster.py` `save_model()`):** After writing the pickle, a SHA256
sidecar is written alongside it:

```python
with open(pkl_path, 'rb') as f:
    digest = hashlib.sha256(f.read()).hexdigest()
with open(pkl_path.replace('.pkl', '.sha256'), 'w') as f:
    f.write(digest + '\n')
```

**Change — reader side (`probability-exporter.py` `forecast_next_24h()`):** After `pickle.load`,
if a `.sha256` sidecar exists the file is re-hashed and compared:

```python
sha_path = model_path.replace('.pkl', '.sha256')
if os.path.exists(sha_path):
    with open(sha_path) as sf:
        expected = sf.read().strip()
    with open(model_path, 'rb') as mf:
        actual = hashlib.sha256(mf.read()).hexdigest()
    if actual != expected:
        log_error(f"Model integrity check FAILED for {model_path} — skipping")
        return 0.0
```

If the check fails the function logs the error and returns `0.0` rather than using the suspect
model. `hashlib` and `log_error` helper added to both exporter files.

---

## FIX 4 — Source label on survival metrics (MAJOR)

**Files:** `ml/survival-analysis.py`, `ml/survival_analysis.py`

**Functions:** `emit_metrics()`, `main()`

`dimm_survival_probability` metrics gave no indication of whether they were derived from real fleet
observations or the synthetic prior. Dashboards could not distinguish the two cases.

**Change:** `emit_metrics()` gained a `data_source: str = "observed"` parameter. The label is
appended to every `dimm_survival_probability` line:

```
dimm_survival_probability{cohort="ddr4_rank1_lt3yr",days="30",source="synthetic"} 0.950000
```

`main()` now sets `data_source = "synthetic"` when `build_synthetic_events()` is called and
`data_source = "observed"` when real data is loaded from `dimm-events.jsonl`.

---

## FIX 5 — Unhandled OperationalError in rasdaemon exporter (MAJOR)

**Files:** `ras/rasdaemon-exporter.py`, `ras/rasdaemon_exporter.py`

**Functions:** `query_mc_events()`, `query_aer_events()`

`query_mc_events()` had no exception handling: a corrupt database, missing table, or locked file
would raise `sqlite3.OperationalError` / `sqlite3.DatabaseError` and crash the exporter, stopping
all RAS metric collection. `query_aer_events()` already caught `OperationalError` but not the
broader `DatabaseError`.

**Change:** Both functions now wrap the connect/execute block in:

```python
except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
    log_info(f"SQLite error querying {conn}: {e}")
    return {}
```

A `log_info` helper (stderr + `logging`) was added. The exporter degrades gracefully: it emits
empty metric sets rather than crashing.

---

## FIX 6 — Document stddev clamp with synthetic gauge (MINOR)

**Files:** `anomaly/zscore-detector.py`, `anomaly/zscore_detector.py`

**Functions:** `compute_stats()`, `emit_metrics()`, `run()`

When a baseline series is constant or empty, `compute_stats()` clamped stddev to `1.0` to avoid
division by zero. This was invisible to dashboards: a node with a flat CE rate looked identical to
a node with genuine variance.

**Change:** `compute_stats()` now returns a three-tuple `(mean, stddev, clamped: bool)`:

```python
if variance > 0:
    return mean, math.sqrt(variance), False
return mean, 1.0, True   # clamped
```

`run()` unpacks all three values and stores `stddev_clamped` in the result dict.

`emit_metrics()` emits a new gauge for each series:

```
# HELP anomaly_synthetic_stddev 1 if stddev was clamped to 1.0 (constant or empty baseline).
# TYPE anomaly_synthetic_stddev gauge
anomaly_synthetic_stddev{instance="node1",collector="edac"} 1
```

`1` means the stddev was synthetic; `0` means it came from real variance. Dashboards can filter or
annotate accordingly.

Tests in `anomaly/tests/test_zscore_detector.py` updated to unpack the three-tuple and assert
`clamped` values. A new test `test_real_stddev_not_clamped` was added.

---

## Test results

```
77 passed in 3.56s
```

Suites covered: `ml/tests/`, `anomaly/tests/`, `ras/tests/`, `apei/tests/`.
