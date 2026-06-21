#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# ci_fault_matrix.sh — Run the complete fault injection × recovery path CI matrix.
#
# Iterates the Cartesian product of:
#   Fault type:  CE (correctable), UE (uncorrectable), AER_correctable, AER_uncorrectable
#   Controller:  all mc<N> controllers present in sysfs
#   Target:      all csrow × channel combinations per controller
#
# For each combination:
#   1. Injects the specified fault type.
#   2. Waits for the poll interval.
#   3. Verifies the expected counter incremented.
#   4. Logs PASS/FAIL/SKIP with full context.
#
# CI integration: this script exits 0 only if all tested combinations pass.
# Skipped combinations (no hardware, no debugfs) do not count as failures.
#
# Usage:
#   sudo bash ci_fault_matrix.sh [options]
#
# Options:
#   --types CE,UE,AER_CE,AER_UE  Comma-separated fault types to test (default: CE,UE)
#   --controller mc0             Limit to one controller (default: all)
#   --poll-msec 1000             Override poll interval
#   --output-dir /tmp/results    Write per-test log files here (default: /tmp/fault-matrix)
#   --junit output.xml           Write JUnit XML report
#   --verbose                    Verbose injection output
#   --dry-run                    Print test plan without executing
#
# Exit: 0 = all tests passed (or all skipped), 1 = one or more failures

set -euo pipefail

FAULT_TYPES="CE,UE"
CONTROLLER_FILTER=""
POLL_MSEC=""
OUTPUT_DIR="/tmp/fault-matrix"
JUNIT_FILE=""
VERBOSE=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --types)       FAULT_TYPES="$2"; shift 2 ;;
        --controller)  CONTROLLER_FILTER="$2"; shift 2 ;;
        --poll-msec)   POLL_MSEC="$2"; shift 2 ;;
        --output-dir)  OUTPUT_DIR="$2"; shift 2 ;;
        --junit)       JUNIT_FILE="$2"; shift 2 ;;
        --verbose)     VERBOSE=true; shift ;;
        --dry-run)     DRY_RUN=true; shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EDAC_MC_ROOT="/sys/devices/system/edac/mc"

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
TOTAL_COUNT=0

declare -a JUNIT_CASES=()

mkdir -p "${OUTPUT_DIR}"

log()    { echo "[ci_matrix] $*" | tee -a "${OUTPUT_DIR}/matrix.log"; }
err()    { echo "[ci_matrix] FAIL: $*" | tee -a "${OUTPUT_DIR}/matrix.log" >&2; }
vlog()   { $VERBOSE && echo "[ci_matrix] $*" | tee -a "${OUTPUT_DIR}/matrix.log" || true; }

# ----- Enumerate controllers -----------------------------------------------

enumerate_controllers() {
    local controllers=()
    for mc_dir in "${EDAC_MC_ROOT}"/mc*; do
        [[ -d "$mc_dir" ]] || continue
        mc_name=$(basename "$mc_dir")
        if [[ -z "$CONTROLLER_FILTER" || "$mc_name" == "$CONTROLLER_FILTER" ]]; then
            controllers+=("$mc_name")
        fi
    done
    echo "${controllers[@]:-}"
}

# ----- Enumerate csrow × channel per controller ----------------------------

enumerate_csrow_channel() {
    local mc="$1"
    local mc_dir="${EDAC_MC_ROOT}/${mc}"
    local pairs=()
    for csrow_dir in "${mc_dir}"/csrow*; do
        [[ -d "$csrow_dir" ]] || continue
        csrow=$(basename "$csrow_dir" | sed 's/csrow//')
        for ch_file in "${csrow_dir}"/ch*_ce_count; do
            [[ -f "$ch_file" ]] || continue
            ch=$(basename "$ch_file" | sed 's/ch\([0-9]*\)_ce_count/\1/')
            pairs+=("${csrow}:${ch}")
        done
    done
    echo "${pairs[@]:-}"
}

# ----- Run one test case ---------------------------------------------------

run_test() {
    local fault_type="$1"
    local controller="$2"
    local csrow="$3"
    local channel="$4"
    local test_name="${fault_type}__${controller}__csrow${csrow}__ch${channel}"
    local log_file="${OUTPUT_DIR}/${test_name}.log"

    TOTAL_COUNT=$((TOTAL_COUNT+1))

    if $DRY_RUN; then
        log "DRY-RUN: $test_name"
        return
    fi

    vlog "Running: $test_name"

    local extra_args=""
    $VERBOSE && extra_args="--verbose"

    local exit_code=0
    case "$fault_type" in
        CE)
            bash "${SCRIPT_DIR}/inject_edac_ce.sh" \
                --controller "$controller" \
                --csrow "$csrow" \
                --channel "$channel" \
                --count 1 \
                ${POLL_MSEC:+--poll-msec "$POLL_MSEC"} \
                $extra_args \
                > "$log_file" 2>&1 || exit_code=$?
            ;;
        UE)
            bash "${SCRIPT_DIR}/inject_edac_ue.sh" \
                --controller "$controller" \
                --csrow "$csrow" \
                --channel "$channel" \
                --count 1 \
                ${POLL_MSEC:+--poll-msec "$POLL_MSEC"} \
                $extra_args \
                > "$log_file" 2>&1 || exit_code=$?
            ;;
        AER_CE)
            bash "${SCRIPT_DIR}/inject_aer.sh" \
                --type correctable \
                --error BadTLP \
                $extra_args \
                > "$log_file" 2>&1 || exit_code=$?
            # Exit code 2 = no AER devices = skip
            [[ $exit_code -eq 2 ]] && exit_code=77  # TAP skip code
            ;;
        AER_UE)
            bash "${SCRIPT_DIR}/inject_aer.sh" \
                --type nonfatal \
                --error CompletionTimeout \
                $extra_args \
                > "$log_file" 2>&1 || exit_code=$?
            [[ $exit_code -eq 2 ]] && exit_code=77
            ;;
        *)
            log "Unknown fault type: $fault_type"
            exit_code=1
            ;;
    esac

    case $exit_code in
        0)
            PASS_COUNT=$((PASS_COUNT+1))
            log "PASS: $test_name"
            JUNIT_CASES+=("<testcase name=\"${test_name}\" classname=\"FaultMatrix\" />")
            ;;
        77)
            SKIP_COUNT=$((SKIP_COUNT+1))
            log "SKIP: $test_name (no hardware/backend available)"
            JUNIT_CASES+=("<testcase name=\"${test_name}\" classname=\"FaultMatrix\"><skipped /></testcase>")
            ;;
        *)
            FAIL_COUNT=$((FAIL_COUNT+1))
            local failure_log
            failure_log=$(tail -5 "$log_file" 2>/dev/null || echo "(no log)")
            err "$test_name — last 5 lines of log:"
            echo "$failure_log" | while IFS= read -r line; do
                err "  $line"
            done
            JUNIT_CASES+=("<testcase name=\"${test_name}\" classname=\"FaultMatrix\"><failure message=\"exit code ${exit_code}\">$(cat "$log_file" 2>/dev/null | head -20)</failure></testcase>")
            ;;
    esac
}

# ----- Main matrix loop ----------------------------------------------------

log "=== Fault Injection CI Matrix ==="
log "Fault types: ${FAULT_TYPES}"
log "Output dir: ${OUTPUT_DIR}"
$DRY_RUN && log "(DRY RUN — no actual injection)"

IFS=',' read -ra TYPES <<< "$FAULT_TYPES"

# Get controller list
CONTROLLERS_STR=$(enumerate_controllers)
if [[ -z "$CONTROLLERS_STR" ]]; then
    log "No EDAC controllers found. Is edac_cortex_ref loaded?"
    log "Skipping EDAC tests. AER tests may still run."
    CONTROLLERS=()
else
    read -ra CONTROLLERS <<< "$CONTROLLERS_STR"
fi

for fault_type in "${TYPES[@]}"; do
    case "$fault_type" in
        CE|UE)
            # Iterate all controllers × csrow × channel
            if [[ ${#CONTROLLERS[@]} -eq 0 ]]; then
                log "SKIP: No EDAC controllers for fault type $fault_type"
                SKIP_COUNT=$((SKIP_COUNT+1))
                continue
            fi
            for mc in "${CONTROLLERS[@]}"; do
                PAIRS_STR=$(enumerate_csrow_channel "$mc")
                if [[ -z "$PAIRS_STR" ]]; then
                    log "SKIP: No csrow/channel pairs for $mc"
                    continue
                fi
                read -ra PAIRS <<< "$PAIRS_STR"
                for pair in "${PAIRS[@]}"; do
                    csrow="${pair%%:*}"
                    channel="${pair##*:}"
                    run_test "$fault_type" "$mc" "$csrow" "$channel"
                done
            done
            ;;
        AER_CE|AER_UE)
            # AER injection is device-global — run once per fault type
            run_test "$fault_type" "n/a" "0" "0"
            ;;
        *)
            log "Unknown fault type: $fault_type — skipping"
            SKIP_COUNT=$((SKIP_COUNT+1))
            ;;
    esac
done

# ----- JUnit XML output ----------------------------------------------------

if [[ -n "$JUNIT_FILE" ]]; then
    {
        echo '<?xml version="1.0" encoding="UTF-8"?>'
        echo "<testsuite name=\"FaultInjectionMatrix\" tests=\"${TOTAL_COUNT}\" failures=\"${FAIL_COUNT}\" skipped=\"${SKIP_COUNT}\">"
        for case in "${JUNIT_CASES[@]}"; do
            echo "  $case"
        done
        echo "</testsuite>"
    } > "$JUNIT_FILE"
    log "JUnit report written to: $JUNIT_FILE"
fi

# ----- Summary -------------------------------------------------------------

log ""
log "=== Matrix Results ==="
log "Total:   $TOTAL_COUNT"
log "Passed:  $PASS_COUNT"
log "Failed:  $FAIL_COUNT"
log "Skipped: $SKIP_COUNT"
log ""
log "Full logs in: ${OUTPUT_DIR}/"

if [[ $FAIL_COUNT -gt 0 ]]; then
    log "MATRIX RESULT: FAILED ($FAIL_COUNT failure(s))"
    exit 1
fi

log "MATRIX RESULT: PASSED"
exit 0
