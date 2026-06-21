#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# fscrypt-setup.sh — Initialise and manage an fscrypt-protected directory.
#
# fscrypt is a Linux kernel feature providing filesystem-level encryption
# for ext4, f2fs, and ubifs. Unlike dm-crypt (block-level), fscrypt encrypts
# individual files — unencrypted metadata (filenames with policy, directory
# structure) is visible to the filesystem.
#
# Usage:
#   bash fscrypt-setup.sh --init    --mountpoint /mnt/data
#   bash fscrypt-setup.sh --create  --mountpoint /mnt/data --dir private/
#   bash fscrypt-setup.sh --unlock  --mountpoint /mnt/data --dir private/
#   bash fscrypt-setup.sh --lock    --dir /mnt/data/private
#   bash fscrypt-setup.sh --status  --dir /mnt/data/private
#   bash fscrypt-setup.sh --demo    [--dry-run]
#
# Requirements:
#   kernel >= 4.1 (ext4 fscrypt), >= 5.4 (recommended)
#   e2fsprogs >= 1.43 (tune2fs -O encrypt)
#   fscrypt CLI tool (https://github.com/google/fscrypt)
#
# Exit codes:
#   0 — success
#   1 — error
#   2 — prerequisite missing

set -euo pipefail

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
MODE=""
MOUNTPOINT=""
DIR=""
DRY_RUN=false

log_info()  { echo "[INFO]  ${SCRIPT_NAME}: $*"; }
log_warn()  { echo "[WARN]  ${SCRIPT_NAME}: $*" >&2; }
log_error() { echo "[ERROR] ${SCRIPT_NAME}: $*" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --init)       MODE="init" ;;
        --create)     MODE="create" ;;
        --unlock)     MODE="unlock" ;;
        --lock)       MODE="lock" ;;
        --status)     MODE="status" ;;
        --demo)       MODE="demo" ;;
        --mountpoint) MOUNTPOINT="$2"; shift ;;
        --dir)        DIR="$2"; shift ;;
        --dry-run)    DRY_RUN=true ;;
        --help|-h)
            sed -n 's/^# \?//p' "${BASH_SOURCE[0]}" | head -25
            exit 0 ;;
        *) log_error "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

[[ -z "${MODE}" ]] && { log_error "Mode required: --init, --create, --unlock, --lock, --status, or --demo"; exit 1; }

check_root() {
    if [[ "${EUID}" -ne 0 ]] && ! "${DRY_RUN}"; then
        log_error "Root required. Use --dry-run for simulation."
        return 2
    fi
}

check_fscrypt_tool() {
    if ! command -v fscrypt >/dev/null 2>&1; then
        log_warn "fscrypt CLI not found."
        log_warn "Install from: https://github.com/google/fscrypt/releases"
        log_warn "Or: apt-get install fscrypt (Debian 11+)"
        return 2
    fi
}

check_kernel_support() {
    if ! grep -q "CONFIG_FS_ENCRYPTION=y" /boot/config-"$(uname -r)" 2>/dev/null; then
        log_warn "CONFIG_FS_ENCRYPTION may not be set in current kernel"
        # Not fatal — kernel might still support it without the config visible
    fi
}

do_init() {
    check_root         || exit 2
    check_fscrypt_tool || exit 2
    [[ -z "${MOUNTPOINT}" ]] && { log_error "--mountpoint required"; exit 1; }

    log_info "Initialising fscrypt on ${MOUNTPOINT} ..."
    if "${DRY_RUN}"; then
        log_info "DRY-RUN: would run: tune2fs -O encrypt ${MOUNTPOINT}"
        log_info "DRY-RUN: would run: fscrypt setup ${MOUNTPOINT}"
        return 0
    fi

    # Enable encrypt feature flag on the filesystem
    local dev
    dev=$(findmnt -n -o SOURCE "${MOUNTPOINT}") || {
        log_error "Cannot determine block device for ${MOUNTPOINT}"
        exit 1
    }
    tune2fs -O encrypt "${dev}" 2>/dev/null || log_warn "tune2fs returned non-zero (may already be enabled)"
    fscrypt setup "${MOUNTPOINT}"
    log_info "fscrypt initialised on ${MOUNTPOINT}"
}

do_create() {
    check_root         || exit 2
    check_fscrypt_tool || exit 2
    [[ -z "${MOUNTPOINT}" ]] && { log_error "--mountpoint required"; exit 1; }
    [[ -z "${DIR}" ]] && { log_error "--dir required"; exit 1; }

    local full_dir="${MOUNTPOINT}/${DIR}"
    log_info "Creating fscrypt-protected directory: ${full_dir}"

    if "${DRY_RUN}"; then
        log_info "DRY-RUN: would run: mkdir -p ${full_dir}"
        log_info "DRY-RUN: would run: fscrypt encrypt ${full_dir} --source=pam_passphrase"
        return 0
    fi

    mkdir -p "${full_dir}"
    fscrypt encrypt "${full_dir}" --source=pam_passphrase --name="fault-resilience-$(date +%s)"
    log_info "Directory ${full_dir} is now encrypted."
    log_info "It will be locked automatically after log out. Unlock with: --unlock"
}

do_unlock() {
    check_root         || exit 2
    check_fscrypt_tool || exit 2
    [[ -z "${MOUNTPOINT}" ]] && { log_error "--mountpoint required"; exit 1; }
    [[ -z "${DIR}" ]] && { log_error "--dir required"; exit 1; }

    local full_dir="${MOUNTPOINT}/${DIR}"
    log_info "Unlocking: ${full_dir}"
    if "${DRY_RUN}"; then
        log_info "DRY-RUN: would run: fscrypt unlock ${full_dir}"
        return 0
    fi
    fscrypt unlock "${full_dir}"
    log_info "Unlocked: ${full_dir}"
}

do_lock() {
    check_root         || exit 2
    check_fscrypt_tool || exit 2
    [[ -z "${DIR}" ]] && { log_error "--dir required"; exit 1; }

    log_info "Locking: ${DIR}"
    if "${DRY_RUN}"; then
        log_info "DRY-RUN: would run: fscrypt lock ${DIR}"
        return 0
    fi
    fscrypt lock "${DIR}"
    log_info "Locked: ${DIR}"
}

do_status() {
    check_fscrypt_tool || exit 2
    [[ -z "${DIR}" ]] && { log_error "--dir required"; exit 1; }

    if "${DRY_RUN}"; then
        log_info "DRY-RUN: would run: fscrypt status ${DIR}"
        return 0
    fi
    fscrypt status "${DIR}"
}

do_demo() {
    log_info "fscrypt demo (informational — no filesystem changes)"
    log_info ""
    log_info "  1. Enable encryption on your ext4 filesystem:"
    log_info "       sudo tune2fs -O encrypt /dev/sda1"
    log_info "       sudo fscrypt setup /mnt/data"
    log_info ""
    log_info "  2. Create an encrypted directory:"
    log_info "       mkdir -p /mnt/data/private"
    log_info "       sudo fscrypt encrypt /mnt/data/private --source=pam_passphrase"
    log_info ""
    log_info "  3. After log out, the directory appears as gibberish filenames."
    log_info "     Unlock with:"
    log_info "       sudo fscrypt unlock /mnt/data/private"
    log_info ""
    log_info "  Threat model: fscrypt protects data at rest against offline access."
    log_info "  It does NOT protect against an attacker with root access to a"
    log_info "  running system — use dm-crypt for that threat model."
    log_info ""
    log_info "  Combine with dm-integrity for integrity + confidentiality:"
    log_info "    dm-integrity (lower layer) → dm-crypt → ext4 + fscrypt"
    if "${DRY_RUN}"; then
        log_info "DRY-RUN mode: all operations above would be simulated."
    fi
}

case "${MODE}" in
    init)   do_init ;;
    create) do_create ;;
    unlock) do_unlock ;;
    lock)   do_lock ;;
    status) do_status ;;
    demo)   do_demo ;;
esac
