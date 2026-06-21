#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# uboot-bootcount-reset.sh — Reset the U-Boot bootcount on successful boot.
#
# Called from a systemd service or init.d script AFTER the system has
# confirmed a successful boot (network up, key services healthy).
#
# State flow:
#   Power-on → U-Boot increments bootcount
#   Linux boots → systemd reaches multi-user.target
#   This script runs → bootcount reset to 0
#
# If bootcount is NOT reset before the next reboot, U-Boot will eventually
# treat the current slot as "failed" and switch to the other A/B slot.
#
# Usage:
#   bash uboot-bootcount-reset.sh [--dry-run] [--check] [--verbose]
#
# Environment:
#   UBOOT_ENV_DEV — MTD device or UBI volume for fw_setenv (default: auto)

set -euo pipefail

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
DRY_RUN=false
CHECK_ONLY=false
VERBOSE=false

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_info()    { echo "[INFO]  ${SCRIPT_NAME}: $*"; }
log_warn()    { echo "[WARN]  ${SCRIPT_NAME}: $*" >&2; }
log_error()   { echo "[ERROR] ${SCRIPT_NAME}: $*" >&2; }
log_verbose() { "${VERBOSE}" && echo "[DEBUG] ${SCRIPT_NAME}: $*" || true; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)   DRY_RUN=true ;;
        --check)     CHECK_ONLY=true ;;
        --verbose|-v) VERBOSE=true ;;
        --help|-h)
            grep "^#" "${BASH_SOURCE[0]}" | sed 's/^# \?//' | head -20
            exit 0
            ;;
        *) log_error "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# Prerequisite check
# ---------------------------------------------------------------------------
check_fw_printenv() {
    if ! command -v fw_printenv >/dev/null 2>&1; then
        log_warn "fw_printenv not found — U-Boot tools not installed."
        log_warn "Install u-boot-tools (Debian) or u-boot-fw-utils (Fedora)."
        return 2
    fi
    return 0
}

check_fw_setenv() {
    if ! command -v fw_setenv >/dev/null 2>&1; then
        log_warn "fw_setenv not found — cannot reset bootcount."
        return 2
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Read current bootcount
# ---------------------------------------------------------------------------
get_bootcount() {
    local bc
    bc=$(fw_printenv -n bootcount 2>/dev/null) || {
        log_verbose "bootcount variable not found in U-Boot env (may be 0)"
        echo "0"
        return 0
    }
    echo "${bc}"
}

get_bootlimit() {
    local bl
    bl=$(fw_printenv -n bootlimit 2>/dev/null) || {
        echo "3"  # sensible default
        return 0
    }
    echo "${bl}"
}

get_upgrade_available() {
    fw_printenv -n upgrade_available 2>/dev/null || echo "0"
}

# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
main() {
    check_fw_printenv || exit 2
    check_fw_setenv   || exit 2

    local bootcount
    bootcount=$(get_bootcount)
    local bootlimit
    bootlimit=$(get_bootlimit)
    local upgrade_available
    upgrade_available=$(get_upgrade_available)

    log_info "Current bootcount=${bootcount}  bootlimit=${bootlimit}  upgrade_available=${upgrade_available}"

    if "${CHECK_ONLY}"; then
        if [[ "${bootcount}" -ge "${bootlimit}" ]]; then
            log_warn "bootcount (${bootcount}) >= bootlimit (${bootlimit}) — next reboot may trigger slot switch"
            exit 1
        fi
        log_info "bootcount is within limits — OK"
        exit 0
    fi

    if [[ "${bootcount}" -eq "0" ]]; then
        log_info "bootcount is already 0 — nothing to do"
        exit 0
    fi

    if "${DRY_RUN}"; then
        log_info "DRY-RUN: would run: fw_setenv bootcount 0"
        if [[ "${upgrade_available}" == "1" ]]; then
            log_info "DRY-RUN: would run: fw_setenv upgrade_available 0"
        fi
        exit 0
    fi

    log_info "Resetting bootcount to 0 ..."
    fw_setenv bootcount 0
    log_info "bootcount reset OK"

    # If an OTA update was applied, clear upgrade_available so the slot
    # is committed on the next reboot.
    if [[ "${upgrade_available}" == "1" ]]; then
        log_info "Committing OTA slot: clearing upgrade_available ..."
        fw_setenv upgrade_available 0
        log_info "upgrade_available cleared OK"
    fi

    log_info "Boot confirmed successfully."
}

main "$@"
