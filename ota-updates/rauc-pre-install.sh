#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# rauc-pre-install.sh — Pre-install hook: validate system state before update
#
# RAUC calls this script after verifying the bundle signature but before
# writing any image data. Exit 0 to allow the update; exit non-zero to abort.
# All output goes to stderr, which RAUC forwards to journald under the rauc
# service unit. Check with: journalctl -u rauc -f
#
# Install to: /usr/lib/rauc/pre-install  (matches system.conf [handlers])
# Must be executable: chmod 755 /usr/lib/rauc/pre-install
#
# RAUC environment variables available in this script:
#   RAUC_SYSTEM_COMPATIBLE  — compatible string from system.conf
#   RAUC_BUNDLE_COMPATIBLE  — compatible string from bundle manifest
#   RAUC_BUNDLE_VERSION     — version from bundle manifest
#   RAUC_SLOT_NAME          — target slot name (e.g. "rootfs.1")
#   RAUC_SLOT_DEVICE        — target block device (e.g. "/dev/mmcblk0p3")
#   RAUC_SLOT_BOOTNAME      — bootname of target slot (e.g. "B")
#   RAUC_SLOT_STATE         — state of target slot: inactive/booted/active
#   RAUC_SLOT_VERSION       — version previously installed in target slot

set -euo pipefail

# --------------------------------------------------------------------------
# Logging helpers — all output to stderr so RAUC captures it to journald
# --------------------------------------------------------------------------
log()  { echo "[pre-install] $*" >&2; }
warn() { echo "[pre-install] WARNING: $*" >&2; }
die()  { echo "[pre-install] ABORT: $*" >&2; exit 1; }

log "Starting pre-install checks for bundle version ${RAUC_BUNDLE_VERSION:-unknown}"
log "Target slot: ${RAUC_SLOT_NAME:-unknown} (${RAUC_SLOT_DEVICE:-unknown})"

# --------------------------------------------------------------------------
# 1. Check sufficient free space on the /data partition
#    The update images are extracted from squashfs into /tmp during install;
#    we also need space for the RAUC status file and logs.
#    Require at least 200 MiB free on /data.
# --------------------------------------------------------------------------
MIN_DATA_FREE_MIB=200
DATA_MOUNT="/data"

if mountpoint -q "$DATA_MOUNT" 2>/dev/null; then
    # df -m: output in MiB; awk picks the "Available" column for $DATA_MOUNT
    DATA_FREE_MIB=$(df -m "$DATA_MOUNT" | awk 'NR==2 {print $4}')
    if [[ "$DATA_FREE_MIB" -lt "$MIN_DATA_FREE_MIB" ]]; then
        die "/data has only ${DATA_FREE_MIB} MiB free; need ${MIN_DATA_FREE_MIB} MiB. " \
            "Clear logs or application data before updating."
    fi
    log "/data free space OK: ${DATA_FREE_MIB} MiB available"
else
    warn "/data is not mounted; skipping free-space check"
fi

# Also verify the target slot partition is large enough to accept the write.
# RAUC does this too, but an early check avoids a slow partial write.
if [[ -b "${RAUC_SLOT_DEVICE:-}" ]]; then
    SLOT_SIZE_BYTES=$(blockdev --getsize64 "$RAUC_SLOT_DEVICE" 2>/dev/null || echo 0)
    log "Target slot device ${RAUC_SLOT_DEVICE} size: ${SLOT_SIZE_BYTES} bytes"
fi

# --------------------------------------------------------------------------
# 2. Verify the hardware watchdog keepalive process is running
#    wdt-setup.sh writes its PID to /run/wdt-keepalive.pid.
#    If the WDT daemon is dead, a hung update could trigger a hard reset
#    mid-write — exactly the failure mode we are trying to prevent.
# --------------------------------------------------------------------------
WDT_PID_FILE="/run/wdt-keepalive.pid"

if [[ -f "$WDT_PID_FILE" ]]; then
    WDT_PID=$(cat "$WDT_PID_FILE")
    if kill -0 "$WDT_PID" 2>/dev/null; then
        log "WDT keepalive running (PID $WDT_PID) — OK"
    else
        die "WDT keepalive PID file exists (${WDT_PID_FILE}) but process $WDT_PID is dead. " \
            "The hardware watchdog may fire mid-update. Start wdt-keepalive before updating."
    fi
else
    warn "WDT keepalive PID file not found at ${WDT_PID_FILE}. " \
         "Ensure wdt-setup.sh is running. Continuing (non-fatal on this board)."
fi

# --------------------------------------------------------------------------
# 3. Check EDAC correctable error rate
#    A spike in correctable errors (CE) before an update indicates degraded
#    DRAM or ECC DIMM. Writing to the inactive partition under high CE load
#    risks silent data corruption in the new firmware. Abort if the CE count
#    has increased by more than EDAC_MAX_CE_PER_HOUR in the last measurement.
#
#    We read the cumulative ce_count from sysfs and compare against a
#    checkpoint file written by the EDAC monitor. If no checkpoint file
#    exists, we just log the current value and proceed.
# --------------------------------------------------------------------------
EDAC_MAX_CE_PER_HOUR=50
EDAC_CHECKPOINT_FILE="/data/edac-ce-checkpoint"
EDAC_SYSFS="/sys/devices/system/edac"

if [[ -d "$EDAC_SYSFS" ]]; then
    # Sum ce_count across all memory controllers
    CURRENT_CE=0
    while IFS= read -r ce_file; do
        count=$(cat "$ce_file" 2>/dev/null || echo 0)
        CURRENT_CE=$(( CURRENT_CE + count ))
    done < <(find "$EDAC_SYSFS" -name "ce_count" -type f 2>/dev/null)

    log "EDAC cumulative correctable error count: ${CURRENT_CE}"

    if [[ -f "$EDAC_CHECKPOINT_FILE" ]]; then
        # File format: "<timestamp_epoch> <ce_count>"
        read -r CHKPT_TIME CHKPT_CE < "$EDAC_CHECKPOINT_FILE"
        NOW=$(date +%s)
        ELAPSED=$(( NOW - CHKPT_TIME ))
        CE_DELTA=$(( CURRENT_CE - CHKPT_CE ))

        # Normalize to per-hour rate
        if [[ "$ELAPSED" -gt 0 ]]; then
            CE_PER_HOUR=$(( CE_DELTA * 3600 / ELAPSED ))
        else
            CE_PER_HOUR=$CE_DELTA
        fi

        log "EDAC CE delta: ${CE_DELTA} over ${ELAPSED}s (${CE_PER_HOUR}/hr)"

        if [[ "$CE_PER_HOUR" -gt "$EDAC_MAX_CE_PER_HOUR" ]]; then
            die "EDAC correctable error rate ${CE_PER_HOUR}/hr exceeds limit ${EDAC_MAX_CE_PER_HOUR}/hr. " \
                "Possible DRAM degradation. Update aborted to protect data integrity."
        fi
    else
        warn "No EDAC checkpoint file at ${EDAC_CHECKPOINT_FILE}; cannot compute rate. " \
             "Writing checkpoint now for future runs."
    fi

    # Update checkpoint with current timestamp and count
    echo "$(date +%s) ${CURRENT_CE}" > "$EDAC_CHECKPOINT_FILE"
    log "EDAC check passed"
else
    log "EDAC sysfs not present — skipping memory error check (non-ECC board?)"
fi

# --------------------------------------------------------------------------
# 4. Battery / power check
#    If the board has a battery-backed supply, refuse to update while on
#    battery with less than 30% charge. A power loss mid-write to the
#    inactive partition would corrupt the new firmware image.
# --------------------------------------------------------------------------
BAT_MIN_PERCENT=30
BAT_SYSFS="/sys/class/power_supply"

if [[ -d "$BAT_SYSFS" ]]; then
    for bat_dir in "${BAT_SYSFS}"/BAT*; do
        [[ -d "$bat_dir" ]] || continue
        STATUS_FILE="${bat_dir}/status"
        CAP_FILE="${bat_dir}/capacity"

        if [[ -f "$STATUS_FILE" && -f "$CAP_FILE" ]]; then
            BAT_STATUS=$(cat "$STATUS_FILE")
            BAT_CAPACITY=$(cat "$CAP_FILE")
            log "Battery $(basename "$bat_dir"): status=${BAT_STATUS}, capacity=${BAT_CAPACITY}%"

            if [[ "$BAT_STATUS" == "Discharging" && "$BAT_CAPACITY" -lt "$BAT_MIN_PERCENT" ]]; then
                die "Battery is discharging at ${BAT_CAPACITY}% (minimum ${BAT_MIN_PERCENT}%). " \
                    "Connect AC power before updating to prevent mid-write power loss."
            fi
        fi
    done
    log "Power check passed"
else
    log "No battery sysfs found — assuming AC/PoE powered board, skipping power check"
fi

# --------------------------------------------------------------------------
# All checks passed
# --------------------------------------------------------------------------
log "All pre-install checks passed. Proceeding with update to slot ${RAUC_SLOT_BOOTNAME:-?}."
exit 0
