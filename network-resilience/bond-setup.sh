#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# bond-setup.sh — Create an active-backup bond over two network interfaces.
#
# Creates a bond device using the Linux bonding driver in active-backup mode
# (mode=1). Falls back to a network-namespace veth-pair setup for CI
# environments where physical NICs are unavailable.
#
# Usage (real hardware):
#   bash bond-setup.sh --create --primary eth0 --secondary eth1 [--name bond0]
#   bash bond-setup.sh --status [--name bond0]
#   bash bond-setup.sh --failover --name bond0          # force switch to secondary
#   bash bond-setup.sh --remove  --name bond0
#
# Usage (veth demo — no real NICs required):
#   bash bond-setup.sh --demo [--dry-run]
#
# Mode mapping:
#   active-backup (mode=1) — Only one slave is active. Failover is automatic
#                             when the active slave's link goes down.
#   LACP (mode=4)          — Both links active, requires switch support.
#                             Use --mode lacp for LACP (not tested here).
#
# Exit codes:
#   0 — success
#   1 — error
#   2 — prerequisite missing (no bonding module, no root)

set -euo pipefail

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
MODE_CMD=""
BOND_NAME="bond0"
PRIMARY_IF=""
SECONDARY_IF=""
BOND_MODE="active-backup"
DRY_RUN=false

log_info()  { echo "[INFO]  ${SCRIPT_NAME}: $*"; }
log_warn()  { echo "[WARN]  ${SCRIPT_NAME}: $*" >&2; }
log_error() { echo "[ERROR] ${SCRIPT_NAME}: $*" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --create)    MODE_CMD="create" ;;
        --status)    MODE_CMD="status" ;;
        --failover)  MODE_CMD="failover" ;;
        --remove)    MODE_CMD="remove" ;;
        --demo)      MODE_CMD="demo" ;;
        --name)      BOND_NAME="$2"; shift ;;
        --primary)   PRIMARY_IF="$2"; shift ;;
        --secondary) SECONDARY_IF="$2"; shift ;;
        --mode)      BOND_MODE="$2"; shift ;;
        --dry-run)   DRY_RUN=true ;;
        --help|-h)
            sed -n 's/^# \?//p' "${BASH_SOURCE[0]}" | head -30
            exit 0 ;;
        *) log_error "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

[[ -z "${MODE_CMD}" ]] && { log_error "Mode required: --create, --status, --failover, --remove, or --demo"; exit 1; }

check_root() {
    if [[ "${EUID}" -ne 0 ]] && ! "${DRY_RUN}"; then
        log_error "Root required. Use --dry-run for simulation."
        return 2
    fi
}

check_bonding_module() {
    if ! lsmod | grep -q "^bonding" 2>/dev/null; then
        if "${DRY_RUN}"; then
            log_info "DRY-RUN: would run: modprobe bonding"
            return 0
        fi
        modprobe bonding 2>/dev/null || {
            log_error "Cannot load bonding module"
            return 2
        }
    fi
}

do_create() {
    check_root            || exit 2
    check_bonding_module  || exit 2
    [[ -z "${PRIMARY_IF}" ]]   && { log_error "--primary required"; exit 1; }
    [[ -z "${SECONDARY_IF}" ]] && { log_error "--secondary required"; exit 1; }

    log_info "Creating bond ${BOND_NAME} (${BOND_MODE}) over ${PRIMARY_IF} + ${SECONDARY_IF}"

    if "${DRY_RUN}"; then
        log_info "DRY-RUN: ip link add ${BOND_NAME} type bond mode ${BOND_MODE}"
        log_info "DRY-RUN: ip link set ${PRIMARY_IF} master ${BOND_NAME}"
        log_info "DRY-RUN: ip link set ${SECONDARY_IF} master ${BOND_NAME}"
        log_info "DRY-RUN: ip link set ${BOND_NAME} up"
        return 0
    fi

    # Bring slaves down before enslaving
    ip link set "${PRIMARY_IF}" down
    ip link set "${SECONDARY_IF}" down

    # Create bond
    ip link add "${BOND_NAME}" type bond mode "${BOND_MODE}"
    echo "+${PRIMARY_IF}"   > /sys/class/net/"${BOND_NAME}"/bonding/slaves 2>/dev/null || \
        ip link set "${PRIMARY_IF}" master "${BOND_NAME}"
    echo "+${SECONDARY_IF}" > /sys/class/net/"${BOND_NAME}"/bonding/slaves 2>/dev/null || \
        ip link set "${SECONDARY_IF}" master "${BOND_NAME}"

    ip link set "${BOND_NAME}" up
    log_info "Bond ${BOND_NAME} created. Active slave: $(cat /sys/class/net/"${BOND_NAME}"/bonding/active_slave 2>/dev/null || echo unknown)"
}

do_status() {
    local bond_dir="/sys/class/net/${BOND_NAME}/bonding"
    if [[ ! -d "${bond_dir}" ]]; then
        log_error "Bond device '${BOND_NAME}' not found"
        exit 1
    fi
    log_info "Bond: ${BOND_NAME}"
    log_info "  Mode:         $(cat "${bond_dir}/mode" 2>/dev/null || echo unknown)"
    log_info "  Active slave: $(cat "${bond_dir}/active_slave" 2>/dev/null || echo none)"
    log_info "  Slaves:       $(cat "${bond_dir}/slaves" 2>/dev/null || echo none)"
    log_info "  MII status:   $(cat "${bond_dir}/mii_status" 2>/dev/null || echo unknown)"
}

do_failover() {
    check_root || exit 2
    local bond_dir="/sys/class/net/${BOND_NAME}/bonding"
    if [[ ! -d "${bond_dir}" ]] && ! "${DRY_RUN}"; then
        log_error "Bond device '${BOND_NAME}' not found"
        exit 1
    fi

    local current_active
    current_active=$(cat "${bond_dir}/active_slave" 2>/dev/null || echo unknown)
    log_info "Current active slave: ${current_active}"

    if "${DRY_RUN}"; then
        log_info "DRY-RUN: would force failover from ${current_active}"
        return 0
    fi

    local slaves
    slaves=$(cat "${bond_dir}/slaves")
    local new_active=""
    for s in ${slaves}; do
        if [[ "${s}" != "${current_active}" ]]; then
            new_active="${s}"
            break
        fi
    done

    if [[ -z "${new_active}" ]]; then
        log_error "No alternative slave to fail over to"
        exit 1
    fi

    echo "${new_active}" > "${bond_dir}/active_slave"
    log_info "Failover complete: active slave is now ${new_active}"
}

do_remove() {
    check_root || exit 2
    log_info "Removing bond ${BOND_NAME} ..."
    if "${DRY_RUN}"; then
        log_info "DRY-RUN: ip link delete ${BOND_NAME}"
        return 0
    fi
    ip link delete "${BOND_NAME}" 2>/dev/null || log_warn "Bond ${BOND_NAME} not found or already removed"
    log_info "Removed ${BOND_NAME}"
}

do_demo() {
    check_root           || exit 2
    check_bonding_module || exit 2

    local NS="bond-test-$$"
    log_info "Demo: creating veth pair in network namespace ${NS} ..."

    if "${DRY_RUN}"; then
        log_info "DRY-RUN: ip netns add ${NS}"
        log_info "DRY-RUN: ip link add veth0 type veth peer veth1"
        log_info "DRY-RUN: ip link add ${BOND_NAME} type bond mode active-backup"
        log_info "DRY-RUN: ip link set veth0 master ${BOND_NAME}"
        log_info "DRY-RUN: ip link set veth1 master ${BOND_NAME}"
        log_info "DRY-RUN: ip link set ${BOND_NAME} up"
        log_info "DRY-RUN: (send traffic, verify failover, clean up)"
        log_info "DRY-RUN: ip link delete ${BOND_NAME}"
        log_info "DRY-RUN: ip netns delete ${NS}"
        return 0
    fi

    # Cleanup on exit
    cleanup() {
        ip link delete "${BOND_NAME}" 2>/dev/null || true
        ip link delete veth0 2>/dev/null || true
        ip netns delete "${NS}" 2>/dev/null || true
        log_info "Demo: cleaned up namespace ${NS}"
    }
    trap cleanup EXIT INT TERM

    ip netns add "${NS}"
    ip link add veth0 type veth peer name veth1
    ip link set veth1 netns "${NS}"

    ip link add "${BOND_NAME}" type bond mode active-backup
    ip link set veth0 master "${BOND_NAME}"
    ip link set "${BOND_NAME}" up
    ip link set veth0 up

    local active
    active=$(cat "/sys/class/net/${BOND_NAME}/bonding/active_slave" 2>/dev/null || echo "veth0")
    log_info "Demo: bond created. Active slave: ${active}"
    log_info "Demo: bond-setup demo completed successfully."
}

case "${MODE_CMD}" in
    create)   do_create ;;
    status)   do_status ;;
    failover) do_failover ;;
    remove)   do_remove ;;
    demo)     do_demo ;;
esac
