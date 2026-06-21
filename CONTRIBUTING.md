<!-- SPDX-License-Identifier: Apache-2.0 -->
# Contributing to fault-oracle

Thank you for your interest in contributing. This repository is a reference implementation
for ARM Linux hardware fault resilience. Contributions that improve accuracy, expand coverage,
or fix bugs are welcome.

---

## Makefile Quick Reference

The top-level `Makefile` is the single entry point for all common developer tasks.
Run `make` (no arguments) to print the full target list with descriptions.

| Target | What it does |
|--------|-------------|
| `make test` | Run the full Python test suite across all modules |
| `make test-go` | Run Go unit tests for the Prometheus exporter |
| `make test-shell` | Run BATS shell integration tests |
| `make test-all` | Run every suite (Python + Go + BATS) |
| `make coverage` | Python coverage report; fails below 90% |
| `make lint` | shellcheck + black/flake8 + gofmt/vet |
| `make build` | Build the Go exporter for linux/arm64 |
| `make qemu-boot` | Boot an ARM64 kernel in QEMU and run the integration suite |
| `make inject-ce` | Inject a correctable ECC error (root required; `BACKEND=none` for dry-run) |
| `make inject-ue` | Inject an uncorrectable ECC error (root required; `BACKEND=none` for dry-run) |
| `make inject-aer` | Inject a PCIe AER error (`AER_TYPE=cor\|non-fatal\|fatal`) |
| `make inject-safe` | Dry-run all injection scripts — safe for CI, no hardware needed |
| `make replay` | Parse and replay the bundled sample EDAC CE storm trace |
| `make replay-custom LOG=…` | Replay a custom log file |
| `make dashboard-local` | Start Prometheus + Grafana locally via Docker Compose |
| `make dashboard-stop` | Stop the local observability stack |
| `make sbom` | Generate an SPDX 2.3 Software Bill of Materials with syft |
| `make release` | Build a versioned tarball + exporter binary + SHA-256 checksums |
| `make release-snapshot` | Quick snapshot build (no git tag required) |
| `make check-tools` | Check that all required tools are installed |
| `make install-tools` | Install required developer tools (Debian/Ubuntu) |
| `make fmt` | Auto-format Python (black) and Go (gofmt) sources |
| `make clean` | Remove build artifacts, coverage reports, and dist/ |

Variables you can override on the command line:

```bash
make inject-ce BACKEND=none          # dry-run, no hardware needed
make inject-aer AER_TYPE=non-fatal   # non-fatal PCIe error
make release VERSION=1.2.0           # pin release version
make lint SC_LEVEL=error             # stricter shellcheck
```

---

## Setting Up the Development Environment

### Required tools

| Tool | Minimum version | Purpose |
|------|----------------|---------|
| Python | 3.11 | Test suite, replay toolkit |
| Go | 1.21 | Prometheus exporter |
| gcc-aarch64-linux-gnu | any | ARM64 cross-compilation |
| Docker Engine + Compose v2 | 24.0 | Local dev stack |
| kubectl + Helm | 1.28 / 3.12 | Kubernetes deployment |
| shellcheck | 0.9 | Shell script static analysis |
| bats-core | 1.10 | Shell-based integration tests |

### Python environment

```bash
# Install test dependencies (pinned for reproducibility):
pip install -r tests/requirements.txt

# Verify:
python3 -m pytest --version
```

### Go environment

```bash
# Build the exporter:
cd exporter/
go build -o hw-fault-exporter .

# Run Go tests:
go test -v -race ./...

# Tidy modules after adding a dependency:
go mod tidy
```

### ARM64 cross-compiler

```bash
# Debian/Ubuntu:
sudo apt-get install -y gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu

# Verify:
aarch64-linux-gnu-gcc --version
```

---

## Running Tests

### Python unit tests

```bash
# Install dependencies then run the full suite:
pip install -r tests/requirements.txt
pytest tests/ -v --tb=short

# Run a specific module's tests:
cd boot-resilience && python3 -m pytest tests/ -v

# Run with coverage:
pytest tests/ -v --cov=replay --cov-report=term-missing
```

### Shell-based tests (BATS)

```bash
# Install bats-core if not present:
git clone --depth=1 https://github.com/bats-core/bats-core.git /tmp/bats-core
sudo /tmp/bats-core/install.sh /usr/local

# Run the BATS suite:
bash tests/bats/run_bats.sh

# Run in TAP format (for CI):
bash tests/bats/run_bats.sh --tap
```

### Shell script syntax check

```bash
# Quick syntax-only check (no hardware required):
find . -name "*.sh" -not -path "./.git/*" -exec bash -n {} \; -print

# Full static analysis with shellcheck:
find . -name "*.sh" -not -path "./.git/*" | xargs shellcheck -x -S warning
```

---

## Adding a New Module

Each module lives in its own top-level directory and must contain:

1. `README.md` — purpose, usage, and design decisions (SPDX header required in HTML comment)
2. At least one shell script implementing the feature
3. `tests/test_<module>.py` — pytest test file with SPDX header and meaningful test coverage

### Minimum test file structure

```python
# SPDX-License-Identifier: Apache-2.0
"""Tests for <module-name>."""

import pytest


def test_example():
    """Describe what this test verifies."""
    assert True  # Replace with real assertions
```

### Checklist for new modules

- [ ] `SPDX-License-Identifier: Apache-2.0` on all source files
- [ ] Shell scripts pass `bash -n` (syntax check) and `shellcheck -S warning`
- [ ] Shell scripts use `set -euo pipefail` at the top
- [ ] Python test file present with at least one non-trivial test
- [ ] `README.md` describes what the module does and why
- [ ] No references to proprietary systems, internal codenames, or production metrics

---

## Commit Message Convention

This repository uses [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/).

### Format

```
<type>(<scope>): <short summary>

[body — explains WHY, not what]

[footer: Refs: #<task-id>]
```

Rules:
- Subject line ≤ 72 characters, imperative mood ("add", not "adds" or "added"), no trailing period
- Body is optional for trivial changes; required when the *why* is not obvious from the diff
- Footer must include `Refs: #N` for every task ID this commit addresses
- Never mix two concerns in one commit — if you need "and also" in the subject, split the commit

### Types

| Type | When to use |
|------|------------|
| `feat` | New feature or new module |
| `fix` | Bug fix |
| `docs` | Documentation only changes |
| `test` | Adding or fixing tests |
| `ci` | CI pipeline changes |
| `refactor` | Code restructuring without behaviour change |
| `chore` | Dependency bumps, tooling changes |

### Scopes → directories

| Scope | Directory |
|-------|-----------|
| `edac` | `exporter/collectors/edac.go`, `exporter/collectors/apei.go` |
| `ebpf` | `ebpf/` |
| `ml` | `ml/` |
| `anomaly` | `anomaly/` |
| `aging` | `aging/` |
| `correlation` | `correlation/` |
| `annotations` | `annotations/` |
| `ras` | `ras/` |
| `otel` | `otel/` |
| `gpu` | `gpu/` |
| `storage` | `storage/` |
| `bmc` | `bmc/` |
| `network` | `network/` |
| `remediation` | `remediation/` |
| `cxl` | `power_cxl/`, `ebpf/` CXL extension |
| `deploy` | `deploy/` |
| `ci` | `ci/` |
| `docs` | `docs/` |
| `collectors` | `exporter/collectors/` when touching multiple collectors |

### Examples

```
feat(gpu): add NVIDIA XID error collector with ECC counters

Parses /proc/driver/nvidia/gpus/*/information and nvidia-smi ECC
output. Emits gpu_xid_error_total{gpu_index,xid,severity} and
gpu_ecc_sbe_total / gpu_ecc_dbe_total. Degrades gracefully when
no NVIDIA hardware is present (collector_up=0).

Refs: #114

---

fix(ebpf): add bounds check on mc_event lower_layer field access

BPF verifier rejects unbounded pointer arithmetic. Clamp
lower_layer read to struct size before ringbuf submit.

Refs: #151

---

docs(deploy): document resource requests and secrets posture

Refs: #148
```

---

## Code Standards

### Shell scripts

- First line: `# SPDX-License-Identifier: Apache-2.0`
- Second or third line: `set -euo pipefail`
- Use `shellcheck` directives only when the warning is genuinely inapplicable; comment why
- Avoid hardcoded paths — use variables with sensible defaults
- Print usage to stderr if required arguments are missing; exit 1

### Python

- First line: `# SPDX-License-Identifier: Apache-2.0`
- Formatted with `black --line-length 100`
- Linted with `flake8 --max-line-length=100`
- No `print()` in library code — use `logging`

### Go

- First line: `// SPDX-License-Identifier: Apache-2.0`
- Formatted with `gofmt`
- Passes `go vet ./...`
- Error paths must return errors, not silently swallow them

---

## Non-Proprietary Policy

- Do not include any code, configs, or data from employer systems
- Do not use internal project codenames or internal tool names
- Do not include production IP addresses, hostnames, credentials, or metrics
- All examples must be generic and runnable on standard open-source infrastructure

---

## Branch Naming

```
{type}/{track-id}-{slug}
```

Examples:
```
feat/f1-nvidia-xid-collector
fix/m1-ebpf-bounds-check
docs/l1-metric-reference
ci/n1-coverage-gates
test/n2-integration-harness
chore/m6-security-review-fixes
```

The track ID (e.g. `f1`, `g2`, `h3`) maps to the task board so reviewers can find context immediately.

---

## Atomic PR Rules

One PR = one logical concern. A PR is atomic when:

1. **Single responsibility** — the subject line has no "and also". If it does, split it.
2. **Self-contained** — every commit in the PR leaves tests passing. No "fix tests" follow-up commits.
3. **Co-located tests** — tests live in the same PR as the code they cover, not a separate follow-up.
4. **No refactor + feature** — if you need to refactor before adding a feature, land the refactor in a separate PR first.

Justified exceptions (must be noted in PR description):
- A shared dependency introduced alongside its first consumer (e.g. `arch.go` + `pmu.go`)
- Two BPF C files that share the same Makefile target and cannot be verified independently

---

## Coverage Thresholds

| Package | Go line coverage | Python line coverage |
|---------|-----------------|---------------------|
| `exporter/collectors/` | ≥ 80% | — |
| `exporter/` overall | ≥ 75% | — |
| `remediation/` | ≥ 70% | — |
| `ml/` | — | ≥ 85% |
| `anomaly/` | — | ≥ 85% |
| `aging/` | — | ≥ 85% |
| `correlation/` | — | ≥ 85% |
| `gpu/`, `storage/`, `bmc/`, `network/` | — | ≥ 70% |

Hardware-dependent code paths that cannot run in CI without physical hardware must be marked with `# coverage: ignore` (Python) or `// coverage:ignore` (Go) with a comment explaining why. Unmarked uncovered lines count against the threshold.

Run locally before opening a PR:
```bash
make coverage-go      # Go per-package report
make coverage-python  # pytest-cov with htmlcov/
```

---

## Pull Request Checklist

Before opening a PR, verify:

- [ ] Branch name follows `{type}/{track-id}-{slug}` convention
- [ ] Every commit is atomic — single concern, tests pass at each commit
- [ ] Commit messages follow Conventional Commits format with `Refs: #N` footer
- [ ] `bash -n` passes on all shell scripts
- [ ] `shellcheck -S warning` passes (or suppressions are explained)
- [ ] `set -euo pipefail` present in all shell scripts
- [ ] SPDX header present on all source files (`.sh`, `.py`, `.go`, `.yaml`, `.yml`)
- [ ] New Python code formatted with `black --line-length 100` and passes `ruff check`
- [ ] Go code passes `go vet ./...` and `gofmt`
- [ ] Coverage thresholds met (see table above)
- [ ] Tests co-located — no "I'll add tests in a follow-up" PRs
- [ ] No proprietary content (see Non-Proprietary Policy above)
- [ ] PR description filled out using `.github/pull_request_template.md`
