#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# inject_edac_ce.sh — Inject correctable EDAC errors and verify counter response.
#
# Uses the edac_cortex_ref driver's debugfs interface to trigger correctable
# error events and verifies that:
#   1. The sysfs CE counter increments by the expected amount.
#   2. The correct csrow/channel counters are updated.
#   3. A CE log entry appears in dmesg.
#
# Usage:
#   sudo bash inject_edac_ce.sh [options]
#
# Options:
#   --controller mc0     EDAC controller name (default: mc0)
#   --csrow 0            Target csrow (default: 0)
#   --channel 0          Target channel (default: 0)
#   --count 1            Number of CEs to inject (default: 1)
#   --poll-msec 1000     Driver poll interval in ms (default: read from sysfs)
#   --no-verify          Skip counter verification (just inject)
#   --verbose            Print detailed progress
#
# Exit: 0 = success, 1 = injection or verification failed

set -euo pipefail

CONTROLLER="mc0"
CSROW=0
CHANNEL=0
COUNT=1
POLL_MSEC=""
VERIFY=true
VERBOSE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --controller) CONTROLLER="$2"; shift 2 ;;
        --csrow)      CSROW="$2"; shift 2 ;;
        --channel)    CHANNEL="$2"; shift 2 ;;
        --count)      COUNT="$2"; shift 2 ;;
        --poll-msec)  POLL_MSEC="$2"; shift 2 ;;
        --no-verify)  VERIFY=false; shift ;;
        --verbose)    VERBOSE=true; shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

EDAC_ROOT="/sys/devices/system/edac/mc/${CONTROLLER}"
DEBUGFS_ROOT="/sys/kernel/debug/edac_cortex_ref"

log()  { echo "[inject_ce] $*"; }
vlog() { $VERBOSE && echo "[inject_ce] $*" || true; }
err()  { echo "[inject_ce] ERROR: $*" >&2; }

# ----- Preflight checks ----------------------------------------------------

if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root (sysfs write requires CAP_SYS_ADMIN)"
    exit 1
fi

if [[ ! -d "${EDAC_ROOT}" ]]; then
    err "EDAC controller not found: ${EDAC_ROOT}"
    err "Is the edac_cortex_ref module loaded? (sudo insmod edac_cortex_ref.ko)"
    exit 1
fi

if [[ ! -d "${DEBUGFS_ROOT}" ]]; then
    err "Driver debugfs interface not found: ${DEBUGFS_ROOT}"
    err "Is debugfs mounted? (mount -t debugfs debugfs /sys/kernel/debug)"
    exit 1
fi

if [[ ! -w "${DEBUGFS_ROOT}/inject_ce" ]]; then
    err "inject_ce not writable. Ensure module is loaded and you are root."
    exit 1
fi

# Validate csrow and channel bounds
NR_CSROWS=$(cat "/sys/module/edac_cortex_ref/parameters/nr_csrows" 2>/dev/null || echo "2")
NR_CHANNELS=$(cat "/sys/module/edac_cortex_ref/parameters/nr_channels" 2>/dev/null || echo "2")

if [[ "$CSROW" -ge "$NR_CSROWS" ]]; then
    err "csrow $CSROW out of range (nr_csrows=$NR_CSROWS)"
    exit 1
fi

if [[ "$CHANNEL" -ge "$NR_CHANNELS" ]]; then
    err "channel $CHANNEL out of range (nr_channels=$NR_CHANNELS)"
    exit 1
fi

if [[ "$COUNT" -lt 1 || "$COUNT" -gt 1000 ]]; then
    err "count $COUNT out of range [1, 1000]"
    exit 1
fi

# Determine poll interval
if [[ -z "$POLL_MSEC" ]]; then
    POLL_MSEC=$(cat "/sys/module/edac_cortex_ref/parameters/poll_msec" 2>/dev/null || echo "1000")
fi
WAIT_S=$(echo "scale=2; ($POLL_MSEC * 3) / 1000" | bc)

# ----- Read baselines -------------------------------------------------------

log "Injecting ${COUNT} correctable error(s) → ${CONTROLLER}/csrow${CSROW}/ch${CHANNEL}"
vlog "EDAC root: ${EDAC_ROOT}"
vlog "Wait after injection: ${WAIT_S}s (3× poll interval of ${POLL_MSEC}ms)"

BASELINE_TOTAL=$(cat "${EDAC_ROOT}/ce_count")
BASELINE_CSROW=$(cat "${EDAC_ROOT}/csrow${CSROW}/ce_count")
BASELINE_CH=$(cat "${EDAC_ROOT}/csrow${CSROW}/ch${CHANNEL}_ce_count")

vlog "Baselines: total_ce=${BASELINE_TOTAL} csrow${CSROW}_ce=${BASELINE_CSROW} ch${CHANNEL}_ce=${BASELINE_CH}"

# ----- Injection -----------------------------------------------------------

# Set injection target
echo "${CSROW}"   > "${DEBUGFS_ROOT}/inject_csrow"
echo "${CHANNEL}" > "${DEBUGFS_ROOT}/inject_channel"

vlog "Set inject_csrow=${CSROW} inject_channel=${CHANNEL}"

# Record dmesg tail offset for log verification
DMESG_OFFSET=$(dmesg | wc -l)

# Trigger injection
echo "${COUNT}" > "${DEBUGFS_ROOT}/inject_ce"
log "Injection triggered. Waiting ${WAIT_S}s for poll loop..."

sleep "${WAIT_S}"

# ----- Verification --------------------------------------------------------

if ! $VERIFY; then
    log "Skipping verification (--no-verify)"
    exit 0
fi

FAIL=0

# Verify total CE counter
NEW_TOTAL=$(cat "${EDAC_ROOT}/ce_count")
DELTA_TOTAL=$(( NEW_TOTAL - BASELINE_TOTAL ))
if [[ "$DELTA_TOTAL" -ge "$COUNT" ]]; then
    log "PASS: ce_count incremented by $DELTA_TOTAL (expected ≥ $COUNT)"
else
    err "FAIL: ce_count incremented by $DELTA_TOTAL (expected ≥ $COUNT)"
    FAIL=$((FAIL+1))
fi

# Verify csrow-level CE counter
NEW_CSROW=$(cat "${EDAC_ROOT}/csrow${CSROW}/ce_count")
DELTA_CSROW=$(( NEW_CSROW - BASELINE_CSROW ))
if [[ "$DELTA_CSROW" -ge "$COUNT" ]]; then
    log "PASS: csrow${CSROW}/ce_count incremented by $DELTA_CSROW"
else
    err "FAIL: csrow${CSROW}/ce_count incremented by $DELTA_CSROW (expected ≥ $COUNT)"
    FAIL=$((FAIL+1))
fi

# Verify channel-level CE counter
NEW_CH=$(cat "${EDAC_ROOT}/csrow${CSROW}/ch${CHANNEL}_ce_count")
DELTA_CH=$(( NEW_CH - BASELINE_CH ))
if [[ "$DELTA_CH" -ge "$COUNT" ]]; then
    log "PASS: csrow${CSROW}/ch${CHANNEL}_ce_count incremented by $DELTA_CH"
else
    err "FAIL: csrow${CSROW}/ch${CHANNEL}_ce_count incremented by $DELTA_CH (expected ≥ $COUNT)"
    FAIL=$((FAIL+1))
fi

# Verify dmesg log entry
# Derive the dmesg controller token from $CONTROLLER (e.g. mc1 -> EDAC MC1)
# so injecting into a non-default controller is verified correctly.
DMESG_MC="EDAC ${CONTROLLER^^}"
if dmesg | tail -n +$((DMESG_OFFSET+1)) | grep -q "${DMESG_MC}.*CE"; then
    log "PASS: CE log entry found in dmesg"
else
    err "FAIL: No CE log entry found in dmesg after injection"
    FAIL=$((FAIL+1))
fi

# Verify stats file
STATS_CE=$(cat "${DEBUGFS_ROOT}/stats" | grep total_ce_injected | awk '{print $2}')
if [[ "$STATS_CE" -ge "$COUNT" ]]; then
    log "PASS: debugfs/stats total_ce_injected=${STATS_CE}"
else
    err "FAIL: debugfs/stats total_ce_injected=${STATS_CE} (expected ≥ $COUNT)"
    FAIL=$((FAIL+1))
fi

# ----- Result --------------------------------------------------------------

if [[ $FAIL -gt 0 ]]; then
    err "CE injection verification: $FAIL check(s) failed"
    exit 1
fi

log "CE injection complete — all checks passed"
log "Final counters: ce_count=${NEW_TOTAL} csrow${CSROW}/ce_count=${NEW_CSROW} ch${CHANNEL}_ce_count=${NEW_CH}"
exit 0
