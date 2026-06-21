# Code Review: Observability Scripts

## Summary

Seven Python scripts across the observability stack were reviewed for correctness, safety, and robustness. The scripts cover Z-score anomaly detection, event correlation, dmesg watching, DIMM aging analysis, Grafana annotation pushing, rasdaemon SQLite export, and ACPI BERT parsing. Overall code quality is good — the scripts are well-structured, have clear docstrings, and use only stdlib dependencies. However, six concrete issues were found ranging from one blocker (incorrect CPER struct field offsets) to several majors and minors.

---

## Findings

### [BLOCKER] Incorrect CPER field offsets in parse_cper_records
File: `apei/bert-reader.py`  Line: 88–90

**Issue:** After unpacking via `CPER_HEADER_FMT` into `hdr`, the code discards `hdr` and immediately re-reads raw bytes with hard-coded offsets that do not match the format string. The comment maps `hdr[1]` to `Revision(4B)` and `hdr[2]` to `SignatureEnd(2B)`, but the UEFI 2.9 CPER header layout is:

```
offset  0: SignatureStart   16 bytes (char[16], NOT a GUID — it is literally "CPER")
offset 16: Revision          2 bytes (uint16)
offset 18: SignatureEnd      4 bytes (uint32, always 0xFFFFFFFF)
offset 22: SectionCount      2 bytes (uint16)
offset 24: ErrorSeverity     4 bytes (uint32)
offset 28: ValidationBits    4 bytes (uint32)
offset 32: RecordLength      4 bytes (uint32)
```

The code extracts:
- `record_length` from offset `+20` — this is actually `SignatureEnd` (0xFFFFFFFF = 4,294,967,295), not the record length.
- `severity_raw`  from offset `+24` — this is `ValidationBits`, not `ErrorSeverity`.
- `section_count` from offset `+18` — this is correct (SectionCount is at +22 per spec; `+18` lands in `SignatureEnd`, off by 4 bytes).

Additionally, the `CPER_HEADER_FMT` string itself is wrong: it packs `<16sIHBB16sQIIQQ16sI4sI` which totals 128 bytes, but the field layout does not match UEFI 2.9 Appendix N. The format interleaves a spurious 4-byte `I` after the 16-byte signature rather than a 2-byte `H` for Revision.

Using `record_length = struct.unpack_from("<I", region_data, offset + 20)[0]` will almost always return `0xFFFFFFFF`, causing the `record_length < CPER_HEADER_SIZE` guard at line 118 to trigger immediately and break after the first record. In practice the parser returns at most one partially-correct entry.

**Fix:** Redefine the format to match UEFI 2.9 Table N-1 exactly:
```python
CPER_HEADER_FMT = "<16sHIHIII4sI8s20sQ16s"
# Or use named offsets directly:
CPER_SIG_OFFSET      = 0   # 16 bytes
CPER_REVISION_OFFSET = 16  # uint16
CPER_SIGEND_OFFSET   = 18  # uint32
CPER_SECTCNT_OFFSET  = 22  # uint16
CPER_SEVERITY_OFFSET = 24  # uint32 (ErrorSeverity)
CPER_RECLEN_OFFSET   = 32  # uint32 (RecordLength)
```
Replace all magic-offset reads with named constants derived from the corrected format.

---

### [BLOCKER] BERT body offset arithmetic is ambiguous and may read wrong region
File: `apei/bert-reader.py`  Line: 185

**Issue:** The region data extraction logic is:
```python
region_data = raw[region_offset:region_offset + region_length] if region_offset > 0 else raw[ACPI_TABLE_HEADER_SIZE + BERT_BODY_SIZE:]
```
`region_offset` comes from `BootErrorRegionOffset` in the BERT body, which per ACPI 6.5 §18.3.1 is a physical address — not a byte offset into the BERT table file. On real hardware this value is a firmware-assigned physical memory address (e.g. `0x00000000BEFE0000`), making `raw[region_offset:]` an attempt to slice far beyond the end of the table bytes. The result is always an empty `bytes` object, so `parse_cper_records` receives nothing and returns `[]` silently.

When reading from `/sys/firmware/acpi/tables/BERT`, the BERT table only contains the header and the region length/offset fields; the actual error region data lives at the physical address mapped via `/sys/firmware/acpi/tables/data/BERT` (which the script defines as `BERT_REGION_PATH` but never uses in `main()`).

**Fix:**
1. Use `BERT_REGION_PATH` (`/sys/firmware/acpi/tables/data/BERT`) for the actual region data, not `BERT_TABLE_PATH`.
2. Add a check that `region_offset` is 0 when reading from the data sysfs node (the kernel pre-slices it), and use `raw[:region_length]` in that case.
3. Do not attempt `raw[region_offset:]` with a physical address.

---

### [MAJOR] Z-score stddev clamped to 1.0 causes false positives on zero-rate series
File: `anomaly/zscore-detector.py`  Line: 101

**Issue:** `compute_stats` clamps `stddev` to `1.0` when the variance is zero (constant series). For a node with a steady CE rate of 0.0 errors/s (healthy, quiescent), the baseline mean is `0.0` and stddev is forced to `1.0`. Any non-zero current value then produces `zscore = current / 1.0 = current`. For a freshly-observed CE burst of `3.1` errors/s the Z-score is `3.1 > 3.0`, triggering an anomaly — which is the desired behaviour. However, the inverse is equally true: if the historical baseline has a constant non-zero rate (e.g. all 2016 samples are exactly `5.0`), then stddev is forced to `1.0`, and a current reading of `8.1` yields `zscore = (8.1 - 5.0) / 1.0 = 3.1` — an alarm on what may be a negligible perturbation. Conversely, a genuine jump from `5.0` to `5.5` gives `zscore = 0.5`, which is suppressed even though any deviation from a perfectly constant baseline is likely meaningful.

The clamping is not documented in the module docstring or in `compute_stats`'s docstring (which only says "Returns (0, 1) for empty/constant"). There is no `anomaly_baseline_stddev` metric emitted for the constant-series case that would reveal the synthetic `1.0` to an operator.

**Fix:** Add an explicit metric `anomaly_constant_baseline{instance, collector} 1` when stddev was clamped so operators know the Z-score is artificial. Consider an alternative policy: for constant-zero baselines, alarm on `current > 0` directly rather than through Z-score arithmetic. Document the clamping choice in the module docstring.

---

### [MAJOR] EventBuffer thread safety — single-threaded but API implies concurrent access; seen_correlations cleared bluntly
File: `correlation/event-correlator.py`  Lines: 128–159

**Issue (part A — thread safety):** `EventBuffer` has no lock. The class is safe as written because `main()` is strictly single-threaded (reading from `sys.stdin` in a `for` loop, calling `buf.add` and then `detect_correlations` sequentially). However, the class is named and documented as a generic "sliding window event buffer" with no thread-safety disclaimer. If anyone wraps it with a background expiry thread or reads from multiple producers (a plausible future change given the multi-source pipeline), `_expire`'s list-rebuild and `add`'s `append` could race. A `threading.Lock` around `add` and `_expire` would cost nothing and prevent the footgun.

**Issue (part B — seen_correlations blunt reset):** At line 158–159:
```python
if len(seen_correlations) > 100:
    seen_correlations.clear()
```
This resets deduplication state for all correlation types simultaneously. If the buffer holds a genuine ongoing storm (say 80 `edac_ce` events over 60 s), the `event_storm` entry in `seen_correlations` will be cleared after 100 unrelated entries accumulate. The very next `detect_correlations` call will re-fire the annotation for the same storm. In a high-frequency environment this causes annotation spam. The intent seems to be "allow recurring storms to re-fire after some time", but the condition is on *count*, not *time*.

Rule 1 (`memory_multi_source`) and Rule 2 (`pcie_memory_cascade`) deduplicate on `("memory_multi_source", "")` — the empty string because `corr.get("event_type", "")` returns `""` for those rules. This means only one annotation ever fires per run for a multi-source burst, even if EDAC CE + MCE events continue for hours. The suppression is too aggressive for those rules while simultaneously too loose for storm rules.

**Fix (part A):** Add `self._lock = threading.Lock()` in `__init__` and acquire it in `add`, `_expire`, and `count_by_type`. Add a `# Not thread-safe` comment if the lock is intentionally omitted.

**Fix (part B):** Replace the count-based clear with a time-based per-key expiry: store `{key: last_fired_time}` and re-fire correlations only after a configurable cool-down (e.g. 5 minutes).

---

### [MAJOR] OLS regression: single data point returns slope 0 but `days_to_threshold` treats it as infinite
File: `aging/dimm-aging-report.py`  Lines: 43–44, 68–75

**Issue:** `linear_regression` returns `(0.0, y_vals[0], 0.0)` for `n < 2`. This is reasonable for the intercept but the slope of `0.0` means `days_to_threshold` returns `float("inf")` for any single-point series (via the `slope <= 0` guard at line 70). A DIMM with one data point showing a CE rate of 150/day (well above `warn_threshold=10`) will report `days_to_threshold = inf` and `health_score = 0.0` (correctly bad health) but the projected threshold metric in Prometheus will show `1e308` — which Grafana will render as "never will fail", directly contradicting the `health_score=0.0` reading. An operator could be misled into ignoring a DIMM that is already above threshold.

Additionally, `days_to_threshold` does not handle the case where `current_rate` is already above `warn_threshold` when the slope is negative (improving health). `remaining = threshold - current_rate` is negative, so `remaining / slope` yields a positive number (negative / negative), meaning the function returns a finite positive number of days — as if the threshold will be reached in the future — when the DIMM is already in violation. This is mathematically backwards.

**Fix:**
- For `n < 2`, set `slope = 0.0` but also check in `days_to_threshold` whether `current_rate >= threshold` before consulting the slope: if already above threshold, return `0.0` regardless.
- In `days_to_threshold`, handle negative slope correctly: if `slope < 0` and `current_rate >= threshold`, return `0.0`; if `slope < 0` and `current_rate < threshold`, return `float("inf")` (improving, will never reach threshold).
- Add a note in the Prometheus output or a separate metric when `r_squared < 0.1` (regression unreliable) so operators know the projection is not trustworthy.

---

### [MAJOR] SQLite read-only URI is enforced at the OS level but missing error context on connection failure
File: `ras/rasdaemon-exporter.py`  Lines: 127–129

**Issue (correctness — the question asked):** `sqlite3.connect("file:{path}?mode=ro", uri=True)` does enforce read-only at the SQLite VFS level, not just advisory. SQLite's URI `mode=ro` maps to `SQLITE_OPEN_READONLY` which will cause any write attempt to fail with `sqlite3.OperationalError: attempt to write a readonly database`. It is not advisory. This is correct behaviour for an exporter.

However, if `args.db` does not exist at connection time (a race between the `args.db.exists()` guard at line 120 and the actual `connect` call — possible if rasdaemon rotates the file), `sqlite3.connect` with `mode=ro` raises `sqlite3.OperationalError: unable to open database file` rather than creating a new database (the default behaviour without `mode=ro`). This exception is not caught; it propagates to the top level and kills the process with a traceback rather than the clean error path at line 122–123.

**Issue (missing error handling):** `mc_conn.execute("SELECT COUNT(*) FROM mc_event")` at line 129 is outside any try/except. If the schema is absent (empty DB, rasdaemon schema migration) this raises an uncaught `sqlite3.OperationalError`.

**Fix:**
```python
try:
    mc_conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    mc_counts = query_mc_events(mc_conn, since_epoch)
    total = mc_conn.execute("SELECT COUNT(*) FROM mc_event").fetchone()[0]
    mc_conn.close()
except sqlite3.OperationalError as exc:
    sys.stderr.write(f"rasdaemon-exporter: DB error: {exc}\n")
    output = emit_metrics({}, {}, 0)
```

---

### [MINOR] No retry on Prometheus network timeout; single failure aborts the run
File: `anomaly/zscore-detector.py`  Line: 55 and Line: 72; also `aging/dimm-aging-report.py`  Line: 103

**Issue:** Both `query_range` (timeout=15 s) and `query_instant` (timeout=10 s) are called without any retry. A single transient network timeout raises an unhandled `urllib.error.URLError` which propagates to `main()`, is caught at line 166, written to stderr, and causes `sys.exit(1)`. For a systemd timer that runs every 5 minutes this means one blip in Prometheus availability drops a data point from the anomaly detector's own `anomaly_detector_last_run_timestamp` metric. More critically, the anomaly textfile is not updated, so node_exporter continues serving stale data silently. The same pattern exists in `dimm-aging-report.py`.

`annotation-pusher.py` and `event-correlator.py` do handle Grafana POST failures gracefully (log and continue), but neither retries.

**Fix:** Wrap the Prometheus fetch in a small retry loop (e.g. 3 attempts with 2 s back-off) before raising. Alternatively, write an empty/comment-only `.prom` file on failure so node_exporter raises a stale-file alert rather than silently serving old data.

---

### [MINOR] dmesg `--time-format=reltime` is not available on all kernel versions; no fallback
File: `correlation/dmesg-watcher.py`  Line: 61

**Issue:** `dmesg --time-format=reltime` requires util-linux >= 2.26 (kernel 3.17+ for the underlying monotonic timestamp support). On older embedded ARM targets or minimal initramfs environments the flag is rejected and `subprocess.Popen` launches a process that immediately exits with an error, causing the `for line in proc.stdout` loop to emit nothing with no logged error. The caller (`event-correlator.py`) silently processes zero events.

**Fix:** Catch `proc.returncode != 0` after the loop (or check immediately after `Popen`), log a warning, and fall back to plain `dmesg` output with a regex that matches both `[timestamp]` and human-readable formats.

---

### [MINOR] annotation-pusher.py: `seen` fingerprint set never expires; long-running mode leaks memory
File: `annotations/annotation-pusher.py`  Lines: 110, 131–136

**Issue:** In continuous (`--once` not set) mode, `seen` accumulates fingerprints for every alert ever pushed. If the same alert fires, clears, and re-fires (a common pattern with flapping hardware errors), its fingerprint remains in `seen` and the re-fire is silently dropped. The comment at line 157 in `event-correlator.py` (a sibling script) acknowledges this same problem and addresses it with the blunt 100-entry clear — `annotation-pusher.py` makes no such accommodation at all.

On hardware with many transient alerts (a common EDAC scenario), the set also grows without bound.

**Fix:** Store `{fingerprint: last_push_time}` and re-push after a configurable suppress window (e.g. 30 minutes). This both prevents memory growth and correctly re-annotates recurring faults.

---

### [MINOR] BERT region bounds check uses `region_offset` without validating it is within `raw`
File: `apei/bert-reader.py`  Line: 64–67 and Line: 185

**Issue:** `parse_bert_table` checks that `len(data) >= ACPI_TABLE_HEADER_SIZE + BERT_BODY_SIZE` (48 bytes minimum), but does not validate that `region_offset` is a sensible offset into `raw`. As noted in the BLOCKER finding above, `region_offset` is a physical address on real hardware, but even if treated as a file offset, there is no check that `region_offset + region_length <= len(raw)` before slicing. A malformed or truncated BERT file silently produces an empty `region_data` bytes object.

**Fix:** After unpacking, validate:
```python
if region_offset != 0 and region_offset + region_length > len(data):
    raise ValueError(f"BERT region [{region_offset}:{region_offset+region_length}] exceeds table size {len(data)}")
```

---

### [NIT] `compute_stats` uses population variance, not sample variance
File: `anomaly/zscore-detector.py`  Line: 100

**Issue:** `variance = sum((x - mean) ** 2 for x in values) / n` divides by `n` (population variance). For a 7-day baseline at 5-minute intervals `n` ≈ 2016, so the difference between `n` and `n-1` (Bessel's correction) is negligible (< 0.05%). This is not a correctness issue at the scale used, but should be noted in a comment so future maintainers do not apply Bessel's correction thinking it is missing.

---

### [NIT] `emit_prometheus` uses `gauge` type for `ras_mc_event_total` and `ras_aer_event_total`
File: `ras/rasdaemon-exporter.py`  Lines: 84, 91

**Issue:** Both metrics are declared `# TYPE ... counter` in the `emit_metrics` function, which is correct for monotonically increasing event totals. However, because this is a textfile collector that re-reads the DB on each run and re-emits counts from a `WHERE timestamp >= ?` filter, the value can decrease between runs (if `--since` trims old events). Prometheus will record a counter reset and emit spurious `rate()` spikes. The semantics are actually gauge-like (a snapshot count for a time window), but the metric names end in `_total` which the Prometheus data model reserves for counters.

**Fix:** Either rename to `ras_mc_event_window_count` / `ras_aer_event_window_count` with type `gauge`, or always query all events (no time filter) and rely on the monotonically increasing DB to keep the counter valid.

---

### [NIT] `_window_to_seconds` duplicated across two files
Files: `anomaly/zscore-detector.py` Line: 89; `aging/dimm-aging-report.py` Line: 89

**Issue:** Identical function body copied verbatim. Neither file supports `w` (week) unit despite defining it in the dict, while the `--window` argument in `zscore-detector.py` uses `DEFAULT_WINDOW = "7d"` (not `"1w"`), so the unit is mostly vestigial. No functional bug but a maintenance hazard.

**Fix:** Extract into a shared `observability/utils.py` module.

---

## Metrics

- Files reviewed: 7
- Blockers: 2
- Majors: 4
- Minors: 3
- Nits: 3
