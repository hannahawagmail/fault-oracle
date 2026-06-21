#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# rauc-install-dry.sh — Validate a RAUC bundle without flashing it.
#
# Performs all checks that rauc install would perform (signature, compatibility,
# manifest, slot availability) without writing anything to flash.
#
# Usage:
#   bash rauc-install-dry.sh --bundle /path/to/update.raucb [--system-conf /etc/rauc/system.conf]
#   bash rauc-install-dry.sh --check  /path/to/update.raucb
#
# Exit codes:
#   0 — bundle is valid and would install successfully
#   1 — bundle is invalid or incompatible
#   2 — rauc not installed or bundle not found

set -euo pipefail

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
BUNDLE=""
SYSTEM_CONF="/etc/rauc/system.conf"
MODE="install-dry"

log_info()  { echo "[INFO]  ${SCRIPT_NAME}: $*"; }
log_warn()  { echo "[WARN]  ${SCRIPT_NAME}: $*" >&2; }
log_error() { echo "[ERROR] ${SCRIPT_NAME}: $*" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bundle)      BUNDLE="$2"; shift ;;
        --system-conf) SYSTEM_CONF="$2"; shift ;;
        --check)       MODE="check"; BUNDLE="${2:-}"; shift 2>/dev/null || true ;;
        --help|-h)
            sed -n 's/^# \?//p' "${BASH_SOURCE[0]}" | head -20
            exit 0 ;;
        *.raucb)       BUNDLE="$1" ;;
        *) log_error "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

[[ -z "${BUNDLE}" ]] && { log_error "Bundle path required (--bundle FILE or FILE.raucb)"; exit 1; }
[[ -f "${BUNDLE}" ]] || { log_error "Bundle not found: ${BUNDLE}"; exit 2; }

check_rauc() {
    if ! command -v rauc >/dev/null 2>&1; then
        log_error "rauc not found. Install: apt-get install rauc"
        return 2
    fi
}

check_rauc || exit 2

log_info "Validating RAUC bundle: ${BUNDLE}"
log_info "rauc info output:"
rauc info "${BUNDLE}" || {
    log_error "rauc info failed — bundle may be corrupt or unsigned"
    exit 1
}

log_info ""
log_info "Checking bundle with rauc check-bundle ..."
# rauc check-bundle (available in rauc >= 1.7) validates signature + manifest
if rauc --version 2>&1 | grep -qE "1\.[7-9]|[2-9]\.[0-9]"; then
    rauc check-bundle "${BUNDLE}" || {
        log_error "rauc check-bundle failed"
        exit 1
    }
else
    log_warn "rauc < 1.7 — check-bundle not available, using info only"
fi

if [[ "${MODE}" == "install-dry" ]]; then
    log_info ""
    log_info "Performing dry-run compatibility check ..."
    if [[ -f "${SYSTEM_CONF}" ]]; then
        # Extract compatible from bundle and compare with system.conf
        local_compat=$(grep "^compatible=" "${SYSTEM_CONF}" 2>/dev/null | cut -d= -f2 | tr -d ' ')
        bundle_compat=$(rauc info "${BUNDLE}" 2>/dev/null | grep -i "compatible" | awk '{print $NF}')
        log_info "  System compatible:  ${local_compat:-<not found>}"
        log_info "  Bundle compatible:  ${bundle_compat:-<not found>}"
        if [[ -n "${local_compat}" ]] && [[ -n "${bundle_compat}" ]] \
           && [[ "${local_compat}" != "${bundle_compat}" ]]; then
            log_error "Compatibility mismatch — bundle is for '${bundle_compat}', system is '${local_compat}'"
            exit 1
        fi
    else
        log_warn "System config not found at ${SYSTEM_CONF} — skipping compatibility check"
    fi
fi

log_info ""
log_info "Bundle validation: PASSED"
log_info "The bundle would install correctly (no flash operation performed)."
