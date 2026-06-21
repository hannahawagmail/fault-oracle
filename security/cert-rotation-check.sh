#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# security/cert-rotation-check.sh — Check TLS certificate expiry and warn
# when any cert in a bundle expires within WARN_DAYS days.
#
# Usage:
#   bash security/cert-rotation-check.sh [--cert-dir DIR] [--warn-days N] [--json]
#
# Exit codes:
#   0 — all certs valid and not expiring soon
#   1 — one or more certs expiring within WARN_DAYS
#   2 — one or more certs already expired
#   3 — openssl not found or cert file missing

set -euo pipefail

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
CERT_DIR="${CERT_DIR:-$(pwd)/certs}"
WARN_DAYS="${WARN_DAYS:-30}"
JSON_OUT=false

log_info()  { echo "[INFO]  ${SCRIPT_NAME}: $*" >&2; }
log_warn()  { echo "[WARN]  ${SCRIPT_NAME}: $*" >&2; }
log_error() { echo "[ERROR] ${SCRIPT_NAME}: $*" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cert-dir)  CERT_DIR="$2";  shift ;;
        --warn-days) WARN_DAYS="$2"; shift ;;
        --json)      JSON_OUT=true   ;;
        --help|-h)
            sed -n 's/^# \?//p' "${BASH_SOURCE[0]}" | head -15; exit 0 ;;
        *) log_error "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

command -v openssl >/dev/null 2>&1 || { log_error "openssl not found"; exit 3; }

CERTS=("ca.crt" "server.crt" "client.crt")
MAX_EXIT=0
JSON_ENTRIES=()

check_cert() {
    local name="$1"
    local path="${CERT_DIR}/${name}"

    if [[ ! -f "${path}" ]]; then
        log_warn "Certificate not found: ${path}"
        return 3
    fi

    local not_after
    not_after="$(openssl x509 -in "${path}" -noout -enddate 2>/dev/null | cut -d= -f2)"
    local expiry_epoch
    expiry_epoch="$(date -d "${not_after}" +%s 2>/dev/null || date -j -f "%b %d %T %Y %Z" "${not_after}" +%s 2>/dev/null)"
    local now_epoch
    now_epoch="$(date +%s)"
    local days_left=$(( (expiry_epoch - now_epoch) / 86400 ))

    local status="ok"
    local exit_code=0

    if [[ ${days_left} -lt 0 ]]; then
        status="expired"
        exit_code=2
        log_error "EXPIRED: ${name} expired ${days_left#-} days ago (${not_after})"
    elif [[ ${days_left} -lt ${WARN_DAYS} ]]; then
        status="expiring"
        exit_code=1
        log_warn "EXPIRING: ${name} expires in ${days_left} days (${not_after})"
    else
        log_info "OK: ${name} — ${days_left} days remaining (${not_after})"
    fi

    JSON_ENTRIES+=("{\"cert\":\"${name}\",\"days_left\":${days_left},\"not_after\":\"${not_after}\",\"status\":\"${status}\"}")
    return "${exit_code}"
}

for cert in "${CERTS[@]}"; do
    check_cert "${cert}" || {
        code=$?
        [[ ${code} -gt ${MAX_EXIT} ]] && MAX_EXIT=${code}
    }
done

if "${JSON_OUT}"; then
    printf '{"warn_days":%s,"certs":[%s]}\n' "${WARN_DAYS}" "$(IFS=,; echo "${JSON_ENTRIES[*]}")"
fi

exit "${MAX_EXIT}"
