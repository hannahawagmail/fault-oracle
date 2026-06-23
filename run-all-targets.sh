#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# run-all-targets.sh — Run every Makefile target and report pass/fail/skip.
set -uo pipefail

export PATH="/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")" || exit 1

PASS=0 FAIL=0 SKIP=0
RED='\033[31m' GREEN='\033[32m' YELLOW='\033[33m' RESET='\033[0m'

run() {
    local target="$1"
    local skip_reason="${2:-}"
    if [[ -n "$skip_reason" ]]; then
        printf "  ${YELLOW}SKIP${RESET}  %-20s %s\n" "$target" "$skip_reason"
        SKIP=$((SKIP+1))
        return
    fi
    if make "$target" >/dev/null 2>&1; then
        printf "  ${GREEN}PASS${RESET}  %s\n" "$target"
        PASS=$((PASS+1))
    else
        printf "  ${RED}FAIL${RESET}  %s\n" "$target"
        FAIL=$((FAIL+1))
    fi
}

echo ""
echo "=== fault-oracle: Full Target Validation ==="
echo ""

# Testing
echo "── Testing ──"
run test
run test-go
run test-shell
run test-all

# Coverage
echo ""
echo "── Coverage ──"
run coverage-python
run coverage-go
run coverage-all

# Lint
echo ""
echo "── Lint ──"
run lint
run lint-shell
run lint-python
run lint-go

# Build
echo ""
echo "── Build ──"
run build
run build-local
run build-all-arch

# QEMU
echo ""
echo "── QEMU ──"
run qemu-boot
run qemu-boot-dry

# Fault Injection
echo ""
echo "── Fault Injection ──"
run inject-safe
if [[ "$(id -u)" -ne 0 ]]; then
    run inject-ce "requires root"
    run inject-ue "requires root"
    run inject-aer "requires root"
else
    run inject-ce
    run inject-ue
    run inject-aer
fi

# Replay
echo ""
echo "── Replay ──"
run replay
run replay-fast

# Dashboard (skip if docker not running)
echo ""
echo "── Dashboard ──"
if docker info >/dev/null 2>&1; then
    run dashboard-local
    run dashboard-status
    run dashboard-logs
    run dashboard-stop
else
    run dashboard-local "docker not running"
    run dashboard-status "docker not running"
    run dashboard-logs "docker not running"
    run dashboard-stop "docker not running"
fi

# Release & SBOM
echo ""
echo "── Release ──"
run release-snapshot
if command -v syft >/dev/null 2>&1; then
    run sbom
    run release
else
    run sbom "syft not installed"
    run release "requires sbom (syft not installed)"
fi

# Utilities
echo ""
echo "── Utilities ──"
run check-tools
run fmt
run pre-commit
run clean
run view-man
run install-man "requires sudo"
run install-tools "requires sudo/apt"
run replay-custom "requires LOG= argument"

# Summary
echo ""
echo "═══════════════════════════════════════════"
printf "  ${GREEN}PASS: %d${RESET}  ${RED}FAIL: %d${RESET}  ${YELLOW}SKIP: %d${RESET}\n" "$PASS" "$FAIL" "$SKIP"
echo "═══════════════════════════════════════════"
echo ""

[[ $FAIL -eq 0 ]] && exit 0 || exit 1
