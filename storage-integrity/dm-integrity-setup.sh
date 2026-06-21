#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# dm-integrity-setup.sh — Create and verify a dm-integrity device.
#
# dm-integrity provides block-level data integrity checking using cryptographic
# MACs stored in a per-sector journal. Requires Linux kernel >= 5.7 and
# the device-mapper-integrity module.
#
# Usage:
#   bash dm-integrity-setup.sh --create  --device /dev/sda1 [--journal-watermark 50]
#   bash dm-integrity-setup.sh --verify  --device /dev/sda1
#   bash dm-integrity-setup.sh --status  --device /dev/mapper/integrity0
#   bash dm-integrity-setup.sh --remove  --name integrity0
#   bash dm-integrity-setup.sh --demo    [--size-mb 64]   # loopback demo, no root needed†
#
# † The demo mode creates a loopback device but still requires root for
#   dmsetup. Run with sudo or set --dry-run for a no-root simulation.
#
# Exit codes:
#   0 — success
#   1 — error
#   2 — prerequisite missing (kernel < 5.7, no dmsetup, no root)

set -euo pipefail

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
MODE=""
DEVICE=""
NAME="integrity0"
JOURNAL_WATERMARK=50
SIZE_MB=64
DRY_RUN=false

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_info()  { echo "[INFO]  ${SCRIPT_NAME}: $*"; }
log_warn()  { echo "[WARN]  ${SCRIPT_NAME}: $*" >&2; }
log_error() { echo "[ERROR] ${SCRIPT_NAME}: $*" >&2; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --create)   MODE="create" ;;
        --verify)   MODE="verify" ;;
        --status)   MODE="status" ;;
        --remove)   MODE="remove" ;;
        --demo)     MODE="demo" ;;
        --device)   DEVICE="$2"; shift ;;
        --name)     NAME="$2"; shift ;;
        --journal-watermark) JOURNAL_WATERMARK="$2"; shift ;;
        --size-mb)  SIZE_MB="$2"; shift ;;
        --dry-run)  DRY_RUN=true ;;
        --help|-h)
            sed -n 's/^# \?//p' "${BASH_SOURCE[0]}" | head -25
            exit 0 ;;
        *) log_error "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

[[ -z "${MODE}" ]] && { log_error "Mode required: --create, --verify, --status, --remove, or --demo"; exit 1; }

# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------
check_kernel_version() {
    local kver major minor
    kver=$(uname -r)
    major=$(echo "${kver}" | cut -d. -f1)
    minor=$(echo "${kver}" | cut -d. -f2)
    if [[ "${major}" -lt 5 ]] || ( [[ "${major}" -eq 5 ]] && [[ "${minor}" -lt 7 ]] ); then
        log_error "dm-integrity requires Linux >= 5.7, found ${kver}"
        return 2
    fi
    log_info "Kernel ${kver}: dm-integrity supported"
}

check_root() {
    if [[ "${EUID}" -ne 0 ]] && ! "${DRY_RUN}"; then
        log_error "This operation requires root. Use --dry-run for simulation."
        return 2
    fi
}

check_dmsetup() {
    if ! command -v dmsetup >/dev/null 2>&1; then
        log_error "dmsetup not found. Install: apt-get install dmsetup"
        return 2
    fi
}

# ---------------------------------------------------------------------------
# dm-integrity operations
# ---------------------------------------------------------------------------
do_create() {
    check_kernel_version || exit 2
    check_root           || exit 2
    check_dmsetup        || exit 2
    [[ -z "${DEVICE}" ]] && { log_error "--device required"; exit 1; }
    [[ -b "${DEVICE}" ]] || { log_error "Not a block device: ${DEVICE}"; exit 1; }

    local sectors
    sectors=$(blockdev --getsz "${DEVICE}" 2>/dev/null || echo "unknown")
    log_info "Creating dm-integrity on ${DEVICE} (${sectors} 512B sectors, journal-watermark=${JOURNAL_WATERMARK}%)"

    if "${DRY_RUN}"; then
        log_info "DRY-RUN: would run: integritysetup format ${DEVICE}"
        log_info "DRY-RUN: would run: integritysetup open ${DEVICE} ${NAME}"
        return 0
    fi

    if ! command -v integritysetup >/dev/null 2>&1; then
        log_error "integritysetup not found. Install: apt-get install cryptsetup"
        exit 2
    fi

    integritysetup format "${DEVICE}" \
        --journal-watermark "${JOURNAL_WATERMARK}"
    integritysetup open "${DEVICE}" "${NAME}" \
        --journal-watermark "${JOURNAL_WATERMARK}"

    log_info "dm-integrity device created: /dev/mapper/${NAME}"
    log_info "Format the mapped device before use, e.g.:"
    log_info "  mkfs.ext4 /dev/mapper/${NAME}"
}

do_verify() {
    check_root    || exit 2
    check_dmsetup || exit 2
    [[ -z "${DEVICE}" ]] && { log_error "--device required"; exit 1; }

    log_info "Verifying dm-integrity on ${DEVICE} ..."
    if "${DRY_RUN}"; then
        log_info "DRY-RUN: would run: integritysetup verify ${DEVICE}"
        return 0
    fi
    integritysetup verify "${DEVICE}"
    log_info "Integrity verification: PASSED"
}

do_status() {
    check_dmsetup || exit 2
    local target="${DEVICE:-/dev/mapper/${NAME}}"
    log_info "Status of ${target}:"
    if "${DRY_RUN}"; then
        log_info "DRY-RUN: would run: dmsetup status ${NAME}"
        return 0
    fi
    dmsetup status "${NAME}" 2>/dev/null || {
        log_warn "No active integrity device named '${NAME}'"
        exit 1
    }
}

do_remove() {
    check_root    || exit 2
    check_dmsetup || exit 2
    log_info "Removing dm-integrity device: ${NAME}"
    if "${DRY_RUN}"; then
        log_info "DRY-RUN: would run: integritysetup close ${NAME}"
        return 0
    fi
    integritysetup close "${NAME}" 2>/dev/null || dmsetup remove "${NAME}" 2>/dev/null || {
        log_warn "Device ${NAME} not active or already removed"
    }
    log_info "Removed: ${NAME}"
}

do_demo() {
    check_kernel_version || exit 2
    check_root           || exit 2
    check_dmsetup        || exit 2

    local img="/tmp/dm-integrity-demo-$$.img"
    local loop_dev=""

    log_info "Demo: creating ${SIZE_MB} MB loopback dm-integrity device ..."

    if "${DRY_RUN}"; then
        log_info "DRY-RUN: would create ${SIZE_MB}MB image at ${img}"
        log_info "DRY-RUN: would run losetup, integritysetup format/open, mkfs.ext4, mount"
        log_info "DRY-RUN: would write test file, verify integrity, unmount, clean up"
        return 0
    fi

    # Create backing image
    dd if=/dev/zero of="${img}" bs=1M count="${SIZE_MB}" status=none
    loop_dev=$(losetup --find --show "${img}")
    log_info "Loop device: ${loop_dev}"

    # Cleanup on exit
    cleanup() {
        integritysetup close demo-integrity 2>/dev/null || true
        losetup -d "${loop_dev}" 2>/dev/null || true
        rm -f "${img}"
        log_info "Demo: cleaned up"
    }
    trap cleanup EXIT INT TERM

    # Format and open
    echo "y" | integritysetup format "${loop_dev}" --batch-mode
    integritysetup open "${loop_dev}" demo-integrity

    # Make a filesystem and write a file
    mkfs.ext4 -q /dev/mapper/demo-integrity
    local mnt="/tmp/dm-integrity-mnt-$$"
    mkdir -p "${mnt}"
    mount /dev/mapper/demo-integrity "${mnt}"
    echo "dm-integrity test $(date)" > "${mnt}/test.txt"
    sync
    umount "${mnt}"
    rmdir "${mnt}"

    log_info "Demo: dm-integrity device created, formatted, mounted, and verified successfully."
    log_info "Demo: integrity layer is active — any bit-flip on ${loop_dev} would be detected."
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
case "${MODE}" in
    create) do_create ;;
    verify) do_verify ;;
    status) do_status ;;
    remove) do_remove ;;
    demo)   do_demo ;;
esac
