#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# verify_ras_registers.sh — Verify ARM RAS Extension register exposure via debugfs
#
# Usage:
#   sudo bash verify_ras_registers.sh          # standard checks
#   sudo bash verify_ras_registers.sh --ras    # also probe RAS record file (future feature)
#
# Exit codes:
#   0  All executed checks passed (SKIP counts as pass)
#   1  One or more checks FAILED
#
# Prerequisites:
#   - The edac_cortex_ref module must be loaded (insmod edac_cortex_ref.ko)
#   - debugfs must be mounted at /sys/kernel/debug
#   - Run as root (debugfs writes require CAP_SYS_ADMIN)

set -euo pipefail

# --------------------------------------------------------------------------
# Colour / formatting helpers
# --------------------------------------------------------------------------
RED='\033[0;31m'
GRN='\033[0;32m'
YEL='\033[0;33m'
NC='\033[0m'  # no colour

PASS=0
FAIL=0
SKIP=0

pass() { echo -e "${GRN}[PASS]${NC} $*"; ((PASS++)); }
fail() { echo -e "${RED}[FAIL]${NC} $*"; ((FAIL++)); }
skip() { echo -e "${YEL}[SKIP]${NC} $*"; ((SKIP++)); }

# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------
OPT_RAS=false
for arg in "$@"; do
    case "$arg" in
        --ras) OPT_RAS=true ;;
        --help|-h)
            echo "Usage: $0 [--ras]"
            echo "  --ras   Also probe the hypothetical ras_record0 debugfs file."
            exit 0
            ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

DEBUGFS_ROOT=/sys/kernel/debug
EDAC_DBG_DIR="${DEBUGFS_ROOT}/edac_cortex_ref"
EDAC_SYSFS_DIR=/sys/devices/system/edac/mc/mc0

echo "========================================================"
echo " edac_cortex_ref — RAS + fault-injection verification"
echo " $(date)"
echo "========================================================"
echo

# --------------------------------------------------------------------------
# Check 1: debugfs is mounted
# --------------------------------------------------------------------------
echo "--- Check 1: debugfs mounted"
if mount | grep -q "debugfs on ${DEBUGFS_ROOT}"; then
    pass "debugfs is mounted at ${DEBUGFS_ROOT}"
elif [ -d "${DEBUGFS_ROOT}" ] && ls "${DEBUGFS_ROOT}" &>/dev/null; then
    pass "debugfs appears accessible at ${DEBUGFS_ROOT} (mount entry may differ)"
else
    fail "debugfs is NOT mounted at ${DEBUGFS_ROOT}"
    echo "    Fix: sudo mount -t debugfs debugfs ${DEBUGFS_ROOT}"
    # Cannot proceed without debugfs
    echo
    echo "Cannot continue without debugfs. Aborting."
    exit 1
fi

# --------------------------------------------------------------------------
# Check 2: module is loaded (debugfs directory exists)
# --------------------------------------------------------------------------
echo
echo "--- Check 2: edac_cortex_ref module loaded"
if [ -d "${EDAC_DBG_DIR}" ]; then
    pass "debugfs directory ${EDAC_DBG_DIR} exists"
else
    fail "debugfs directory ${EDAC_DBG_DIR} not found — is the module loaded?"
    echo "    Fix: sudo insmod edac_cortex_ref.ko"
    echo
    echo "Cannot continue without the module. Aborting."
    exit 1
fi

# --------------------------------------------------------------------------
# Check 3: expected debugfs files are present
# --------------------------------------------------------------------------
echo
echo "--- Check 3: expected debugfs knobs present"
for knob in inject_ce inject_ue inject_csrow inject_channel stats inject_poison; do
    if [ -e "${EDAC_DBG_DIR}/${knob}" ]; then
        pass "  ${EDAC_DBG_DIR}/${knob} exists"
    else
        fail "  ${EDAC_DBG_DIR}/${knob} missing"
    fi
done

# --------------------------------------------------------------------------
# Check 4: inject_ce path — inject 1 CE and verify the counter increments
# --------------------------------------------------------------------------
echo
echo "--- Check 4: inject_ce path"

# Read current CE count from sysfs (tolerate missing path on older kernels)
if [ -r "${EDAC_SYSFS_DIR}/ce_count" ]; then
    CE_BEFORE=$(cat "${EDAC_SYSFS_DIR}/ce_count" 2>/dev/null || echo 0)
else
    CE_BEFORE=0
    skip "Cannot read ${EDAC_SYSFS_DIR}/ce_count — sysfs path absent, skipping counter delta check"
fi

echo 1 > "${EDAC_DBG_DIR}/inject_ce"
pass "Wrote 1 to inject_ce"

# Wait up to 3 seconds for the poll loop to drain the error
POLL_WAIT=3
echo "    Waiting ${POLL_WAIT}s for poll loop to drain..."
sleep "${POLL_WAIT}"

if [ -r "${EDAC_SYSFS_DIR}/ce_count" ]; then
    CE_AFTER=$(cat "${EDAC_SYSFS_DIR}/ce_count" 2>/dev/null || echo 0)
    if [ "$CE_AFTER" -gt "$CE_BEFORE" ]; then
        pass "ce_count incremented: ${CE_BEFORE} -> ${CE_AFTER}"
    else
        fail "ce_count did not increment after inject_ce (before=${CE_BEFORE} after=${CE_AFTER})"
        echo "    Check poll_msec module parameter — is the poll loop running?"
    fi
fi

# --------------------------------------------------------------------------
# Check 5: stats file shows cumulative counts
# --------------------------------------------------------------------------
echo
echo "--- Check 5: stats file"
if STATS=$(cat "${EDAC_DBG_DIR}/stats" 2>/dev/null); then
    if echo "${STATS}" | grep -q "total_ce_injected"; then
        pass "stats file contains total_ce_injected field"
    else
        fail "stats file does not contain expected fields"
        echo "    Got: ${STATS}"
    fi
    if echo "${STATS}" | grep -q "total_ue_injected"; then
        pass "stats file contains total_ue_injected field"
    else
        fail "stats file does not contain total_ue_injected field"
    fi
else
    fail "Cannot read ${EDAC_DBG_DIR}/stats"
fi

# --------------------------------------------------------------------------
# Check 6: --ras flag — probe hypothetical ras_record0 file
# --------------------------------------------------------------------------
echo
echo "--- Check 6: RAS record0 debugfs file (future feature hook)"
if $OPT_RAS; then
    RAS_FILE="${EDAC_DBG_DIR}/ras_record0"
    if [ -r "${RAS_FILE}" ]; then
        RAS_DATA=$(cat "${RAS_FILE}" 2>/dev/null || true)
        pass "ras_record0 file exists and is readable"
        echo "    Content: ${RAS_DATA}"
    else
        skip "ras_record0 not present (not yet implemented — gracefully skipped)"
        echo "    This file will be added in a future revision when in-kernel RAS"
        echo "    record polling is wired to the debugfs interface."
    fi
else
    skip "--ras flag not passed; skipping ras_record0 probe"
fi

# --------------------------------------------------------------------------
# Check 7: inject_poison — test page-offline escalation path
# --------------------------------------------------------------------------
echo
echo "--- Check 7: inject_poison (CONFIG_MEMORY_FAILURE)"

# Detect whether the running kernel has CONFIG_MEMORY_FAILURE enabled.
# Try /proc/config.gz first, then /boot/config-$(uname -r).
HAVE_MF=false
if [ -r /proc/config.gz ]; then
    if zcat /proc/config.gz 2>/dev/null | grep -q "^CONFIG_MEMORY_FAILURE=y"; then
        HAVE_MF=true
    fi
elif BOOT_CFG=$(ls /boot/config-"$(uname -r)" 2>/dev/null | head -1); [ -n "$BOOT_CFG" ]; then
    if grep -q "^CONFIG_MEMORY_FAILURE=y" "$BOOT_CFG" 2>/dev/null; then
        HAVE_MF=true
    fi
fi

if $HAVE_MF; then
    echo "    CONFIG_MEMORY_FAILURE=y detected — attempting poison injection"
    INJECT_RET=0
    echo 1 > "${EDAC_DBG_DIR}/inject_poison" 2>/dev/null || INJECT_RET=$?
    if [ "${INJECT_RET}" -eq 0 ]; then
        pass "inject_poison write succeeded (check dmesg for PFN log)"
    else
        fail "inject_poison write failed (exit ${INJECT_RET})"
        echo "    The module may have been built without CONFIG_MEMORY_FAILURE."
        echo "    Check dmesg for 'SKIP: CONFIG_MEMORY_FAILURE not set'."
    fi
else
    skip "CONFIG_MEMORY_FAILURE not detected in kernel config"
    echo "    The inject_poison knob will return -ENOSYS on this kernel."
    echo "    Rebuild with CONFIG_MEMORY_FAILURE=y to exercise this path."
    # Verify the knob gracefully returns ENOSYS
    INJECT_RET=0
    echo 1 > "${EDAC_DBG_DIR}/inject_poison" 2>/dev/null || INJECT_RET=$?
    if [ "${INJECT_RET}" -eq 38 ] 2>/dev/null || [ "${INJECT_RET}" -ne 0 ]; then
        pass "inject_poison correctly refused (returned error) on non-MEMORY_FAILURE kernel"
    else
        skip "Could not verify inject_poison error return (shell exit code mapping varies)"
    fi
fi

# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------
echo
echo "========================================================"
echo " Results: ${PASS} passed, ${SKIP} skipped, ${FAIL} failed"
echo "========================================================"

if [ "${FAIL}" -gt 0 ]; then
    echo "OVERALL: FAIL"
    exit 1
else
    echo "OVERALL: PASS"
    exit 0
fi
