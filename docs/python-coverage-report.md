# Python Test Coverage Report

Generated: 2026-06-21

Coverage measured with `pytest-cov 7.1.0` on Python 3.10.12.
All hardware-dependent entry points (`_run`, `run_command`, `run_ipmitool_*`,
`query_prometheus_*`, `generate_runbook`, `collect_*`, `detect_*`, `main`,
`if __name__`) were annotated with `# pragma: no cover` as they require
real hardware (IPMI BMC, nvidia-smi, ibstat, storcli, smartctl, ndctl, etc.)
or external network services (Prometheus, Loki, Kubernetes API, Anthropic API).

## Per-Package Summary

| Package | Stmts | Miss | Coverage | Threshold | Status |
|---------|-------|------|----------|-----------|--------|
| ml/ | 147 | 10 | 93% | 85% | **PASS** |
| anomaly/ | 58 | 1 | 98% | 85% | **PASS** |
| aging/ | 81 | 4 | 95% | 85% | **PASS** |
| gpu/ | 179 | 20 | 89% | 70% | **PASS** |
| bmc/ | 394 | 95 | 76% | 70% | **PASS** |
| network/ | 164 | 20 | 88% | 70% | **PASS** |
| power_cxl/ | 353 | 99 | 72% | 70% | **PASS** |
| storage/ | 387 | 143 | 63% | 70% | **FAIL** |
| remediation/ | 283 | 89 | 69% | 70% | **FAIL** |

**Total: 239 passed, 4 skipped, 0 failed**

## Packages Below Threshold

### storage/ — 63% (threshold: 70%)

Root cause: `storage/controller_collector.py` (RAID controller via storcli) is
not imported by any test and scores 0% (63/63 statements uncovered). The
other modules score well:
- `nvme_collector.py`: 95% (6 miss — hardware subprocess branches)
- `sata_collector.py`: 81% (16 miss — hardware subprocess branches)
- `wear_predictor.py`: 48% (58 miss — Prometheus network query helpers and
  `emit_metrics` pure-logic function untested; `extract_device_series` and
  `emit_metrics` need unit tests)

**Recommended fix**: add tests for `emit_metrics`, `extract_device_series`, and
the controller detection logic in `controller_collector.py` using mock sysfs paths.

### remediation/ — 69% (threshold: 70%)

Root cause: `webhook-server.py` lacks direct unit tests (58/147 statements
uncovered — HTTP handler class and helper functions). The core
`remediation-controller.py` logic scores 77% after pragmas.

**Recommended fix**: add mock HTTP handler tests for `WebhookHandler.do_POST`
and `handle_replacement_confirmed` / `handle_false_positive`.

## Uncovered Lines in Key Files

### storage/wear_predictor.py (48%)
- Lines 50, 59: `ols_slope_intercept` edge cases (n<2 and denom==0 branches) — missing test for 0 or 1-point series
- Lines 97-98, 110-111, 126: `predict_device` branches for insufficient span and zero slope
- Lines 169-183: `extract_device_series` — pure parser, no test; add one unit test
- Lines 196-272: `emit_metrics` — pure Prometheus text formatter; add one unit test with mock predictions

### storage/controller_collector.py (0%)
- Entire file untested — `detect_drivers`, `collect_megaraid`, `emit_metrics`
- All three functions are pure logic over filesystem paths / JSON; testable with mock paths

### remediation/webhook-server.py (61%)
- Lines 73-76, 98-99, 114-115: Prometheus counter helpers (no test)
- Lines 184-191: `_load_state` / `_save_state` file I/O
- Lines 205-242: `WebhookHandler.do_POST` HTTP handler
- Lines 264-271, 275: `main()` loop

## `# pragma: no cover` Annotations Applied

The following categories of code were annotated. These are lines that require
real hardware or external services and cannot safely run in CI:

| Pattern | Example | Reason |
|---------|---------|--------|
| `def run_command(...)` | storage/, gpu/ | Calls nvme-cli, smartctl, storcli |
| `def _run(...)` | network/, gpu/ | Calls ibstat, perfquery, nvidia-smi, ethtool |
| `def run_ipmitool_sel/sdr(...)` | bmc/ | Calls ipmitool over IPMI LAN |
| `def query_prometheus_range/instant(...)` | anomaly/, aging/, ml/, storage/, remediation/ | Real Prometheus network call |
| `def query_range_series(...)` | aging/ | Real Prometheus network call |
| `def fetch_dbe_series(...)` | gpu/ | Real Prometheus network call |
| `def generate_runbook(...)` | ml/ | Real Anthropic API call |
| `def query_prometheus_instant(...)` | ml/ | Real Prometheus network call |
| `def collect_device(...)` | storage/ | Calls nvme-cli/smartctl per device |
| `def collect_megaraid(...)` | storage/ | Calls storcli |
| `def collect_device_metrics(...)` | network/ | Calls ibstat/perfquery |
| `def collect_ethtool_roce(...)` | network/ | Calls ethtool |
| `def collect_throttle_data(...)` | gpu/ | Calls nvidia-smi |
| `def collect_xid_errors(...)` | gpu/ | Calls nvidia-smi dmon |
| `def collect_ecc_counts(...)` | gpu/ | Calls nvidia-smi |
| `def detect_gpus(...)` | gpu/ | Calls nvidia-smi |
| `class KubeClient` | remediation/ | Kubernetes REST API client |
| `def remediate_node(...)` | remediation/ | Live k8s patch + Loki push |
| `def push_loki(...)` | remediation/ | Loki HTTP push |
| `def start_metrics_server(...)` | remediation/ | Starts an HTTP server |
| `def main(...)` | all | CLI entry point |
| `if __name__ == "__main__"` | all | Script guard |

## How to Run Coverage Locally

```bash
# Install pytest-cov
pip install pytest-cov

# Run all covered packages
cd /path/to/arm-linux-fault-resilience
python3 -m pytest \
  ml/tests/ anomaly/tests/ aging/tests/ \
  gpu/tests/ storage/tests/ bmc/tests/ \
  network/tests/ power_cxl/tests/ remediation/tests/ \
  --cov=ml --cov=anomaly --cov=aging \
  --cov=gpu --cov=storage --cov=bmc \
  --cov=network --cov=power_cxl --cov=remediation \
  --cov-report=term-missing \
  --cov-report=html:htmlcov/ \
  --cov-fail-under=70

# Or use the Makefile targets:
make coverage-python
make coverage-go
```
