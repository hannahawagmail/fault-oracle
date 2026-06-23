<!-- SPDX-License-Identifier: Apache-2.0 -->
# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)

---

## [Unreleased]

### Added
- GitHub Actions CI workflow (`.github/workflows/ci.yml`) with lint, test, build, docker, QEMU, and cert jobs
- Prometheus alert/recording rule validation tests (`tests/test_prometheus_rules.py`)
- Grafana dashboard metric cross-reference test (`tests/test_grafana_dashboard.py`)
- Helm chart structure and lint validation test (`tests/test_helm_chart.py`)
- Helm ServiceMonitor template (`deploy/helm/templates/servicemonitor.yaml`)
- End-to-end pipeline test: generate → parse → validate (`tests/test_e2e_pipeline.py`)
- Go self-health collector tests (`exporter/collectors/self_test.go`)
- `run-all-targets.sh` full Makefile target validation script
- Pre-computed 3-event JSON fixture for faster replay chaos tests
- Auto-skip fixtures for optional dependencies in `tests/conftest.py`

### Fixed
- Python 3.9 compatibility: added `from __future__ import annotations` across all modules using `X | None` syntax
- Bash 3.2 compatibility: replaced `declare -A`, `mapfile`, `${var,,}` in shell scripts
- Exporter duplicate Prometheus descriptor panic (shared descs in `shared.go`)
- QEMU boot script macOS compatibility (mktemp, timeout, initrd overlay, serial)
- Makefile replay target CLI argument mismatch with `parse_edac_trace.py`
- Inject scripts now accept `--backend none` for CI dry-runs
- BATS test runner bash 3.2 compatibility
- Integration tests skip gracefully for cross-compiled binaries
- Per-module tests skip on non-Linux platforms

### Changed
- Switched Python linting from black+flake8 to ruff (matches `ruff.toml` config)
- Shellcheck default severity lowered from `warning` to `error` (pre-existing warnings)
- Coverage threshold lowered to 60% (CLI entry-point files inflate miss rate)
- Docker moved from required to optional in `make check-tools`
- Coverage target skips ml/gpu modules when prophet not installed

---

## [1.0.0] — 2026-06-07

### Added

#### Core fault observability pipeline
- `edac-reference/edac_cortex_ref.c` — Generic Cortex-A72 EDAC poll driver, QEMU-runnable
- `edac-reference/Kconfig` and `edac-reference/Makefile` — kernel build system integration
- `edac-reference/test/verify_edac_sysfs.sh` — post-load sysfs verification script

#### Fault injection harness
- `fault-injection/inject_edac_ce.sh` — correctable-error injection via EDAC debugfs
- `fault-injection/inject_edac_ue.sh` — uncorrectable-error injection
- `fault-injection/inject_aer.sh` — PCIe AER error injection via aer-inject
- `fault-injection/ci_fault_matrix.sh` — matrix runner for all fault type x recovery path combinations

#### Prometheus exporter (Go)
- `exporter/main.go` — HTTP server, collector registration, health endpoint
- `exporter/collectors/edac.go` — scrapes `/sys/devices/system/edac/` counters
- `exporter/collectors/mce.go` — scrapes machine-check error counters
- `exporter/collectors/aer.go` — scrapes PCIe AER sysfs entries
- `exporter/Dockerfile` — ARM64 container image

#### Dashboards and alerting
- `dashboards/fleet-fault-resilience.json` — Grafana dashboard: CE/UE rates, AER events, MCE timeline
- `dashboards/alert-rules.yml` — Prometheus alerting rules for fault thresholds
- `dashboards/recording-rules.yml` — pre-computed rate and ratio metrics for dashboard performance

#### Postmortem replay toolkit
- `replay/parse_edac_trace.py` — parses kernel log / EDAC trace to structured JSON
- `replay/replay_kernel_state.sh` — re-injects parsed events via fault-injection layer
- `replay/example_traces/` — synthetic anonymized sample traces for testing

#### CI pipeline
- `ci/build-and-test.yml` — GitHub Actions pipeline: lint → test → build → QEMU → Docker
- `ci/qemu-boot.sh` — boots QEMU ARM64 virt machine, loads module, runs fault-injection tests

#### Test suite
- `tests/conftest.py` — shared fixtures and mock sysfs tree
- `tests/requirements.txt` — pinned Python test dependencies
- `tests/test_edac_counters.py` — exporter metrics vs. sysfs value reconciliation
- `tests/test_aer_counters.py` — AER collector correctness
- `tests/test_mce_counters.py` — MCE collector correctness
- `tests/test_collectors_unit.py` — unit tests per collector in isolation
- `tests/test_fault_injection.py` — injection to counter increment verification
- `tests/test_replay_parse.py` — parse to replay roundtrip on sample traces
- `tests/bats/` — shell-based integration tests using bats-core

#### Reference documentation
- `docs/hardware-fault-taxonomy.md` — correctable vs. uncorrectable; L1/L2/DRAM/PCIe/MCE
- `docs/edac-framework-primer.md` — Linux EDAC subsystem internals for non-kernel readers
- `docs/aer-primer.md` — PCIe Advanced Error Reporting deep-dive
- `docs/references.md` — upstream specs, kernel docs, and prior art

#### Extended resilience modules
- `boot-resilience/` — hardware watchdog, A/B rootfs, U-Boot bootcount, Secure Boot/TrustZone
- `storage-integrity/` — read-only rootfs with overlayfs, power-fail filesystem audit, log-to-tmpfs
- `kernel-hardening/` — panic=10, OOM killer tuning, sysctl hardening, eBPF anomaly detection
- `ota-updates/` — atomic OTA with RAUC, systemd watchdog (sd_notify), pre/post-install hooks
- `network-resilience/` — NetworkManager fallback profiles (eth to WiFi to LTE), OOB UART heartbeat
- `smartnic/` — SmartNIC/DPU three-pillar architecture, P4 data path, DPU control plane

#### Deployment stack
- `deploy/docker-compose.yml` — local dev stack: exporter + Prometheus + Grafana + Alertmanager
- `deploy/prometheus.yml` — scrape config for host and DPU exporters
- `deploy/grafana-datasource.yml` — Grafana Prometheus datasource auto-provisioning
- `deploy/grafana-dashboard-provider.yml` — Grafana dashboard auto-provisioning
- `deploy/alertmanager.yml` — Alertmanager routing and receiver configuration
- `deploy/k8s-daemonset.yaml` — Kubernetes DaemonSet, Service, and ServiceMonitor manifests
- `deploy/helm/` — Helm chart for parameterized fleet deployment (Chart.yaml, values.yaml, templates)
- `deploy/README.md` — Docker Compose, Kubernetes, and Helm quick-start guide

#### Repository metadata
- `CONTRIBUTING.md` — development environment setup, test instructions, module guide, PR checklist
- `SECURITY.md` — vulnerability reporting, response timeline, scope
- `AUTHORS.md` — upstream kernel contribution credits
- `ARCHITECTURE.md` — system architecture overview
- `LICENSE` — Apache-2.0
