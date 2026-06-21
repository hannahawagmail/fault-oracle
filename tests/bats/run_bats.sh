#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# run_bats.sh — Run all BATS test suites
#
# Usage: bash tests/bats/run_bats.sh [--tap] [--pretty]
#
# Requires bats-core: https://github.com/bats-core/bats-core
# Install: git clone https://github.com/bats-core/bats-core.git && ./bats-core/install.sh /usr/local

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORMATTER="pretty"

# ---------------------------------------------------------------------------
# Parse flags
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tap)    FORMATTER="tap"; shift ;;
        --pretty) FORMATTER="pretty"; shift ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Usage: $0 [--tap] [--pretty]" >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Check for bats binary
# ---------------------------------------------------------------------------

if ! command -v bats &>/dev/null; then
    echo ""
    echo "ERROR: 'bats' not found in PATH."
    echo ""
    echo "Install bats-core with one of the following methods:"
    echo ""
    echo "  # Option 1 — git clone + install script (recommended)"
    echo "  git clone https://github.com/bats-core/bats-core.git /tmp/bats-core"
    echo "  sudo /tmp/bats-core/install.sh /usr/local"
    echo ""
    echo "  # Option 2 — Homebrew (macOS)"
    echo "  brew install bats-core"
    echo ""
    echo "  # Option 3 — apt (Debian/Ubuntu)"
    echo "  sudo apt-get install -y bats"
    echo ""
    exit 2
fi

BATS_VERSION="$(bats --version 2>/dev/null || echo 'unknown')"
echo "=== BATS Test Runner ==="
echo "bats version: $BATS_VERSION"
echo "formatter:    $FORMATTER"
echo "suite dir:    $SCRIPT_DIR"
echo ""

# ---------------------------------------------------------------------------
# Collect all .bats files
# ---------------------------------------------------------------------------

mapfile -t BATS_FILES < <(find "$SCRIPT_DIR" -maxdepth 1 -name "*.bats" | sort)

if [[ ${#BATS_FILES[@]} -eq 0 ]]; then
    echo "No .bats files found in $SCRIPT_DIR"
    exit 0
fi

echo "Found ${#BATS_FILES[@]} test suite(s):"
for f in "${BATS_FILES[@]}"; do
    echo "  - $(basename "$f")"
done
echo ""

# ---------------------------------------------------------------------------
# Run each suite and collect results
# ---------------------------------------------------------------------------

PASS_SUITES=0
FAIL_SUITES=0
TOTAL_SUITES=${#BATS_FILES[@]}

for bats_file in "${BATS_FILES[@]}"; do
    suite_name="$(basename "$bats_file" .bats)"
    echo "--- Suite: $suite_name ---"

    exit_code=0
    if [[ "$FORMATTER" == "tap" ]]; then
        bats --formatter tap "$bats_file" || exit_code=$?
    else
        bats --formatter pretty "$bats_file" || exit_code=$?
    fi

    if [[ $exit_code -eq 0 ]]; then
        PASS_SUITES=$((PASS_SUITES + 1))
        echo "  [PASS] $suite_name"
    else
        FAIL_SUITES=$((FAIL_SUITES + 1))
        echo "  [FAIL] $suite_name (exit code $exit_code)"
    fi
    echo ""
done

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo "=== BATS Run Summary ==="
echo "Total suites:  $TOTAL_SUITES"
echo "Passed:        $PASS_SUITES"
echo "Failed:        $FAIL_SUITES"
echo ""

if [[ $FAIL_SUITES -gt 0 ]]; then
    echo "RESULT: FAILED ($FAIL_SUITES suite(s) failed)"
    exit 1
fi

echo "RESULT: PASSED (all $TOTAL_SUITES suite(s) passed)"
exit 0
