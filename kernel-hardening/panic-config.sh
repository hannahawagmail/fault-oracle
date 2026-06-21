#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# panic-config.sh — Configure kernel panic behavior for production ARM systems
#
# Usage:
#   bash panic-config.sh [--apply] [--status] [--dry-run] [--reboot-delay 10]
#                        [--panic-on-oops] [--watchdog-panic] [--show-bootargs]
#
# Options:
#   --status            Show current values of all panic-related sysctl params
#   --apply             Write recommended values via sysctl -w and persist to
#                       /etc/sysctl.d/99-fault-resilience.conf
#   --dry-run           Show what would be written without making any changes
#   --reboot-delay N    Set kernel.panic = N seconds (default: 10)
#   --panic-on-oops     Include kernel.panic_on_oops=1
#   --watchdog-panic    Include kernel.softlockup_panic=1 kernel.hardlockup_panic=1
#   --show-bootargs     Show /proc/cmdline and suggest additions for U-Boot
#
# Exit codes:
#   0 — success
#   1 — error

set -e

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
REBOOT_DELAY=10
DO_APPLY=false
DO_STATUS=false
DO_DRY_RUN=false
DO_SHOW_BOOTARGS=false
ADD_PANIC_ON_OOPS=false
ADD_WATCHDOG_PANIC=false
SYSCTL_CONF=/etc/sysctl.d/99-fault-resilience.conf

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
BLU='\033[0;34m'
RST='\033[0m'

log()  { echo -e "${BLU}[panic-config]${RST} $*"; }
ok()   { echo -e "${GRN}[OK]${RST} $*"; }
warn() { echo -e "${YLW}[WARN]${RST} $*"; }
err()  { echo -e "${RED}[ERR]${RST} $*" >&2; }

usage() {
    sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

read_sysctl() {
    local key="$1"
    local path="/proc/sys/${key//.//}"
    if [[ -r "$path" ]]; then
        cat "$path"
    else
        echo "N/A"
    fi
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
[[ $# -eq 0 ]] && usage

while [[ $# -gt 0 ]]; do
    case "$1" in
        --status)          DO_STATUS=true ;;
        --apply)           DO_APPLY=true ;;
        --dry-run)         DO_DRY_RUN=true ;;
        --show-bootargs)   DO_SHOW_BOOTARGS=true ;;
        --panic-on-oops)   ADD_PANIC_ON_OOPS=true ;;
        --watchdog-panic)  ADD_WATCHDOG_PANIC=true ;;
        --reboot-delay)
            shift
            if [[ -z "$1" || ! "$1" =~ ^-?[0-9]+$ ]]; then
                err "--reboot-delay requires an integer argument"
                exit 1
            fi
            REBOOT_DELAY="$1"
            ;;
        --help|-h) usage ;;
        *)
            err "Unknown option: $1"
            exit 1
            ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# --status
# ---------------------------------------------------------------------------
if $DO_STATUS; then
    echo ""
    log "Current kernel panic sysctl values:"
    echo ""
    printf "  %-40s %s\n" "Parameter" "Current Value"
    printf "  %-40s %s\n" "---------" "-------------"

    declare -A PARAMS=(
        [kernel.panic]="Reboot delay after panic (0=hang, >0=seconds)"
        [kernel.panic_on_oops]="Treat oops as panic (0=log, 1=panic)"
        [kernel.panic_on_warn]="Treat WARN_ON as panic (0=log, 1=panic)"
        [kernel.softlockup_panic]="Soft lockup triggers panic (0/1)"
        [kernel.hardlockup_panic]="Hard lockup triggers panic (0/1)"
        [kernel.unknown_nmi_panic]="Unknown NMI triggers panic (0/1)"
        [kernel.oops_limit]="Number of oops before panic (0=unlimited)"
    )

    for key in kernel.panic kernel.panic_on_oops kernel.panic_on_warn \
                kernel.softlockup_panic kernel.hardlockup_panic \
                kernel.unknown_nmi_panic kernel.oops_limit; do
        val=$(read_sysctl "$key")
        desc="${PARAMS[$key]}"
        # Highlight dangerous values
        if [[ "$key" == "kernel.panic" && "$val" == "0" ]]; then
            val="${RED}${val} (will hang forever!)${RST}"
        elif [[ "$key" == "kernel.panic_on_oops" && "$val" == "0" ]]; then
            val="${YLW}${val} (oops are non-fatal — not recommended for prod)${RST}"
        fi
        printf "  %-40s %b\n" "$key" "$val"
        printf "  %-40s %s\n" "" "↳ $desc"
        echo ""
    done

    # Check kdump
    echo ""
    log "kdump (crash kernel) status:"
    if [[ -r /sys/kernel/kexec_crash_size ]]; then
        crash_size=$(cat /sys/kernel/kexec_crash_size)
        if [[ "$crash_size" -gt 0 ]]; then
            ok "crash kernel reserved: ${crash_size} bytes"
        else
            warn "crash kernel size is 0 — kdump is not loaded"
            warn "Load with: kexec -p /boot/vmlinuz-... --initrd=..."
        fi
    else
        warn "/sys/kernel/kexec_crash_size not found — kdump not supported or not enabled"
        warn "Add 'crashkernel=256M' to your kernel cmdline to enable it"
    fi
    echo ""
fi

# ---------------------------------------------------------------------------
# --show-bootargs
# ---------------------------------------------------------------------------
if $DO_SHOW_BOOTARGS; then
    echo ""
    log "Current kernel command line (/proc/cmdline):"
    echo ""
    if [[ -r /proc/cmdline ]]; then
        echo "  $(cat /proc/cmdline)"
    else
        warn "Cannot read /proc/cmdline"
    fi
    echo ""
    log "Suggested additions for U-Boot bootargs:"
    echo ""
    echo "  panic=10          — reboot 10 s after panic"
    echo "  panic_on_oops=1   — promote oops to panic"
    echo "  oops=panic        — older alias for panic_on_oops (compatibility)"
    echo "  crashkernel=256M  — reserve memory for kdump (if supported)"
    echo ""
    log "To add in U-Boot:"
    echo ""
    echo "  setenv bootargs \"\${bootargs} panic=10 panic_on_oops=1 oops=panic\""
    echo "  saveenv"
    echo ""
    log "To add via GRUB (if applicable):"
    echo ""
    echo "  Edit /etc/default/grub:"
    echo "    GRUB_CMDLINE_LINUX_DEFAULT=\"... panic=10 panic_on_oops=1\""
    echo "  Then run: update-grub"
    echo ""
fi

# ---------------------------------------------------------------------------
# Build the desired sysctl settings map
# ---------------------------------------------------------------------------
declare -A SETTINGS

# Always include core panic settings
SETTINGS[kernel.panic]="$REBOOT_DELAY"
SETTINGS[kernel.unknown_nmi_panic]="1"
SETTINGS[kernel.oops_limit]="1"

if $ADD_PANIC_ON_OOPS; then
    SETTINGS[kernel.panic_on_oops]="1"
    SETTINGS[kernel.panic_on_warn]="0"
fi

if $ADD_WATCHDOG_PANIC; then
    SETTINGS[kernel.softlockup_panic]="1"
    SETTINGS[kernel.hardlockup_panic]="1"
fi

# ---------------------------------------------------------------------------
# Build the sysctl.d file content
# ---------------------------------------------------------------------------
generate_conf() {
    cat <<EOF
# SPDX-License-Identifier: Apache-2.0
# 99-fault-resilience.conf — Kernel panic policy for production ARM systems
# Generated by panic-config.sh on $(date -u '+%Y-%m-%dT%H:%M:%SZ')
# Deploy: sysctl --system

# ---------------------------------------------------------------------------
# Kernel panic behavior
# ---------------------------------------------------------------------------

# Automatically reboot ${REBOOT_DELAY} seconds after a kernel panic.
# Set to 0 to halt forever (default, wrong for production).
kernel.panic = ${REBOOT_DELAY}

# Treat a kernel oops (non-fatal error) as a full panic.
# Prevents continued execution with a potentially corrupted kernel state.
kernel.panic_on_oops = $(if $ADD_PANIC_ON_OOPS; then echo 1; else echo 0; fi)

# Do NOT panic on WARN_ON() — too many upstream warnings fire on normal load.
# Set to 1 only on staging/CI to catch regressions.
kernel.panic_on_warn = 0

# Soft lockup: a CPU task has not slept for > watchdog_thresh seconds.
# Setting to 1 causes a panic (and reboot) rather than a stuck system.
kernel.softlockup_panic = $(if $ADD_WATCHDOG_PANIC; then echo 1; else echo 0; fi)

# Hard lockup: a CPU has not received any interrupt for several seconds.
# More severe than soft lockup; usually indicates a hardware fault.
kernel.hardlockup_panic = $(if $ADD_WATCHDOG_PANIC; then echo 1; else echo 0; fi)

# Unknown NMI (Non-Maskable Interrupt): panic if an unrecognized NMI fires.
# On ARM this is rarely triggered but is correct to enable for production.
kernel.unknown_nmi_panic = 1

# After this many oopses, force a panic. 1 = panic on first oops.
# Only meaningful when panic_on_oops = 0.
kernel.oops_limit = 1
EOF
}

# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------
if $DO_DRY_RUN; then
    echo ""
    log "Dry-run mode — no changes will be made"
    echo ""
    log "Would write to: ${SYSCTL_CONF}"
    echo ""
    generate_conf
    echo ""
    log "Would execute:"
    echo ""
    for key in "${!SETTINGS[@]}"; do
        echo "  sysctl -w ${key}=${SETTINGS[$key]}"
    done
    echo ""
    ok "Dry-run complete"
    exit 0
fi

# ---------------------------------------------------------------------------
# --apply
# ---------------------------------------------------------------------------
if $DO_APPLY; then
    if [[ $EUID -ne 0 ]] && ! $DO_DRY_RUN; then
        err "--apply requires root privileges"
        exit 1
    fi

    echo ""
    log "Applying kernel panic sysctl settings..."
    echo ""

    # Apply live
    for key in "${!SETTINGS[@]}"; do
        val="${SETTINGS[$key]}"
        if sysctl -w "${key}=${val}" >/dev/null 2>&1; then
            ok "Set ${key}=${val}"
        else
            warn "Could not set ${key}=${val} (parameter may not exist on this kernel)"
        fi
    done

    # Write persistent config
    log "Writing ${SYSCTL_CONF} ..."
    CONF_DIR=$(dirname "$SYSCTL_CONF")
    mkdir -p "$CONF_DIR"
    generate_conf > "${SYSCTL_CONF}.tmp"
    mv "${SYSCTL_CONF}.tmp" "$SYSCTL_CONF"
    ok "Wrote ${SYSCTL_CONF}"

    # Verify
    echo ""
    log "Verifying applied values:"
    for key in "${!SETTINGS[@]}"; do
        got=$(read_sysctl "$key")
        want="${SETTINGS[$key]}"
        if [[ "$got" == "$want" ]]; then
            ok "${key} = ${got}"
        else
            warn "${key} = ${got} (expected ${want})"
        fi
    done
    echo ""
    ok "Done. Settings will persist across reboots via ${SYSCTL_CONF}"
fi
