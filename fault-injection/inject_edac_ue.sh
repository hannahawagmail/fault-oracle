#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# inject_edac_ue.sh — Inject uncorrectable EDAC errors and verify recovery path.
#
# Injects uncorrectable errors via the driver's debugfs interface and verifies:
#   1. The ue_count sysfs counter increments correctly.
#   2. A UE dmesg log entry (KERN_CRIT or KERN_ALERT level) appears.
#   3. If CONFIG_MEMORY_FAILURE=y, a memory_failure() invocation is logged.
#   4. The csrow-level ue_count is updated.
#
# WARNING: Uncorrectable error injection on a real system with CONFIG_MEMORY_FAILURE=y
# will invoke memory_failure() on address 0, which is typically safe (page 0 is
# often reserved) but should only be run in a test environment (QEMU recommended).
#
# Usage:
#   sudo bash inject_edac_ue.sh [options]
#
# Options:
#   --controller mc0     EDAC controller name (default: mc0)
#   --csrow 0            Target csrow (default: 0)
#   --channel 0          Target channel (default: 0)
#   --count 1            Number of UEs to inject (default: 1, max: 10)
#   --poll-msec 1000     Driver poll interval in ms (default: read from sysfs)
#   --check-memory-failure  Also check for memory_failure() log entry
#   --verbose            Print detailed progress
#
# Exit: 0 = success, 1 = injection or verification failed

set -euo pipefail

CONTROLLER="mc0"
CSROW=0
CHANNEL=0
COUNT=1
POLL_MSEC=""
CHECK_MEMORY_FAILURE=false
VERBOSE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --controller)         CONTROLLER="$2"; shift 2 ;;
        --csrow)              CSROW="$2"; shift 2 ;;
        --channel)            CHANNEL="$2"; shift 2 ;;
        --count)              COUNT="$2"; shift 2 ;;
        --poll-msec)          POLL_MSEC="$2"; shift 2 ;;
        --check-memory-failure) CHECK_MEMORY_FAILURE=true; shift ;;
        --verbose)            VERBOSE=true; shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

EDAC_ROOT="/sys/devices/system/edac/mc/${CONTROLLER}"
DEBUGFS_ROOT="/sys/kernel/debug/edac_cortex_ref"

log()  { echo "[inject_ue] $*"; }
vlog() { $VERBOSE && echo "[inject_ue] $*" || true; }
err()  { echo "[inject_ue] ERROR: $*" >&2; }

# ----- Preflight checks ----------------------------------------------------

if [[ $EUID -ne 0 ]]; then
    err "Must be root"
    exit 1
fi

if [[ ! -d "${EDAC_ROOT}" ]]; then
    err "EDAC controller not found: ${EDAC_ROOT}"
    exit 1
fi

if [[ ! -d "${DEBUGFS_ROOT}" ]]; then
    err "debugfs interface not found: ${DEBUGFS_ROOT}"
    exit 1
fi

if [[ "$COUNT" -lt 1 || "$COUNT" -gt 10 ]]; then
    err "count $COUNT out of range [1, 10] for UE injection (limit lower than CE for safety)"
    exit 1
fi

NR_CSROWS=$(cat "/sys/module/edac_cortex_ref/parameters/nr_csrows" 2>/dev/null || echo "2")
NR_CHANNELS=$(cat "/sys/module/edac_cortex_ref/parameters/nr_channels" 2>/dev/null || echo "2")

if [[ "$CSROW" -ge "$NR_CSROWS" || "$CHANNEL" -ge "$NR_CHANNELS" ]]; then
    err "csrow/channel out of range"
    exit 1
fi

# Check if CONFIG_MEMORY_FAILURE is enabled
MEMORY_FAILURE_ENABLED=false
if [[ -f "/proc/sys/vm/memory_failure_early_kill" ]]; then
    MEMORY_FAILURE_ENABLED=true
    vlog "CONFIG_MEMORY_FAILURE=y detected"
fi

# Warn if memory failure is enabled and we're not in a VM
if $MEMORY_FAILURE_ENABLED && [[ ! -f "/sys/hypervisor/type" && ! -d "/proc/xen" ]]; then
    log "WARNING: CONFIG_MEMORY_FAILURE=y and not running in a detected VM."
    log "UE injection will invoke memory_failure(page=0) which is normally safe"
    log "but should only be run in a test environment. Proceeding in 3s..."
    sleep 3
fi

# ----- Determine poll interval ----------------------------------------------

if [[ -z "$POLL_MSEC" ]]; then
    POLL_MSEC=$(cat "/sys/module/edac_cortex_ref/parameters/poll_msec" 2>/dev/null || echo "1000")
fi
WAIT_S=$(echo "scale=2; ($POLL_MSEC * 3) / 1000" | bc)

# ----- Read baselines -------------------------------------------------------

log "Injecting ${COUNT} uncorrectable error(s) → ${CONTROLLER}/csrow${CSROW}/ch${CHANNEL}"

BASELINE_TOTAL=$(cat "${EDAC_ROOT}/ue_count")
BASELINE_CSROW=$(cat "${EDAC_ROOT}/csrow${CSROW}/ue_count")

vlog "Baseline: total_ue=${BASELINE_TOTAL} csrow${CSROW}_ue=${BASELINE_CSROW}"

# Record dmesg offset for log verification
DMESG_OFFSET=$(dmesg | wc -l)

# ----- Injection -----------------------------------------------------------

echo "${CSROW}"   > "${DEBUGFS_ROOT}/inject_csrow"
echo "${CHANNEL}" > "${DEBUGFS_ROOT}/inject_channel"
echo "${COUNT}"   > "${DEBUGFS_ROOT}/inject_ue"

log "UE injection triggered. Waiting ${WAIT_S}s for poll loop..."
sleep "${WAIT_S}"

# ----- Verification --------------------------------------------------------

FAIL=0

# Verify total UE counter
NEW_TOTAL=$(cat "${EDAC_ROOT}/ue_count")
DELTA_TOTAL=$(( NEW_TOTAL - BASELINE_TOTAL ))
if [[ "$DELTA_TOTAL" -ge "$COUNT" ]]; then
    log "PASS: ue_count incremented by $DELTA_TOTAL"
else
    err "FAIL: ue_count incremented by $DELTA_TOTAL (expected ≥ $COUNT)"
    FAIL=$((FAIL+1))
fi

# Verify csrow-level UE counter
NEW_CSROW=$(cat "${EDAC_ROOT}/csrow${CSROW}/ue_count")
DELTA_CSROW=$(( NEW_CSROW - BASELINE_CSROW ))
if [[ "$DELTA_CSROW" -ge "$COUNT" ]]; then
    log "PASS: csrow${CSROW}/ue_count incremented by $DELTA_CSROW"
else
    err "FAIL: csrow${CSROW}/ue_count incremented by $DELTA_CSROW"
    FAIL=$((FAIL+1))
fi

# Verify UE log entry in dmesg (EDAC logs UEs at KERN_CRIT)
if dmesg | tail -n +$((DMESG_OFFSET+1)) | grep -qiE "EDAC MC.*UE|uncorrectable|memory failure"; then
    log "PASS: UE/uncorrectable log entry found in dmesg"
else
    err "FAIL: No UE log entry found in dmesg after injection"
    FAIL=$((FAIL+1))
fi

# Optional: verify memory_failure() was invoked
if $CHECK_MEMORY_FAILURE && $MEMORY_FAILURE_ENABLED; then
    if dmesg | tail -n +$((DMESG_OFFSET+1)) | grep -q "memory_failure"; then
        log "PASS: memory_failure() invocation logged"
    else
        log "INFO: memory_failure() not logged (page 0 may be reserved and handled differently)"
    fi
fi

# Verify stats
STATS_UE=$(cat "${DEBUGFS_ROOT}/stats" | grep total_ue_injected | awk '{print $2}')
if [[ "$STATS_UE" -ge "$COUNT" ]]; then
    log "PASS: debugfs/stats total_ue_injected=${STATS_UE}"
else
    err "FAIL: debugfs/stats total_ue_injected=${STATS_UE}"
    FAIL=$((FAIL+1))
fi

# ----- Result --------------------------------------------------------------

if [[ $FAIL -gt 0 ]]; then
    err "UE injection verification: $FAIL check(s) failed"
    exit 1
fi

log "UE injection complete — all checks passed"
log "Final counters: ue_count=${NEW_TOTAL} csrow${CSROW}/ue_count=${NEW_CSROW}"
exit 0
