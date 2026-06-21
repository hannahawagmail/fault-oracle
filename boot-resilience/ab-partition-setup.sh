#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# ab-partition-setup.sh — Set up and manage A/B rootfs partition scheme
#
# State machine: active | pending | failed | standby
# Integrates with U-Boot environment variables for bootcount-based failover
#
# Reads state from:
#   1. fw_printenv (U-Boot environment — most authoritative)
#   2. /proc/cmdline (detect current boot slot from root= parameter)
#   3. /sys/firmware/devicetree/base/chosen/bootargs (U-Boot passed args)
#
# Usage:
#   bash ab-partition-setup.sh [--status]
#                              [--mark-pending  <A|B>]
#                              [--mark-active   <A|B>]
#                              [--mark-failed   <A|B>]
#                              [--set-bootlimit <N>]
#                              [--switch]
#                              [--dry-run]
#
# Exit codes:
#   0 — success
#   1 — error or validation failure
#   2 — fw_printenv not available (graceful skip)

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults and state
# ---------------------------------------------------------------------------
DRY_RUN=false
MODE=""
PART_ARG=""
BOOTLIMIT_ARG=""

# Partition device paths — adjust for your board
# Common patterns:
#   eMMC: /dev/mmcblk0p2 (A), /dev/mmcblk0p3 (B)
#   NVMe: /dev/nvme0n1p2 (A), /dev/nvme0n1p3 (B)
#   SD:   /dev/mmcblk1p2 (A), /dev/mmcblk1p3 (B)
PART_A_LABEL="rootfs-A"
PART_B_LABEL="rootfs-B"

# U-Boot environment variable names (must match board's uboot-bootcount.env)
UENV_SLOT_A_STATE="slot_a_state"
UENV_SLOT_B_STATE="slot_b_state"
UENV_BOOTSLOT="bootslot"
UENV_BOOTCOUNT="bootcount"
UENV_BOOTLIMIT="bootlimit"

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
info()  { echo "[INFO]  $(date '+%Y-%m-%d %H:%M:%S') $*" >&2; }
warn()  { echo "[WARN]  $(date '+%Y-%m-%d %H:%M:%S') $*" >&2; }
error() { echo "[ERROR] $(date '+%Y-%m-%d %H:%M:%S') $*" >&2; }
pass()  { echo "[ PASS ] $*"; }
fail()  { echo "[ FAIL ] $*"; }
skip()  { echo "[ SKIP ] $*"; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --status)        MODE="status";                    shift   ;;
            --mark-pending)  MODE="mark-pending";  PART_ARG="$2"; shift 2 ;;
            --mark-active)   MODE="mark-active";   PART_ARG="$2"; shift 2 ;;
            --mark-failed)   MODE="mark-failed";   PART_ARG="$2"; shift 2 ;;
            --set-bootlimit) MODE="set-bootlimit"; BOOTLIMIT_ARG="$2"; shift 2 ;;
            --switch)        MODE="switch";                    shift   ;;
            --dry-run)       DRY_RUN=true;                    shift   ;;
            --help|-h)
                sed -n '2,20p' "$0" | sed 's/^# //'
                exit 0
                ;;
            *)
                error "Unknown argument: $1"
                exit 1
                ;;
        esac
    done

    if [[ -z "$MODE" ]]; then
        error "No mode specified. Use --status, --mark-pending, --mark-active, etc."
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Validate partition letter argument
# ---------------------------------------------------------------------------
validate_partition() {
    local part="$1"
    if [[ "$part" != "A" && "$part" != "B" ]]; then
        error "Invalid partition '$part'. Must be 'A' or 'B'."
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Check whether fw_printenv / fw_setenv are available
# ---------------------------------------------------------------------------
check_fw_env() {
    if ! command -v fw_printenv &>/dev/null; then
        warn "fw_printenv not found. Install u-boot-tools package."
        warn "On Debian/Ubuntu: apt-get install u-boot-tools"
        warn "Falling back to /proc/cmdline and /sys/firmware/devicetree/..."
        return 1
    fi

    # Verify it can actually talk to the environment (config may be missing)
    if ! fw_printenv "$UENV_BOOTCOUNT" &>/dev/null; then
        warn "fw_printenv found but cannot read environment."
        warn "Check /etc/fw_env.config for correct MTD/eMMC partition offsets."
        return 1
    fi

    return 0
}

# ---------------------------------------------------------------------------
# Read a U-Boot env variable; returns empty string on failure
# ---------------------------------------------------------------------------
uenv_get() {
    local var="$1"
    fw_printenv -n "$var" 2>/dev/null || echo ""
}

# ---------------------------------------------------------------------------
# Set a U-Boot env variable (respects dry-run)
# ---------------------------------------------------------------------------
uenv_set() {
    local var="$1"
    local val="$2"
    if $DRY_RUN; then
        info "[dry-run] Would run: fw_setenv $var $val"
        return 0
    fi
    fw_setenv "$var" "$val"
}

# ---------------------------------------------------------------------------
# Detect current boot slot from /proc/cmdline or DTB chosen bootargs
# ---------------------------------------------------------------------------
detect_current_slot_from_cmdline() {
    local cmdline=""

    # Prefer /proc/cmdline (kernel command line actually in use)
    if [[ -r /proc/cmdline ]]; then
        cmdline=$(cat /proc/cmdline)
    fi

    # Also check U-Boot-passed args via device tree chosen node
    local dt_bootargs="/sys/firmware/devicetree/base/chosen/bootargs"
    if [[ -r "$dt_bootargs" ]]; then
        local dt_args
        dt_args=$(cat "$dt_bootargs" 2>/dev/null | tr -d '\0')
        # Use DT args if /proc/cmdline is empty or matches DT
        [[ -z "$cmdline" ]] && cmdline="$dt_args"
    fi

    # Extract root= parameter and try to determine slot
    local root_dev
    root_dev=$(echo "$cmdline" | grep -oP 'root=\K\S+' || echo "")

    if [[ -z "$root_dev" ]]; then
        echo "unknown"
        return
    fi

    # Resolve by-label if used
    if echo "$root_dev" | grep -q "PARTLABEL\|LABEL"; then
        local label
        label=$(echo "$root_dev" | grep -oP 'PARTLABEL=\K\S+|LABEL=\K\S+' || echo "")
        if [[ "$label" == "$PART_A_LABEL" ]]; then
            echo "A"
            return
        elif [[ "$label" == "$PART_B_LABEL" ]]; then
            echo "B"
            return
        fi
    fi

    # Resolve by device path convention (p2 = A, p3 = B)
    if echo "$root_dev" | grep -qE 'p2$|2$'; then
        echo "A"
    elif echo "$root_dev" | grep -qE 'p3$|3$'; then
        echo "B"
    else
        echo "unknown (root=$root_dev)"
    fi
}

# ---------------------------------------------------------------------------
# --status mode
# ---------------------------------------------------------------------------
mode_status() {
    echo "=== A/B Partition Status ==="
    echo ""

    # Detect current slot from cmdline first (always available)
    local current_slot
    current_slot=$(detect_current_slot_from_cmdline)
    echo "  Current boot slot (from cmdline): $current_slot"

    echo ""
    if check_fw_env; then
        echo "  U-Boot environment variables:"
        local slot_a slot_b bootslot bootcount bootlimit
        slot_a=$(uenv_get "$UENV_SLOT_A_STATE")
        slot_b=$(uenv_get "$UENV_SLOT_B_STATE")
        bootslot=$(uenv_get "$UENV_BOOTSLOT")
        bootcount=$(uenv_get "$UENV_BOOTCOUNT")
        bootlimit=$(uenv_get "$UENV_BOOTLIMIT")

        printf "    %-20s = %s\n" "slot_a_state"  "${slot_a:-<unset>}"
        printf "    %-20s = %s\n" "slot_b_state"  "${slot_b:-<unset>}"
        printf "    %-20s = %s\n" "bootslot"      "${bootslot:-<unset>}"
        printf "    %-20s = %s\n" "bootcount"     "${bootcount:-<unset>}"
        printf "    %-20s = %s\n" "bootlimit"     "${bootlimit:-<unset>}"

        echo ""
        # Interpret state
        if [[ -n "$bootcount" && -n "$bootlimit" ]]; then
            if (( bootcount >= bootlimit )); then
                fail "bootcount ($bootcount) >= bootlimit ($bootlimit) — system is in fallback risk zone"
            else
                pass "bootcount ($bootcount) < bootlimit ($bootlimit) — OK"
            fi
        fi

        if [[ -n "$slot_a" ]]; then
            if [[ "$slot_a" == "active" ]]; then
                pass "Slot A: $slot_a"
            elif [[ "$slot_a" == "failed" ]]; then
                fail "Slot A: $slot_a"
            else
                skip "Slot A: $slot_a"
            fi
        fi

        if [[ -n "$slot_b" ]]; then
            if [[ "$slot_b" == "active" ]]; then
                pass "Slot B: $slot_b"
            elif [[ "$slot_b" == "failed" ]]; then
                fail "Slot B: $slot_b"
            else
                skip "Slot B: $slot_b"
            fi
        fi
    else
        skip "fw_printenv unavailable — showing cmdline only"
        skip "Install u-boot-tools and configure /etc/fw_env.config to get full status"
        echo ""
        # Try to show partition info from blkid
        if command -v blkid &>/dev/null; then
            echo "  Partition labels (from blkid):"
            blkid -o list 2>/dev/null | grep -iE "rootfs|boot" || echo "  (none found or not root)"
        fi
        exit 2
    fi

    echo ""
    echo "=== End Status ==="
}

# ---------------------------------------------------------------------------
# Mark a partition as pending (next boot will try it)
# ---------------------------------------------------------------------------
mode_mark_pending() {
    local part="$1"
    validate_partition "$part"
    local other
    [[ "$part" == "A" ]] && other="B" || other="A"
    local state_var="slot_$(echo "$part" | tr '[:upper:]' '[:lower:]')_state"

    info "Marking slot $part as 'pending' (next boot will attempt it)"

    if check_fw_env; then
        uenv_set "$state_var" "pending"
        uenv_set "$UENV_BOOTSLOT" "$part"
        uenv_set "$UENV_BOOTCOUNT" "0"
        pass "Slot $part marked as pending. Reboot to activate."
    else
        fail "Cannot mark pending — fw_setenv not available"
        exit 2
    fi
}

# ---------------------------------------------------------------------------
# Mark a partition as active (boot confirmed successful)
# ---------------------------------------------------------------------------
mode_mark_active() {
    local part="$1"
    validate_partition "$part"
    local other
    [[ "$part" == "A" ]] && other="B" || other="A"
    local state_var="slot_$(echo "$part" | tr '[:upper:]' '[:lower:]')_state"
    local other_var="slot_$(echo "$other" | tr '[:upper:]' '[:lower:]')_state"

    info "Marking slot $part as 'active' — confirming successful boot"

    if check_fw_env; then
        uenv_set "$state_var" "active"
        uenv_set "$UENV_BOOTCOUNT" "0"
        # Set other slot to standby (it's no longer the active one)
        local other_state
        other_state=$(uenv_get "$other_var")
        if [[ "$other_state" != "failed" ]]; then
            uenv_set "$other_var" "standby"
            info "Slot $other set to 'standby'"
        fi
        pass "Slot $part marked as active. bootcount reset to 0."
    else
        fail "Cannot mark active — fw_setenv not available"
        exit 2
    fi
}

# ---------------------------------------------------------------------------
# Mark a partition as failed (trigger fallback on next boot)
# ---------------------------------------------------------------------------
mode_mark_failed() {
    local part="$1"
    validate_partition "$part"
    local other
    [[ "$part" == "A" ]] && other="B" || other="A"
    local state_var="slot_$(echo "$part" | tr '[:upper:]' '[:lower:]')_state"

    warn "Marking slot $part as 'failed' — will not be selected for boot"

    if check_fw_env; then
        uenv_set "$state_var" "failed"
        # Switch to other slot if current was pending/active on the failed one
        local current_bootslot
        current_bootslot=$(uenv_get "$UENV_BOOTSLOT")
        if [[ "$current_bootslot" == "$part" ]]; then
            info "Switching bootslot from $part to $other"
            uenv_set "$UENV_BOOTSLOT" "$other"
            uenv_set "$UENV_BOOTCOUNT" "0"
        fi
        fail "Slot $part marked as failed. System will use slot $other."
    else
        fail "Cannot mark failed — fw_setenv not available"
        exit 2
    fi
}

# ---------------------------------------------------------------------------
# Set bootlimit
# ---------------------------------------------------------------------------
mode_set_bootlimit() {
    local limit="$1"
    if ! [[ "$limit" =~ ^[0-9]+$ ]] || (( limit < 1 || limit > 10 )); then
        error "bootlimit must be an integer between 1 and 10, got: $limit"
        exit 1
    fi

    info "Setting bootlimit to $limit"
    if check_fw_env; then
        uenv_set "$UENV_BOOTLIMIT" "$limit"
        pass "bootlimit set to $limit"
    else
        fail "Cannot set bootlimit — fw_setenv not available"
        exit 2
    fi
}

# ---------------------------------------------------------------------------
# Switch active partition (A→B or B→A)
# ---------------------------------------------------------------------------
mode_switch() {
    if ! check_fw_env; then
        fail "Cannot switch — fw_setenv not available"
        exit 2
    fi

    local current
    current=$(uenv_get "$UENV_BOOTSLOT")
    if [[ -z "$current" ]]; then
        # Fall back to cmdline detection
        current=$(detect_current_slot_from_cmdline)
    fi

    local target
    case "$current" in
        A) target="B" ;;
        B) target="A" ;;
        *)
            error "Cannot determine current slot ('$current'). Set bootslot manually."
            exit 1
            ;;
    esac

    info "Switching boot slot from $current to $target"
    local target_state_var="slot_$(echo "$target" | tr '[:upper:]' '[:lower:]')_state"
    local target_state
    target_state=$(uenv_get "$target_state_var")

    if [[ "$target_state" == "failed" ]]; then
        warn "Target slot $target is marked 'failed'. Proceeding anyway with forced switch."
    fi

    uenv_set "$UENV_BOOTSLOT" "$target"
    uenv_set "$target_state_var" "pending"
    uenv_set "$UENV_BOOTCOUNT" "0"
    pass "Switched to slot $target (pending). Reboot to activate."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    parse_args "$@"

    case "$MODE" in
        status)       mode_status                    ;;
        mark-pending) mode_mark_pending  "$PART_ARG" ;;
        mark-active)  mode_mark_active   "$PART_ARG" ;;
        mark-failed)  mode_mark_failed   "$PART_ARG" ;;
        set-bootlimit)mode_set_bootlimit "$BOOTLIMIT_ARG" ;;
        switch)       mode_switch                    ;;
    esac
}

main "$@"
