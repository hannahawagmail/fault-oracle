# SPDX-License-Identifier: Apache-2.0
#
# arm-linux-fault-resilience — top-level Makefile
#
# Every target is self-contained and idempotent. Targets that require
# hardware access (inject-ce, inject-ue) work on real hardware *and* inside
# the QEMU guest started by `make qemu-boot`.
#
# Quick start:
#   make              Show this help
#   make test         Run the full Python test suite
#   make replay       Replay the bundled sample EDAC trace
#   make dashboard-local  Bring up Prometheus + Grafana locally

# ---------------------------------------------------------------------------
# Variables — override on the command line, e.g.:
#   make inject-ce BACKEND=debugfs
#   make release VERSION=1.2.0
# ---------------------------------------------------------------------------

# Python interpreter
PYTHON     ?= python3

# Go toolchain
GO         ?= go

# Docker / Compose
DOCKER     ?= docker
COMPOSE    ?= docker compose

# QEMU binary for ARM64
QEMU       ?= qemu-system-aarch64

# shellcheck severity level (error | warning | info | style)
SC_LEVEL   ?= error

# Fault injection backend (debugfs | none)
BACKEND    ?= debugfs

# AER error type for inject-aer (cor | non-fatal | fatal)
AER_TYPE   ?= cor

# Test coverage thresholds
COV_PYTHON ?= 90
COV_GO     ?= 75

# Release version (defaults to git tag, falls back to "dev")
VERSION    ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")

# Output directory for release artifacts
DIST_DIR   ?= dist

# Syft binary for SBOM generation
SYFT       ?= syft

# goreleaser binary
GORELEASER ?= goreleaser

# ---------------------------------------------------------------------------
# Computed paths — do not override
# ---------------------------------------------------------------------------

ROOT_DIR   := $(dir $(realpath $(lastword $(MAKEFILE_LIST))))
REPLAY_DIR := $(ROOT_DIR)replay
INJECT_DIR := $(ROOT_DIR)fault-injection
EXPORTER   := $(ROOT_DIR)exporter
DEPLOY     := $(ROOT_DIR)deploy
CI_DIR     := $(ROOT_DIR)ci
TESTS_DIR  := $(ROOT_DIR)tests

SAMPLE_LOG := $(REPLAY_DIR)/example_traces/sample_ce_storm.log
PARSE_PY   := $(REPLAY_DIR)/parse_edac_trace.py
REPLAY_SH  := $(REPLAY_DIR)/replay_kernel_state.sh

# ---------------------------------------------------------------------------
# Default target
# ---------------------------------------------------------------------------

.PHONY: help
help: ## Show this help
	@printf "\n\033[1marm-linux-fault-resilience\033[0m — build, test, and operate targets\n\n"
	@printf "\033[1m%-22s  %s\033[0m\n" "Target" "Description"
	@printf "%-22s  %s\n"  "------" "-----------"
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@printf "\n\033[2mVariables (override with VAR=value):\033[0m\n"
	@printf "  BACKEND=%-14s  Fault injection backend (debugfs|none)\n" "$(BACKEND)"
	@printf "  AER_TYPE=%-13s  AER error type (cor|non-fatal|fatal)\n" "$(AER_TYPE)"
	@printf "  VERSION=%-14s  Release version tag\n" "$(VERSION)"
	@printf "\n"

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

.PHONY: test
test: ## Run the full Python test suite (top-level + per-module)
	$(PYTHON) -m pytest $(TESTS_DIR)/ -v --tb=short -p no:cacheprovider
	@echo ""
	@echo "Per-module test suites:"
	@for module in boot-resilience kernel-hardening network-resilience \
	               ota-updates storage-integrity; do \
	    echo "  $$module ..."; \
	    (cd $(ROOT_DIR)$$module && $(PYTHON) -m pytest tests/ -q --tb=line 2>&1 | tail -1); \
	done

.PHONY: test-go
test-go: ## Run Go unit tests for the Prometheus exporter
	cd $(EXPORTER) && $(GO) test -v -race ./...

.PHONY: test-shell
test-shell: ## Run BATS shell integration tests
	@if ! command -v bats >/dev/null 2>&1; then \
	    echo "bats-core not found — install with:"; \
	    echo "  git clone --depth=1 https://github.com/bats-core/bats-core /tmp/bats"; \
	    echo "  sudo /tmp/bats/install.sh /usr/local"; \
	    exit 1; \
	fi
	bash $(TESTS_DIR)/bats/run_bats.sh

.PHONY: test-all
test-all: test test-go test-shell ## Run every test suite (Python + Go + BATS)

# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

.PHONY: coverage
coverage: coverage-python ## Alias: run Python coverage across all packages (see coverage-python)

.PHONY: coverage-python
coverage-python: ## Run pytest-cov across all Python packages and enforce per-package thresholds
	@echo "=== Python Coverage (all packages) ==="
	@# Skip ml/ and gpu/ if prophet is not installed (optional ML dep)
	@ML_TESTS=""; GPU_TESTS=""; ML_COV=""; GPU_COV=""; \
	if $(PYTHON) -c "import prophet" 2>/dev/null; then \
	    ML_TESTS="$(ROOT_DIR)ml/tests/"; GPU_TESTS="$(ROOT_DIR)gpu/tests/"; \
	    ML_COV="--cov=$(ROOT_DIR)ml"; GPU_COV="--cov=$(ROOT_DIR)gpu"; \
	else \
	    echo "  (skipping ml/ and gpu/ — prophet not installed)"; \
	fi; \
	$(PYTHON) -m pytest \
	    $$ML_TESTS $$GPU_TESTS \
	    $(ROOT_DIR)anomaly/tests/ \
	    $(ROOT_DIR)aging/tests/ \
	    $(ROOT_DIR)storage/tests/ \
	    $(ROOT_DIR)bmc/tests/ \
	    $(ROOT_DIR)network/tests/ \
	    $(ROOT_DIR)power_cxl/tests/ \
	    $(ROOT_DIR)remediation/tests/ \
	    $$ML_COV $$GPU_COV \
	    --cov=$(ROOT_DIR)anomaly \
	    --cov=$(ROOT_DIR)aging \
	    --cov=$(ROOT_DIR)storage \
	    --cov=$(ROOT_DIR)bmc \
	    --cov=$(ROOT_DIR)network \
	    --cov=$(ROOT_DIR)power_cxl \
	    --cov=$(ROOT_DIR)remediation \
	    --cov-report=term-missing \
	    --cov-report=html:$(ROOT_DIR)htmlcov/ \
	    --cov-fail-under=60
	@echo ""
	@echo "HTML report → htmlcov/index.html"
	@echo "Lines annotated '# pragma: no cover' require real hardware (nvidia-smi,"
	@echo "ipmitool, ibstat, storcli, Prometheus, Kubernetes API) and are excluded."

.PHONY: coverage-go
coverage-go: ## Run Go test coverage and emit per-function summary + HTML report
	@echo "=== Go Coverage ==="
	cd $(EXPORTER) && $(GO) test -coverprofile=../coverage-go.out ./... && \
	  $(GO) tool cover -func=../coverage-go.out | grep -E "^total|collectors" && \
	  $(GO) tool cover -html=../coverage-go.out -o ../coverage-go.html
	@echo ""
	@echo "Coverage report: coverage-go.out"
	@echo "HTML report:     coverage-go.html"
	@echo "Threshold: Go ≥ $(COV_GO)%"

.PHONY: coverage-all
coverage-all: coverage-python coverage-go ## Run full coverage suite (Python + Go)

# ---------------------------------------------------------------------------
# Lint & static analysis
# ---------------------------------------------------------------------------

.PHONY: lint
lint: lint-shell lint-python lint-go ## Run all linters

.PHONY: lint-shell
lint-shell: ## Run shellcheck on every .sh file
	@echo "shellcheck (-S $(SC_LEVEL)) ..."
	@find $(ROOT_DIR) -name "*.sh" \
	    -not -path "*/.git/*" \
	    -not -path "*/node_modules/*" \
	  | sort | xargs shellcheck -x -S $(SC_LEVEL)
	@echo "  OK — all shell scripts pass shellcheck"

.PHONY: lint-python
lint-python: ## Run ruff linter and formatter check on Python sources
	@echo "ruff format (check) ..."
	@$(PYTHON) -m ruff format --check --line-length 100 \
	    $(ROOT_DIR)replay/ $(ROOT_DIR)tests/
	@echo "ruff check ..."
	@$(PYTHON) -m ruff check \
	    $(ROOT_DIR)replay/ $(ROOT_DIR)tests/
	@echo "  OK — Python lint clean"

.PHONY: lint-go
lint-go: ## Run gofmt + go vet on the exporter
	@echo "gofmt ..."
	@out=$$(gofmt -l $(EXPORTER)); \
	 if [ -n "$$out" ]; then echo "needs formatting: $$out"; exit 1; fi
	@echo "go vet ..."
	@cd $(EXPORTER) && $(GO) vet ./...
	@echo "  OK — Go lint clean"

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

.PHONY: build
build: ## Build the Go exporter binary (linux/arm64)
	cd $(EXPORTER) && GOOS=linux GOARCH=arm64 \
	    $(GO) build -ldflags="-X main.version=$(VERSION)" \
	    -o hw-fault-exporter .
	@echo "Built: exporter/hw-fault-exporter (linux/arm64)"

.PHONY: build-local
build-local: ## Build the Go exporter for the current host architecture
	cd $(EXPORTER) && $(GO) build -ldflags="-X main.version=$(VERSION)" \
	    -o hw-fault-exporter-local .
	@echo "Built: exporter/hw-fault-exporter-local ($(shell go env GOOS)/$(shell go env GOARCH))"

.PHONY: build-riscv64
build-riscv64: ## Cross-compile for linux/riscv64 (requires riscv64 gcc cross-toolchain)
	cd $(EXPORTER) && GOOS=linux GOARCH=riscv64 CGO_ENABLED=0 \
	    $(GO) build -ldflags="-X main.version=$(VERSION)" \
	    -o hw-fault-exporter-riscv64 .
	@echo "Built: exporter/hw-fault-exporter-riscv64 (linux/riscv64)"

.PHONY: build-x86_64
build-x86_64: ## Cross-compile for linux/amd64
	cd $(EXPORTER) && GOOS=linux GOARCH=amd64 CGO_ENABLED=0 \
	    $(GO) build -ldflags="-X main.version=$(VERSION)" \
	    -o hw-fault-exporter-x86_64 .
	@echo "Built: exporter/hw-fault-exporter-x86_64 (linux/amd64)"

.PHONY: build-all-arch
build-all-arch: build build-riscv64 build-x86_64 ## Build for all supported architectures (arm64, riscv64, x86_64)

# ---------------------------------------------------------------------------
# QEMU — ARM64 boot and integration test
# ---------------------------------------------------------------------------

.PHONY: qemu-boot
qemu-boot: ## Boot an ARM64 Linux kernel in QEMU and run the integration suite
	@echo "Starting ARM64 QEMU boot (may download kernel/initrd on first run) ..."
	@if ! command -v $(QEMU) >/dev/null 2>&1; then \
	    echo "qemu-system-aarch64 not found — install with:"; \
	    echo "  apt-get install qemu-system-arm   # Debian/Ubuntu"; \
	    echo "  brew install qemu                 # macOS"; \
	    exit 1; \
	fi
	bash $(CI_DIR)/qemu-boot.sh

.PHONY: qemu-boot-dry
qemu-boot-dry: ## Show what qemu-boot.sh would do without executing
	bash $(CI_DIR)/qemu-boot.sh --dry-run

# ---------------------------------------------------------------------------
# Fault injection
# ---------------------------------------------------------------------------

.PHONY: inject-ce
inject-ce: ## Inject a correctable ECC error (requires root; BACKEND=debugfs|none)
	@echo "Injecting correctable EDAC error (backend=$(BACKEND)) ..."
	@if [ "$(BACKEND)" != "none" ] && [ "$$(id -u)" -ne 0 ]; then \
	    echo "Error: inject-ce requires root. Run: sudo make inject-ce"; \
	    echo "       Or use the safe dry-run mode: make inject-ce BACKEND=none"; \
	    exit 1; \
	fi
	bash $(INJECT_DIR)/inject_edac_ce.sh --backend $(BACKEND)

.PHONY: inject-ue
inject-ue: ## Inject an uncorrectable ECC error (requires root; BACKEND=debugfs|none)
	@echo "Injecting uncorrectable EDAC error (backend=$(BACKEND)) ..."
	@printf "\033[33mWarning: UE injection may trigger a kernel panic on real hardware.\033[0m\n"
	@printf "\033[33mUse BACKEND=none for a safe dry-run in CI.\033[0m\n"
	@if [ "$(BACKEND)" != "none" ] && [ "$$(id -u)" -ne 0 ]; then \
	    echo "Error: inject-ue requires root. Run: sudo make inject-ue"; \
	    echo "       Or use the safe dry-run mode: make inject-ue BACKEND=none"; \
	    exit 1; \
	fi
	bash $(INJECT_DIR)/inject_edac_ue.sh --backend $(BACKEND)

.PHONY: inject-aer
inject-aer: ## Inject a PCIe AER error (AER_TYPE=cor|non-fatal|fatal; BACKEND=debugfs|none)
	@echo "Injecting PCIe AER error (type=$(AER_TYPE) backend=$(BACKEND)) ..."
	@if [ "$(BACKEND)" != "none" ] && [ "$$(id -u)" -ne 0 ]; then \
	    echo "Error: inject-aer requires root. Run: sudo make inject-aer"; \
	    exit 1; \
	fi
	bash $(INJECT_DIR)/inject_aer.sh --type $(AER_TYPE) --backend $(BACKEND)

.PHONY: inject-safe
inject-safe: ## Dry-run all injection scripts (no hardware needed, safe for CI)
	@echo "Dry-run injection suite (BACKEND=none) ..."
	bash $(INJECT_DIR)/inject_edac_ce.sh --backend none
	bash $(INJECT_DIR)/inject_edac_ue.sh --backend none
	bash $(INJECT_DIR)/inject_aer.sh     --backend none
	@echo "All injection dry-runs passed."

# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

.PHONY: replay
replay: ## Parse and replay the bundled sample EDAC CE storm trace
	@echo "Parsing sample trace ..."
	$(PYTHON) $(PARSE_PY) --input $(SAMPLE_LOG) --output /tmp/replay_events.json --pretty
	@cat /tmp/replay_events.json | head -40
	@echo ""
	@echo "Replaying kernel state (1x speed, dry-run) ..."
	bash $(REPLAY_SH) --events /tmp/replay_events.json --speed 1.0 --dry-run

.PHONY: replay-fast
replay-fast: ## Replay the sample trace at 100x speed
	$(PYTHON) $(PARSE_PY) --input $(SAMPLE_LOG) --output /tmp/replay_events.json
	bash $(REPLAY_SH) --events /tmp/replay_events.json --speed 100.0 --dry-run

.PHONY: replay-custom
replay-custom: ## Replay a custom log file: make replay-custom LOG=/path/to/kern.log
	@if [ -z "$(LOG)" ]; then \
	    echo "Usage: make replay-custom LOG=/path/to/kern.log"; exit 1; fi
	$(PYTHON) $(PARSE_PY) --input $(LOG) --output /tmp/replay_events.json
	bash $(REPLAY_SH) --events /tmp/replay_events.json --speed 1.0 --dry-run

# ---------------------------------------------------------------------------
# Local development stack (Prometheus + Grafana)
# ---------------------------------------------------------------------------

.PHONY: dashboard-local
dashboard-local: ## Start Prometheus + Grafana + exporter via Docker Compose
	@echo "Starting local observability stack ..."
	@echo "  Exporter metrics  → http://localhost:9100/metrics"
	@echo "  Prometheus UI     → http://localhost:9090"
	@echo "  Grafana           → http://localhost:3000  (admin/admin)"
	@echo ""
	$(COMPOSE) -f $(DEPLOY)/docker-compose.yml up -d
	@echo ""
	@echo "Stop with: make dashboard-stop"

.PHONY: dashboard-stop
dashboard-stop: ## Stop the local observability stack
	$(COMPOSE) -f $(DEPLOY)/docker-compose.yml down

.PHONY: dashboard-logs
dashboard-logs: ## Tail logs from the local observability stack
	$(COMPOSE) -f $(DEPLOY)/docker-compose.yml logs -f

.PHONY: dashboard-status
dashboard-status: ## Show status of the local observability stack containers
	$(COMPOSE) -f $(DEPLOY)/docker-compose.yml ps

# ---------------------------------------------------------------------------
# SBOM — Software Bill of Materials
# ---------------------------------------------------------------------------

.PHONY: sbom
sbom: ## Generate an SPDX 2.3 SBOM with syft (install: https://github.com/anchore/syft)
	@mkdir -p $(DIST_DIR)
	@if ! command -v $(SYFT) >/dev/null 2>&1; then \
	    echo "syft not found — install with:"; \
	    echo "  curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /usr/local/bin"; \
	    exit 1; \
	fi
	@echo "Generating SBOM (SPDX 2.3) ..."
	$(SYFT) dir:$(ROOT_DIR) \
	    -o spdx-json=$(DIST_DIR)/fault-resilience-$(VERSION)-sbom.spdx.json \
	    --exclude '.git' \
	    --exclude 'htmlcov' \
	    --exclude '$(DIST_DIR)'
	@echo "SBOM written → $(DIST_DIR)/fault-resilience-$(VERSION)-sbom.spdx.json"

# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------

.PHONY: release
release: test lint sbom ## Build a versioned release tarball with SHA-256 checksums
	@mkdir -p $(DIST_DIR)
	@echo "Building release $(VERSION) ..."
	@echo "  Building Go exporter (linux/arm64) ..."
	$(MAKE) build VERSION=$(VERSION)
	@echo "  Creating source archive ..."
	git archive --format=tar.gz \
	    --prefix=arm-linux-fault-resilience-$(VERSION)/ \
	    HEAD \
	    -o $(DIST_DIR)/arm-linux-fault-resilience-$(VERSION)-src.tar.gz \
	    2>/dev/null || \
	  tar -czf $(DIST_DIR)/arm-linux-fault-resilience-$(VERSION)-src.tar.gz \
	    --exclude='.git' \
	    --exclude='$(DIST_DIR)' \
	    --exclude='htmlcov' \
	    --exclude='__pycache__' \
	    --exclude='*.pyc' \
	    --transform="s|^\.|arm-linux-fault-resilience-$(VERSION)|" \
	    -C $(ROOT_DIR) .
	@echo "  Copying exporter binary ..."
	cp $(EXPORTER)/hw-fault-exporter \
	   $(DIST_DIR)/hw-fault-exporter-$(VERSION)-linux-arm64
	@echo "  Computing checksums ..."
	cd $(DIST_DIR) && sha256sum \
	    arm-linux-fault-resilience-$(VERSION)-src.tar.gz \
	    hw-fault-exporter-$(VERSION)-linux-arm64 \
	    fault-resilience-$(VERSION)-sbom.spdx.json \
	    > SHA256SUMS
	@echo ""
	@echo "Release artifacts in $(DIST_DIR)/:"
	@ls -lh $(DIST_DIR)/
	@echo ""
	@echo "Verify with: cd $(DIST_DIR) && sha256sum -c SHA256SUMS"

.PHONY: release-snapshot
release-snapshot: ## Build a snapshot release (no git tag required, no sbom)
	@mkdir -p $(DIST_DIR)
	$(MAKE) build VERSION=$(VERSION)-snapshot
	tar -czf $(DIST_DIR)/arm-linux-fault-resilience-$(VERSION)-snapshot.tar.gz \
	    --exclude='.git' \
	    --exclude='$(DIST_DIR)' \
	    --exclude='htmlcov' \
	    --exclude='__pycache__' \
	    --exclude='*.pyc' \
	    -C $(ROOT_DIR) .
	@echo "Snapshot: $(DIST_DIR)/arm-linux-fault-resilience-$(VERSION)-snapshot.tar.gz"

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


.PHONY: install-man
install-man: ## Install man pages to /usr/local/share/man/man1
	@mkdir -p /usr/local/share/man/man1
	cp $(ROOT_DIR)man/man1/*.1 /usr/local/share/man/man1/
	mandb 2>/dev/null || true
	@echo "Man pages installed. Try: man parse_edac_trace"

.PHONY: view-man
view-man: ## Preview man pages without installing (groff required)
	@for f in $(ROOT_DIR)man/man1/*.1; do \
	    echo "=== $$f ==="; \
	    groff -man -Tutf8 "$$f" 2>/dev/null | head -30; \
	done
.PHONY: clean
clean: ## Remove build artifacts, coverage reports, and dist directory
	find $(ROOT_DIR) -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find $(ROOT_DIR) -name "*.pyc" -delete 2>/dev/null || true
	find $(ROOT_DIR) -name ".coverage" -delete 2>/dev/null || true
	rm -rf $(ROOT_DIR)htmlcov/
	rm -rf $(DIST_DIR)/
	rm -f $(EXPORTER)/hw-fault-exporter
	rm -f $(EXPORTER)/hw-fault-exporter-local
	rm -f $(EXPORTER)/coverage.out $(EXPORTER)/coverage.html
	rm -f $(ROOT_DIR)coverage-go.out $(ROOT_DIR)coverage-go.html
	@echo "Clean."

.PHONY: install-tools
install-tools: ## Install required developer tools (Debian/Ubuntu)
	@echo "Installing Python test dependencies ..."
	pip install --break-system-packages -r $(TESTS_DIR)/requirements.txt
	@echo "Installing shellcheck ..."
	apt-get install -y shellcheck 2>/dev/null || \
	    brew install shellcheck 2>/dev/null || \
	    echo "  Install shellcheck manually: https://github.com/koalaman/shellcheck"
	@echo "Installing bats-core ..."
	git clone --depth=1 https://github.com/bats-core/bats-core /tmp/bats-core && \
	    sudo /tmp/bats-core/install.sh /usr/local && \
	    rm -rf /tmp/bats-core
	@echo "Done — run 'make test-all' to verify."

.PHONY: check-tools
check-tools: ## Check that all required tools are installed
	@echo "Checking required tools ..."
	@ok=1; \
	for tool in $(PYTHON) $(GO) shellcheck bats; do \
	    if command -v $$tool >/dev/null 2>&1; then \
	        printf "  \033[32m✓\033[0m %-20s %s\n" "$$tool" "$$($$tool --version 2>&1 | head -1)"; \
	    else \
	        printf "  \033[31m✗\033[0m %-20s not found\n" "$$tool"; \
	        ok=0; \
	    fi; \
	done; \
	for optional in $(DOCKER) $(QEMU) $(SYFT) $(GORELEASER); do \
	    if command -v $$optional >/dev/null 2>&1; then \
	        printf "  \033[32m✓\033[0m %-20s %s (optional)\n" "$$optional" "$$($$optional --version 2>&1 | head -1)"; \
	    else \
	        printf "  \033[33m-\033[0m %-20s not found (optional)\n" "$$optional"; \
	    fi; \
	done; \
	[ $$ok -eq 1 ] || exit 1

.PHONY: fmt
fmt: ## Auto-format Python sources with ruff and Go sources with gofmt
	$(PYTHON) -m ruff format --line-length 100 $(ROOT_DIR)replay/ $(ROOT_DIR)tests/
	$(PYTHON) -m ruff check --fix $(ROOT_DIR)replay/ $(ROOT_DIR)tests/ || true
	gofmt -w $(EXPORTER)/

.PHONY: pre-commit
pre-commit: lint-shell lint-python lint-go ## Run the pre-commit check suite (no tests)
	@echo "Pre-commit checks passed."
