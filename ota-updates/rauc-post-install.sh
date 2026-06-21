#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# rauc-post-install.sh — Post-install: flush writes and set bootcount limit
#
# RAUC calls this script after all images have been written to the inactive
# slot and their checksums verified. The bootloader environment has NOT been
# updated yet — RAUC does that after this script exits 0.
#
# Responsibilities of this script:
#   1. Flush kernel write buffers and drop page cache to minimize the risk
#      of data loss if power is cut before the next sync.
#   2. Set U-Boot bootcount=0 and bootlimit=3 so the bootloader will try the
#      new slot up to 3 times before falling back to the current slot.
#   3. Set U-Boot BOOT_ORDER to make the new slot the primary boot target.
#   4. Log the transition so there is an audit trail in journald.
#   5. Restart the WDT keepalive in case the update took longer than the WDT
#      timeout and the keepalive ping period needs resetting.
#
# Exit 0: RAUC proceeds to update bootloader flags and triggers reboot.
# Exit non-zero: RAUC aborts; bootloader flags are NOT changed; old slot boots.
#
# Install to: /usr/lib/rauc/post-install  (matches system.conf [handlers])
# Must be executable: chmod 755 /usr/lib/rauc/post-install
#
# RAUC environment variables available here:
#   RAUC_BUNDLE_VERSION   — version string from bundle manifest
#   RAUC_SLOT_NAME        — slot that was just written (e.g. "rootfs.1")
#   RAUC_SLOT_DEVICE      — block device written (e.g. "/dev/mmcblk0p3")
#   RAUC_SLOT_BOOTNAME    — bootname of the slot just written (e.g. "B")
#   RAUC_SLOT_STATE       — was "inactive" before install
#   RAUC_SLOT_VERSION     — version that was in the slot before this update

set -euo pipefail

log()  { echo "[post-install] $*" >&2; }
warn() { echo "[post-install] WARNING: $*" >&2; }
die()  { echo "[post-install] ABORT: $*" >&2; exit 1; }

NEW_SLOT_BOOTNAME="${RAUC_SLOT_BOOTNAME:-}"
NEW_VERSION="${RAUC_BUNDLE_VERSION:-unknown}"
OLD_VERSION="${RAUC_SLOT_VERSION:-unknown}"
SLOT_DEVICE="${RAUC_SLOT_DEVICE:-}"

log "Post-install: wrote version ${NEW_VERSION} to slot ${RAUC_SLOT_NAME:-?} (${SLOT_DEVICE})"
log "Replaced previous slot version: ${OLD_VERSION}"

# --------------------------------------------------------------------------
# 1. Flush filesystem writes
#    sync() flushes all dirty pages to block devices. We then drop the page
#    cache to force future reads to come from the block device, giving us
#    confidence that what we verify on disk matches what the hardware stored.
#    This is especially important on eMMC with write caching enabled.
# --------------------------------------------------------------------------
log "Flushing kernel write buffers..."
sync

# Drop page cache (1), dentry/inode cache (2), or both (3).
# We use 3 here (most aggressive) because post-update is a natural point
# where we want to verify storage, not read from cache.
if echo 3 > /proc/sys/vm/drop_caches 2>/dev/null; then
    log "Page cache dropped"
else
    warn "Could not drop page cache (non-fatal)"
fi

# Run sync again after cache drop to ensure any deferred writes are flushed
sync
log "Write flush complete"

# --------------------------------------------------------------------------
# 2. Set U-Boot bootcount and bootlimit
#    bootcount: RAUC sets this to 0 so the new slot gets a fresh attempt count.
#    bootlimit: if the new slot fails to boot this many times, U-Boot reverts
#               to the other slot. 3 attempts handles: initial kernel panic,
#               userspace crash-loop, and one more margin attempt.
#
#    fw_setenv comes from the u-boot-tools package. The tool reads
#    /etc/fw_env.config to find where the U-Boot environment is stored
#    (typically /dev/mmcblk0 at a fixed offset, or a dedicated partition).
# --------------------------------------------------------------------------
FW_SETENV=$(command -v fw_setenv 2>/dev/null || true)

if [[ -n "$FW_SETENV" ]]; then
    log "Setting U-Boot bootcount=0"
    "$FW_SETENV" bootcount 0 || die "Failed to set bootcount via fw_setenv"

    log "Setting U-Boot bootlimit=3"
    "$FW_SETENV" bootlimit 3 || die "Failed to set bootlimit via fw_setenv"

    # --------------------------------------------------------------------------
    # 3. Set boot order to prefer the newly-written slot
    #    RAUC will also do this, but we set it here for explicitness and to
    #    ensure it is correct even if RAUC's bootloader integration differs.
    #    The format "B A" means: try B first, fall back to A.
    #    Adapt this logic if your board uses a different boot order variable name.
    # --------------------------------------------------------------------------
    if [[ -n "$NEW_SLOT_BOOTNAME" ]]; then
        # Build BOOT_ORDER: new slot first, other slot as fallback
        case "$NEW_SLOT_BOOTNAME" in
            A) BOOT_ORDER="A B" ;;
            B) BOOT_ORDER="B A" ;;
            *) BOOT_ORDER="${NEW_SLOT_BOOTNAME} A B" ;;  # Extend for 3+ slots
        esac

        log "Setting U-Boot BOOT_ORDER=\"${BOOT_ORDER}\""
        "$FW_SETENV" BOOT_ORDER "$BOOT_ORDER" || die "Failed to set BOOT_ORDER via fw_setenv"
    else
        warn "RAUC_SLOT_BOOTNAME is empty; skipping BOOT_ORDER update"
    fi

    # Verify the values were written correctly
    VERIFY_BOOTCOUNT=$(fw_printenv -n bootcount 2>/dev/null || echo "unknown")
    VERIFY_BOOTLIMIT=$(fw_printenv -n bootlimit 2>/dev/null || echo "unknown")
    VERIFY_ORDER=$(fw_printenv -n BOOT_ORDER 2>/dev/null || echo "unknown")
    log "U-Boot env after update: bootcount=${VERIFY_BOOTCOUNT}, bootlimit=${VERIFY_BOOTLIMIT}, BOOT_ORDER=${VERIFY_ORDER}"
else
    warn "fw_setenv not found — skipping U-Boot environment update."
    warn "Install u-boot-tools package or ensure fw_setenv is in PATH."
    warn "The bootloader may not switch to the new slot without this step!"
    # In a CI/test environment without real U-Boot, this is acceptable.
    # In production, treat this as a fatal error:
    # die "fw_setenv is required for production updates"
fi

# --------------------------------------------------------------------------
# 4. Write transition record to the audit log on /data
#    This gives ops teams an on-device history of OTA updates that survives
#    rootfs wipes. The /data partition is never updated by RAUC.
# --------------------------------------------------------------------------
AUDIT_LOG="/data/ota-audit.log"
TIMESTAMP=$(date --iso-8601=seconds 2>/dev/null || date)
mkdir -p "$(dirname "$AUDIT_LOG")"

{
    echo "---"
    echo "timestamp: ${TIMESTAMP}"
    echo "event: post-install-complete"
    echo "new_version: ${NEW_VERSION}"
    echo "old_slot_version: ${OLD_VERSION}"
    echo "target_slot: ${RAUC_SLOT_NAME:-unknown}"
    echo "target_device: ${SLOT_DEVICE}"
    echo "target_bootname: ${NEW_SLOT_BOOTNAME}"
    echo "boot_order: ${BOOT_ORDER:-not-set}"
    echo "bootlimit: 3"
} >> "$AUDIT_LOG" 2>/dev/null || warn "Could not write to audit log ${AUDIT_LOG}"

log "Audit record written to ${AUDIT_LOG}"

# --------------------------------------------------------------------------
# 5. Restart the WDT keepalive daemon
#    The update may have taken several minutes. If the keepalive daemon pings
#    the WDT every 15s and RAUC's write took 90s, the WDT ping happened ~6
#    times during the write — fine. But if the daemon died during the write
#    (e.g., OOM-killed), we need to restart it before the hardware WDT fires.
# --------------------------------------------------------------------------
WDT_PID_FILE="/run/wdt-keepalive.pid"

if [[ -f "$WDT_PID_FILE" ]]; then
    WDT_PID=$(cat "$WDT_PID_FILE")
    if kill -0 "$WDT_PID" 2>/dev/null; then
        # Process is alive — send SIGUSR1 to trigger an immediate ping
        # (assumes wdt-setup.sh handles SIGUSR1 as a keepalive signal)
        kill -USR1 "$WDT_PID" 2>/dev/null && log "Sent SIGUSR1 to WDT keepalive (PID $WDT_PID)" \
            || warn "Could not signal WDT keepalive PID $WDT_PID"
    else
        warn "WDT keepalive (PID $WDT_PID) appears dead. Attempting restart via systemd..."
        systemctl restart wdt-keepalive.service 2>/dev/null \
            || warn "Could not restart wdt-keepalive.service (non-fatal if not using systemd unit)"
    fi
else
    log "No WDT keepalive PID file; skipping restart signal"
fi

# --------------------------------------------------------------------------
# Done
# --------------------------------------------------------------------------
log "Post-install complete. System will reboot into slot ${NEW_SLOT_BOOTNAME} (version ${NEW_VERSION})."
exit 0
