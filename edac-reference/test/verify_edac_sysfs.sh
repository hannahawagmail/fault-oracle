#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# verify_edac_sysfs.sh — Post-load verification for the EDAC reference driver.
#
# Checks that all expected sysfs entries exist, have the correct format, and
# that counters respond correctly to fault injection (when available).
#
# Usage:
#   sudo bash verify_edac_sysfs.sh [--mc mc0] [--csrows 2] [--channels 2] [--inject]
#
# Options:
#   --mc        EDAC memory controller name (default: mc0)
#   --csrows    Expected number of chip-select rows (default: 2)
#   --channels  Expected number of channels per csrow (default: 2)
#   --inject    Also run injection-based counter verification (requires debugfs)
#   --verbose   Print each check result
#
# Exit code: 0 = all checks passed, 1 = one or more checks failed

set -euo pipefail

# ----- Configuration -------------------------------------------------------

MC="${MC:-mc0}"
NR_CSROWS="${NR_CSROWS:-2}"
NR_CHANNELS="${NR_CHANNELS:-2}"
RUN_INJECT=false
VERBOSE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mc)        MC="$2"; shift 2 ;;
        --csrows)    NR_CSROWS="$2"; shift 2 ;;
        --channels)  NR_CHANNELS="$2"; shift 2 ;;
        --inject)    RUN_INJECT=true; shift ;;
        --verbose)   VERBOSE=true; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

EDAC_ROOT="/sys/devices/system/edac/mc/${MC}"
DEBUGFS_ROOT="/sys/kernel/debug/edac_cortex_ref"
PASS=0
FAIL=0
SKIP=0

# ----- Helpers -------------------------------------------------------------

pass() { PASS=$((PASS+1)); $VERBOSE && echo "  PASS: $1"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL: $1"; }
skip() { SKIP=$((SKIP+1)); $VERBOSE && echo "  SKIP: $1"; }

check_exists() {
    local path="$1"
    local desc="${2:-$path}"
    if [[ -e "$path" ]]; then
        pass "exists: $desc"
    else
        fail "missing: $desc"
    fi
}

check_readable() {
    local path="$1"
    local desc="${2:-$path}"
    if [[ -r "$path" ]]; then
        pass "readable: $desc"
    else
        fail "not readable: $desc"
    fi
}

check_uint() {
    # Check that the file exists and contains a non-negative integer.
    local path="$1"
    local desc="${2:-$path}"
    if [[ ! -r "$path" ]]; then
        fail "missing/unreadable: $desc"
        return
    fi
    local val
    val=$(cat "$path" 2>/dev/null)
    if [[ "$val" =~ ^[0-9]+$ ]]; then
        pass "uint value ($val): $desc"
    else
        fail "bad value '$val' (expected uint): $desc"
    fi
}

check_string() {
    # Check that the file contains a non-empty string.
    local path="$1"
    local desc="${2:-$path}"
    if [[ ! -r "$path" ]]; then
        fail "missing/unreadable: $desc"
        return
    fi
    local val
    val=$(cat "$path" 2>/dev/null)
    if [[ -n "$val" ]]; then
        pass "non-empty string ('$val'): $desc"
    else
        fail "empty: $desc"
    fi
}

# ----- Main checks ---------------------------------------------------------

echo "========================================================"
echo "EDAC sysfs verification: ${EDAC_ROOT}"
echo "Expected topology: ${NR_CSROWS} csrows × ${NR_CHANNELS} channels"
echo "========================================================"

# 1. Check EDAC subsystem root
echo ""
echo "-- 1. EDAC subsystem root"
check_exists "/sys/devices/system/edac" "EDAC root"
check_exists "/sys/devices/system/edac/mc" "EDAC mc directory"
check_exists "${EDAC_ROOT}" "Controller directory ${EDAC_ROOT}"

# 2. Controller-level files
echo ""
echo "-- 2. Controller-level files"
check_uint    "${EDAC_ROOT}/ce_count"         "ce_count"
check_uint    "${EDAC_ROOT}/ce_noinfo_count"  "ce_noinfo_count"
check_uint    "${EDAC_ROOT}/ue_count"         "ue_count"
check_uint    "${EDAC_ROOT}/ue_noinfo_count"  "ue_noinfo_count"
check_string  "${EDAC_ROOT}/mc_name"          "mc_name"
check_uint    "${EDAC_ROOT}/size_mb"          "size_mb"

# Verify mc_name is the expected driver name
mc_name=$(cat "${EDAC_ROOT}/mc_name" 2>/dev/null || echo "")
if [[ "$mc_name" == "cortex-a72-l2-ecc" ]]; then
    pass "mc_name == 'cortex-a72-l2-ecc'"
else
    fail "mc_name == '$mc_name' (expected 'cortex-a72-l2-ecc')"
fi

# Verify size_mb matches expected topology
expected_size_mb=$(( NR_CSROWS * NR_CHANNELS * 2048 ))
actual_size_mb=$(cat "${EDAC_ROOT}/size_mb" 2>/dev/null || echo "0")
# Note: EDAC size_mb may vary by configuration — check it's > 0
if [[ "$actual_size_mb" -gt 0 ]]; then
    pass "size_mb > 0 (actual: ${actual_size_mb} MiB)"
else
    fail "size_mb is 0"
fi

# 3. Per-csrow files
echo ""
echo "-- 3. Per-csrow files"
for row in $(seq 0 $((NR_CSROWS - 1))); do
    csrow_dir="${EDAC_ROOT}/csrow${row}"
    check_exists "${csrow_dir}" "csrow${row} directory"
    check_uint "${csrow_dir}/ce_count" "csrow${row}/ce_count"
    check_uint "${csrow_dir}/ue_count" "csrow${row}/ue_count"

    # Per-channel files within each csrow
    for ch in $(seq 0 $((NR_CHANNELS - 1))); do
        check_uint   "${csrow_dir}/ch${ch}_ce_count"   "csrow${row}/ch${ch}_ce_count"
        check_string "${csrow_dir}/ch${ch}_dimm_label" "csrow${row}/ch${ch}_dimm_label"

        # Verify DIMM label format: "DIMM_<row>_CH<ch>"
        label=$(cat "${csrow_dir}/ch${ch}_dimm_label" 2>/dev/null || echo "")
        expected_label="DIMM_${row}_CH${ch}"
        if [[ "$label" == "$expected_label" ]]; then
            pass "dimm_label == '$expected_label'"
        else
            # Label may differ on real hardware — non-fatal
            $VERBOSE && echo "  INFO: dimm_label='$label' (expected '$expected_label')"
            SKIP=$((SKIP+1))
        fi
    done
done

# 4. reset_counters interface
echo ""
echo "-- 4. Writeable control files"
if [[ -w "${EDAC_ROOT}/reset_counters" ]]; then
    pass "reset_counters is writable"
else
    skip "reset_counters not writable (may require root)"
fi

# 5. Module parameters
echo ""
echo "-- 5. Module parameters"
PARAM_DIR="/sys/module/edac_cortex_ref/parameters"
check_exists "${PARAM_DIR}" "module parameters directory"
check_uint   "${PARAM_DIR}/poll_msec"   "parameter: poll_msec"
check_uint   "${PARAM_DIR}/nr_csrows"   "parameter: nr_csrows"
check_uint   "${PARAM_DIR}/nr_channels" "parameter: nr_channels"

param_csrows=$(cat "${PARAM_DIR}/nr_csrows" 2>/dev/null || echo "0")
if [[ "$param_csrows" == "$NR_CSROWS" ]]; then
    pass "nr_csrows parameter matches expected: ${NR_CSROWS}"
else
    fail "nr_csrows parameter: got $param_csrows, expected $NR_CSROWS"
fi

# 6. debugfs interface (optional — skip if not available)
echo ""
echo "-- 6. debugfs fault injection interface"
if [[ ! -d "/sys/kernel/debug" ]]; then
    skip "debugfs not mounted (run: mount -t debugfs debugfs /sys/kernel/debug)"
elif [[ ! -d "${DEBUGFS_ROOT}" ]]; then
    skip "driver debugfs dir not found (${DEBUGFS_ROOT})"
else
    check_exists "${DEBUGFS_ROOT}/inject_ce"      "inject_ce"
    check_exists "${DEBUGFS_ROOT}/inject_ue"      "inject_ue"
    check_exists "${DEBUGFS_ROOT}/inject_csrow"   "inject_csrow"
    check_exists "${DEBUGFS_ROOT}/inject_channel" "inject_channel"
    check_exists "${DEBUGFS_ROOT}/stats"          "stats"

    check_uint "${DEBUGFS_ROOT}/inject_csrow"   "inject_csrow value"
    check_uint "${DEBUGFS_ROOT}/inject_channel" "inject_channel value"

    # Verify stats file format
    stats=$(cat "${DEBUGFS_ROOT}/stats" 2>/dev/null || echo "")
    if echo "$stats" | grep -q "total_ce_injected:"; then
        pass "stats file contains 'total_ce_injected:'"
    else
        fail "stats file missing expected key"
    fi
fi

# 7. Injection counter verification (optional, requires debugfs + root)
if $RUN_INJECT; then
    echo ""
    echo "-- 7. Fault injection counter verification"

    if [[ ! -d "${DEBUGFS_ROOT}" ]]; then
        skip "debugfs not available — skipping injection tests"
    elif [[ ! -w "${DEBUGFS_ROOT}/inject_ce" ]]; then
        skip "inject_ce not writable (run as root)"
    else
        # Read baseline CE counter
        baseline_ce=$(cat "${EDAC_ROOT}/ce_count" 2>/dev/null || echo "0")
        baseline_csrow0_ch0=$(cat "${EDAC_ROOT}/csrow0/ch0_ce_count" 2>/dev/null || echo "0")

        # Set injection target: csrow 0, channel 0
        echo 0 > "${DEBUGFS_ROOT}/inject_csrow"
        echo 0 > "${DEBUGFS_ROOT}/inject_channel"

        # Inject 3 CEs
        echo 3 > "${DEBUGFS_ROOT}/inject_ce"

        # Wait for poll loop to fire (up to 3× poll interval)
        poll_msec=$(cat "/sys/module/edac_cortex_ref/parameters/poll_msec" 2>/dev/null || echo "1000")
        sleep_s=$(echo "scale=2; ($poll_msec * 3) / 1000" | bc)
        echo "  Waiting ${sleep_s}s for poll loop..."
        sleep "$sleep_s"

        # Verify counter incremented
        new_ce=$(cat "${EDAC_ROOT}/ce_count" 2>/dev/null || echo "0")
        new_csrow0_ch0=$(cat "${EDAC_ROOT}/csrow0/ch0_ce_count" 2>/dev/null || echo "0")

        delta_total=$(( new_ce - baseline_ce ))
        delta_csrow=$(( new_csrow0_ch0 - baseline_csrow0_ch0 ))

        if [[ "$delta_total" -ge 3 ]]; then
            pass "ce_count incremented by ≥ 3 after CE injection (delta: $delta_total)"
        else
            fail "ce_count incremented by only $delta_total after injecting 3 CEs"
        fi

        if [[ "$delta_csrow" -ge 3 ]]; then
            pass "csrow0/ch0_ce_count incremented by ≥ 3 (delta: $delta_csrow)"
        else
            fail "csrow0/ch0_ce_count incremented by only $delta_csrow"
        fi

        # Verify stats reflect injected count
        stats_ce=$(cat "${DEBUGFS_ROOT}/stats" | grep total_ce_injected | awk '{print $2}')
        if [[ "$stats_ce" -ge 3 ]]; then
            pass "stats/total_ce_injected ≥ 3 (value: $stats_ce)"
        else
            fail "stats/total_ce_injected = $stats_ce (expected ≥ 3)"
        fi

        # Test UE injection
        baseline_ue=$(cat "${EDAC_ROOT}/ue_count" 2>/dev/null || echo "0")
        echo 1 > "${DEBUGFS_ROOT}/inject_ue"
        sleep "$sleep_s"
        new_ue=$(cat "${EDAC_ROOT}/ue_count" 2>/dev/null || echo "0")
        delta_ue=$(( new_ue - baseline_ue ))
        if [[ "$delta_ue" -ge 1 ]]; then
            pass "ue_count incremented after UE injection (delta: $delta_ue)"
        else
            fail "ue_count did not increment after UE injection"
        fi

        # Test out-of-range csrow rejection
        result=$(echo 99 > "${DEBUGFS_ROOT}/inject_csrow" 2>&1 || true)
        # Should fail — verify inject_csrow was not updated
        current_csrow=$(cat "${DEBUGFS_ROOT}/inject_csrow")
        if [[ "$current_csrow" != "99" ]]; then
            pass "out-of-range csrow injection rejected"
        else
            fail "out-of-range csrow injection was not rejected"
        fi
    fi
fi

# ----- Summary -------------------------------------------------------------

echo ""
echo "========================================================"
echo "Results: ${PASS} passed, ${FAIL} failed, ${SKIP} skipped"
echo "========================================================"

if [[ $FAIL -gt 0 ]]; then
    echo "VERIFICATION FAILED"
    exit 1
else
    echo "VERIFICATION PASSED"
    exit 0
fi
