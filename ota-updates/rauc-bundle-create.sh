#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# rauc-bundle-create.sh — Create a signed RAUC update bundle from a rootfs tarball.
#
# RAUC (Robust Auto-Update Controller) bundles are squashfs archives containing
# a manifest, the update payload(s), and a CMS signature. This script wraps the
# rauc CLI to produce a signed bundle ready for deployment.
#
# Usage:
#   bash rauc-bundle-create.sh \
#       --rootfs    /path/to/rootfs.tar.gz \
#       --output    /path/to/update.raucb  \
#       --keyfile   /path/to/signing.key   \
#       --certfile  /path/to/signing.crt   \
#       [--version  1.2.3]                 \
#       [--slot     rootfs.0]              \
#       [--dry-run]
#
# Output: a .raucb file compatible with rauc install.
#
# Exit codes:
#   0 — bundle created and verified
#   1 — error
#   2 — prerequisite missing (rauc not installed, no signing keys)

set -euo pipefail

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
ROOTFS=""
OUTPUT=""
KEYFILE=""
CERTFILE=""
VERSION="$(date +%Y%m%d%H%M%S)"
SLOT="rootfs.0"
DRY_RUN=false

log_info()  { echo "[INFO]  ${SCRIPT_NAME}: $*"; }
log_warn()  { echo "[WARN]  ${SCRIPT_NAME}: $*" >&2; }
log_error() { echo "[ERROR] ${SCRIPT_NAME}: $*" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --rootfs)   ROOTFS="$2"; shift ;;
        --output)   OUTPUT="$2"; shift ;;
        --keyfile)  KEYFILE="$2"; shift ;;
        --certfile) CERTFILE="$2"; shift ;;
        --version)  VERSION="$2"; shift ;;
        --slot)     SLOT="$2"; shift ;;
        --dry-run)  DRY_RUN=true ;;
        --help|-h)
            sed -n 's/^# \?//p' "${BASH_SOURCE[0]}" | head -25
            exit 0 ;;
        *) log_error "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------
check_rauc() {
    if ! command -v rauc >/dev/null 2>&1; then
        log_error "rauc not found. Install: apt-get install rauc"
        return 2
    fi
}

check_args() {
    local missing=()
    [[ -z "${ROOTFS}" ]]   && missing+=("--rootfs")
    [[ -z "${OUTPUT}" ]]   && missing+=("--output")
    [[ -z "${KEYFILE}" ]]  && missing+=("--keyfile")
    [[ -z "${CERTFILE}" ]] && missing+=("--certfile")
    if [[ ${#missing[@]} -gt 0 ]]; then
        log_error "Missing required arguments: ${missing[*]}"
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
check_rauc || exit 2
check_args

[[ -f "${ROOTFS}" ]]   || { log_error "Rootfs not found: ${ROOTFS}"; exit 1; }
[[ -f "${KEYFILE}" ]]  || { log_error "Key file not found: ${KEYFILE}"; exit 1; }
[[ -f "${CERTFILE}" ]] || { log_error "Cert file not found: ${CERTFILE}"; exit 1; }

BUNDLE_DIR="$(mktemp -d /tmp/rauc-bundle-XXXXXX)"
trap 'rm -rf "${BUNDLE_DIR}"' EXIT

log_info "Creating RAUC bundle:"
log_info "  Rootfs:  ${ROOTFS}"
log_info "  Output:  ${OUTPUT}"
log_info "  Version: ${VERSION}"
log_info "  Slot:    ${SLOT}"

# Write manifest
cat > "${BUNDLE_DIR}/manifest.raucm" << MANIFEST
[update]
compatible=arm-linux-fault-resilience-board
version=${VERSION}

[bundle]
format=verity

[image.${SLOT}]
filename=$(basename "${ROOTFS}")
MANIFEST

log_info "Manifest written: ${BUNDLE_DIR}/manifest.raucm"

# Copy rootfs payload
cp "${ROOTFS}" "${BUNDLE_DIR}/"
log_info "Payload copied: $(basename "${ROOTFS}")"

if "${DRY_RUN}"; then
    log_info "DRY-RUN: would run: rauc bundle \\"
    log_info "DRY-RUN:   --cert ${CERTFILE} \\"
    log_info "DRY-RUN:   --key  ${KEYFILE} \\"
    log_info "DRY-RUN:   ${BUNDLE_DIR} ${OUTPUT}"
    log_info "DRY-RUN: bundle not created (dry run)"
    exit 0
fi

# Create the bundle
rauc bundle \
    --cert "${CERTFILE}" \
    --key  "${KEYFILE}" \
    "${BUNDLE_DIR}" \
    "${OUTPUT}"

# Verify the bundle
log_info "Verifying bundle ..."
rauc info "${OUTPUT}"
log_info "Bundle created and verified: ${OUTPUT}"
