# Code Review: ML Pipeline

## Summary

Five files were reviewed covering the full ML prediction pipeline: Prophet-based CE rate
forecasting (`ce-forecaster.py`), metric export (`probability-exporter.py`), Kaplan-Meier
survival analysis (`survival-analysis.py`), AI-generated runbooks (`ai-runbook.py`), and
the test suite (`tests/test_ml_pipeline.py`).

The pipeline is architecturally sound and the code is clearly written, but it has two
blockers — one a classic deserialization vulnerability and one a silent API failure mode —
plus several major issues that would affect production correctness and reliability. The
statistical approach has notable approximation errors that overstate probabilities under
certain data patterns. Test coverage verifies plumbing but leaves the most important
statistical invariants untested.

---

## Findings

### [BLOCKER] Unsafe `pickle.load()` on model files with no integrity check
File: `ml/probability-exporter.py`  Line: 51-52
File: `ml/ce-forecaster.py`  Line: 135-136

Issue: `probability-exporter.py` loads every `*.pkl` file found under `MODEL_DIR` using
bare `pickle.load()`. Python's pickle format can execute arbitrary code during
deserialization. If an attacker can write a file to `/var/lib/hw-fault-ml/models/` (e.g.
via a path-traversal in any upstream writer, a symlink, or a compromised build artifact),
they achieve remote code execution as the exporter's service user. There is no HMAC
signature, no hash verification, no allowlist of expected types, and no use of
`pickle.loads()` with a `Unpickler` subclass that restricts `find_class`. The exporter
runs on a 5-minute timer so exploitation requires no special timing.

Fix: Sign every model file at save time (e.g. HMAC-SHA256 of the `.pkl` bytes using a
node-local secret, stored in the companion `.json`). Verify the signature before
`pickle.load()`. Alternatively, migrate to a safer serialization format — Prophet supports
`model_to_dict()` / `model_from_dict()` (JSON-serializable) since Prophet 1.1, which
eliminates pickle entirely.

---

### [BLOCKER] `generate_runbook()` has no retry, no rate-limit handling, and exceptions propagate uncaught to the caller
File: `ml/ai-runbook.py`  Lines: 103-128

Issue: `urllib.request.urlopen(req, timeout=30)` raises `urllib.error.URLError` on network
failure and `urllib.error.HTTPError` on HTTP 4xx/5xx (including 429 rate-limit). Neither
is caught inside `generate_runbook()`. When called from `main()` the exception propagates
and kills the process with a raw traceback — no fallback runbook is written, the
Alertmanager webhook gets an unstructured 500, and the on-call engineer sees nothing. A
single transient 429 or momentary API outage during an active incident (exactly when
runbooks are most needed) causes a silent total failure. There is also no exponential
back-off or jitter.

Fix: Wrap the `urlopen` call with a retry loop (e.g. up to 3 attempts with exponential
back-off starting at 1 s). On `HTTPError` with `code == 429` read the `Retry-After`
header if present. On all non-transient errors (400, 401, 403) fail immediately with a
useful log message. Always write a partial runbook to disk on failure so the on-call
engineer has context even without a Claude-generated body.

```python
import time, urllib.error

MAX_RETRIES = 3

def generate_runbook(context: str, alertname: str, api_key: str) -> str:
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(...)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            return data["content"][0]["text"]
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                raise  # non-retryable
            wait = int(e.headers.get("Retry-After", 2 ** attempt))
            last_exc = e
        except (urllib.error.URLError, TimeoutError) as e:
            wait = 2 ** attempt
            last_exc = e
        time.sleep(wait)
    raise RuntimeError(f"Claude API unavailable after {MAX_RETRIES} attempts") from last_exc
```

---

### [MAJOR] `failure_probability()` conflates a 95% CI bound with a probability — the result is not a calibrated probability
File: `ml/ce-forecaster.py`  Lines: 108-125

Issue: The function counts the fraction of forecast hours where `yhat_upper > threshold`
and returns that fraction as `P(failure)`. This has two statistical errors:

1. `yhat_upper` is the 95th percentile of Prophet's posterior predictive distribution at
   each timestep, not the probability that the true value exceeds the threshold. The
   fraction of hours where the 95th percentile exceeds the threshold can be 1.0 even when
   the median `yhat` is orders of magnitude below the threshold. On noisy but benign data
   (typical early-life DIMMs) with a very low threshold (`1e-6`), this metric will fire at
   1.0 constantly, producing perpetual "critical" alerts.

2. Using `yhat_upper` means the metric responds to forecast uncertainty, not to actual
   expected CE rate — a wider CI (short series, high noise) produces higher
   `failure_probability` regardless of trend, which punishes data-sparse DIMMs.

Fix: A statistically correct approach is to compute at each future timestep the
probability that `y > threshold` under the Prophet predictive distribution. Prophet reports
`yhat` (mean) and `yhat_upper`/`yhat_lower` as the 95% symmetric CI. Assuming normality
(Prophet's default), the per-step standard deviation is `(yhat_upper - yhat) / 1.96`.
Then:

```python
from scipy.stats import norm

def failure_probability(fc, horizon_days, threshold_rate=1e-6):
    now = pd.Timestamp.now()
    window = fc[(fc["ds"] >= now) & (fc["ds"] <= now + pd.Timedelta(days=horizon_days))]
    if window.empty:
        return 0.0
    sigma = (window["yhat_upper"] - window["yhat"]) / 1.96
    # P(y > threshold) per step, then take max (probability of ever exceeding)
    step_probs = 1.0 - norm.cdf(threshold_rate, loc=window["yhat"], scale=sigma.clip(lower=1e-12))
    # Union bound (conservative) or 1 - prod(1-p) for independent steps
    return float(min(1.0, 1.0 - (1.0 - step_probs).prod()))
```

---

### [MAJOR] Prophet `changepoint_prior_scale=0.05` and `daily_seasonality=True` are poorly matched to short, sparse CE rate series
File: `ml/ce-forecaster.py`  Lines: 90-97

Issue: Two independent misconfiguration risks:

1. `daily_seasonality=True` fits a Fourier series with `order=4` (8 free parameters) for
   within-day periodicity. CE rates from EDAC are hardware error counts — they have no
   genuine 24-hour seasonality. Fitting daily seasonality on a sparse, near-zero series
   (<30 days, as the comment in the module header allows) adds 8 free parameters that the
   data cannot constrain, causing significant overfitting. This inflates `yhat_upper`
   which — combined with finding 1 above — inflates `failure_probability`.

2. The minimum-points guard (`DEFAULT_MIN_POINTS = 14`) allows fitting a model on only 14
   hourly observations (less than 1 day of data). A daily-seasonality model requires at
   minimum 2–3 full cycles (48–72 hours = 48–72 points) to fit the seasonal Fourier
   components. With 14 points the Stan sampler often fails to converge or produces
   degenerate uncertainty intervals.

Fix: Disable `daily_seasonality` for CE rate data. Increase `min_points` to at least 72
(3 days) or, better, 168 (1 week) to support weekly seasonality fitting. Consider
`changepoint_prior_scale=0.01` for CE rate data where abrupt mean-shifts are meaningful
signals that should not be smoothed away.

---

### [MAJOR] Survival analysis silently substitutes synthetic prior data indefinitely — no staleness guard
File: `ml/survival-analysis.py`  Lines: 126-130

Issue: If `dimm-events.jsonl` never exists (no fleet events accumulated) or is
permanently empty, `build_synthetic_events()` is used for every run. The synthetic data is
generated with `random.seed(42)` so it is deterministic, but it reflects 2009/2015
academic failure distributions, not the actual fleet. Since `dimm_survival_probability`
and `dimm_median_lifetime_days` are emitted as real Prometheus metrics with no label
indicating they are synthetic, downstream consumers (dashboards, alerts) treat them as
real. A fleet that has never failed a DIMM will show survival curves identical to a fleet
with 300 days median lifetime, silently.

Additionally, `build_synthetic_events()` uses `random.randint(20, 60)` and
`random.expovariate()` which are seeded globally — if anything else seeds `random` earlier
in the process, the synthetic data changes non-deterministically between runs.

Fix:
- Add a `synthetic` boolean field to each emitted metric via a label
  (`source="synthetic"` vs `source="real"`).
- Emit a dedicated `survival_data_is_synthetic` gauge so alerts can suppress synthetic
  survival metrics.
- Add a staleness check: if `dimm-events.jsonl` is older than N days and not empty,
  warn but continue; if it has never been written, set `survival_data_is_synthetic 1`.
- Replace `random` with `numpy.random.default_rng(42)` to isolate the seed from global state.

---

### [MAJOR] `forecast_next_24h()` silently returns `0.0` on any exception, masking model file corruption or load errors
File: `ml/probability-exporter.py`  Lines: 48-60

Issue: The broad `except Exception: return 0.0` swallows all errors from `pickle.load()`,
`model.predict()`, and the window arithmetic. If a model file is corrupted, the `.json`
metadata points to the wrong path, or pickle fails due to a version mismatch, the metric
is emitted as `0.0` — indistinguishable from a DIMM with zero expected errors. There is no
log line, no error counter, nothing visible in Prometheus. An operator investigating why a
DIMM shows low forecast but high failure probability has no visibility into load failures.

Fix: Log the exception at WARNING level and increment a `ml_model_load_errors_total`
counter metric. At minimum:

```python
except Exception as e:
    log.warning("forecast_next_24h failed for %s: %s", label_key, e)
    return 0.0
```

Add `ml_model_load_errors_total` as a gauge in `emit_metrics()` to make load failures
observable.

---

### [MAJOR] `emit_metrics()` calls `fit_km()` twice per cohort — redundant and non-deterministic
File: `ml/survival-analysis.py`  Lines: 88-100

Issue: `emit_metrics()` iterates `sorted(cohorts)` twice: once for `dimm_survival_probability`
and once for `dimm_median_lifetime_days`. Each pass calls `fit_km()` independently. For
the synthetic data this is deterministic (seeded), but for real data with right-censoring
the KM estimator is deterministic — however the redundant fitting doubles CPU work and
makes the code harder to reason about. More importantly, if `fit_km()` raises (e.g. a
cohort with zero events), the first loop succeeds but the second raises, producing
a partial Prometheus output that node_exporter may reject.

Fix: Fit all KM models once at the start of `emit_metrics()`, store in a dict keyed by
cohort, and reuse:

```python
km_models = {c: fit_km(df, c) for c in sorted(cohorts)}
```

---

### [MINOR] `urllib.parse` is imported inside `query_prometheus_instant()` but used before the import at module level
File: `ml/ai-runbook.py`  Lines: 40-44

Issue: `urllib.parse` is imported inside `query_prometheus_instant()` at line 43, but the
call to `urllib.parse.urlencode()` at line 41 happens before the import statement at
line 43. This is a latent bug — it works only because Python caches module imports, so if
`urllib.parse` happened to have been imported elsewhere first (e.g. `urllib.request`
triggers it as a side-effect), the reference at line 41 succeeds. A second `import
urllib.parse` also exists inside `main()` at line 137. Neither is at the module level
where imports belong.

Fix: Remove both inline `import urllib.parse` statements and add `import urllib.parse` at
the top of the file next to the other stdlib imports.

---

### [MINOR] `load_events()` reads the events file with no error handling — a malformed line crashes the entire exporter
File: `ml/survival-analysis.py`  Line: 39

Issue: `[json.loads(l) for l in path.read_text().splitlines() if l.strip()]` will raise
`json.JSONDecodeError` on the first malformed line and crash the process with no output
written. A single corrupt line in `dimm-events.jsonl` (e.g. from a partial write during a
crash) silently suppresses all survival metrics.

Fix: Wrap in a try/except per line, log and skip bad lines:

```python
rows = []
for i, line in enumerate(path.read_text().splitlines()):
    if not line.strip():
        continue
    try:
        rows.append(json.loads(line))
    except json.JSONDecodeError as e:
        log.warning("dimm-events.jsonl line %d malformed, skipping: %s", i + 1, e)
```

---

### [MINOR] `failure_probability()` window uses `>=now` but forecast is generated from `make_future_dataframe` anchored to training end — window can be empty
File: `ml/ce-forecaster.py`  Lines: 118-122

Issue: `model.make_future_dataframe(periods=horizon_days * 24, freq="h")` generates future
rows starting from the last training timestamp. If training data ends significantly before
`now` (e.g. Prometheus scrape lag, or the model was trained hours ago on a delayed
dataset), `fc["ds"] >= now` may return no rows, causing the function to return `0.0`
rather than a meaningful estimate. The 90-day lookback window makes this likely whenever
the training run is delayed by more than a few hours.

Fix: Use the last training timestamp as the reference for the window rather than
`pd.Timestamp.now()`, or check whether the forecast window is empty and log a warning
before returning 0.0:

```python
if window.empty:
    log.warning("failure_probability: forecast window is empty for horizon=%dd "
                "(last forecast ds=%s, now=%s)", horizon_days, fc["ds"].max(), now)
    return 0.0
```

---

### [MINOR] Test `test_worsening_trend_increases_probability` uses a tolerance of `0.05` that makes it non-falsifiable
File: `ml/tests/test_ml_pipeline.py`  Lines: 67-75

Issue: The assertion `p_worse >= p_flat - 0.05` is true for nearly all inputs because the
tolerance is larger than the expected difference between a flat and a mildly worsening
series when both probabilities are near 0. A bug that reverses the monotonicity would
still pass this test. The test also uses `np.random.normal` without fixing the seed, so it
is non-deterministic — CI runs may see different failure rates.

Fix: Fix the numpy random seed at the start of `make_df()` for deterministic tests. Use a
steeper slope (e.g. `slope=5e-6`) so the probability difference is meaningful and reduce
the tolerance to 0.0 (or a very small value like 0.01).

---

### [MINOR] Test `test_7d_le_30d` tolerance is too loose and the invariant can be violated by the current implementation
File: `ml/tests/test_ml_pipeline.py`  Lines: 86-93

Issue: `p7 <= p30 + 0.05` does not actually enforce that 7-day probability is less than or
equal to 30-day probability — a system where `p7 = 0.9` and `p30 = 0.0` still passes.
This is an important monotonicity invariant (longer horizon should always have equal or
higher probability). Given that `failure_probability()` counts fraction of hours exceeding
threshold, a 7-day window is strictly a subset of a 30-day window so `p7 <= p30` must
hold exactly. The test should assert `p7 <= p30` with no tolerance.

---

### [NIT] `build_synthetic_events()` uses `random.expovariate` which can produce extreme outliers
File: `ml/survival-analysis.py`  Lines: 60-64

Issue: `random.expovariate(1 / median_days)` can produce values of many thousands of days
in the tail (the exponential distribution has no upper bound). With `n` up to 60 samples,
roughly 1–2 samples per cohort can exceed 10 × median. Kaplan-Meier handles this
gracefully, but it produces unrealistically wide survival curves that may confuse
developers inspecting the synthetic output.

Fix: Clip to a reasonable maximum (e.g. `min(duration, median_days * 5)`).

---

### [NIT] `CLAUDE_MODEL` constant references a non-public model identifier
File: `ml/ai-runbook.py`  Line: 34

Issue: `CLAUDE_MODEL = "claude-haiku-4-5-20251001"` uses an internal snapshot identifier
that is not publicly documented. Model identifiers for public Claude APIs use the form
`claude-haiku-4-5` (without a date suffix, or with the documented date suffix from
Anthropic's release notes). Using an undocumented ID risks silent fallback to a different
model or a 404 from the API.

Fix: Use a documented, stable model alias (e.g. `claude-haiku-4-5`) and add a comment
referencing the Anthropic model documentation URL.

---

### [NIT] Test suite imports `build_synthetic_df` from `ce_forecaster` but that function does not exist in the module
File: `ml/tests/test_ml_pipeline.py`  Line: 16

Issue: `from ce_forecaster import (fit_model, forecast, failure_probability, build_synthetic_df)`
— `build_synthetic_df` is not defined anywhere in `ce-forecaster.py`. This import will
raise `ImportError` at collection time, preventing the entire test module from running.

Fix: Remove `build_synthetic_df` from the import. If a helper for generating synthetic CE
rate data is needed for tests, use the local `make_df()` helper already defined in the
test file.

---

### [NIT] No tests cover edge cases: empty series, single-point series, all-zero CE rates
File: `ml/tests/test_ml_pipeline.py`

Issue: `make_df()` always generates a healthy 60-day series. There are no tests for:
- `len(df) == 1` (below `min_points` but should not crash if called directly)
- `df["y"].max() == 0.0` (all-zero CE rate — this is the dominant real-world case for
  healthy DIMMs; Prophet may warn or produce degenerate intervals)
- Empty forecast window returned by `failure_probability()` (covered by finding above)
- `load_events()` receiving an empty file or a file with one malformed line

These are the cases most likely to surface in production before meaningful CE data
accumulates.

---

## Metrics

- Files reviewed: 5
- Blockers: 2
- Majors: 5
- Minors: 4
- Nits: 4
