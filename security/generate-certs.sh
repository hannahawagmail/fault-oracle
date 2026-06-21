#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# security/generate-certs.sh — Generate CA, server, and client TLS certificates
# for mutual TLS authentication between Prometheus scrapers and hw-fault-exporter.
#
# Usage:
#   bash security/generate-certs.sh [--out-dir DIR] [--days N] [--cn HOSTNAME] [--dry-run]
#
# Output (in OUT_DIR):
#   ca.key / ca.crt          — Self-signed CA
#   server.key / server.crt  — Exporter server cert (signed by CA)
#   client.key / client.crt  — Prometheus client cert (signed by CA)
#   server-bundle.crt        — server.crt + ca.crt (for nginx-style trust chains)
#
# Exit codes:
#   0 — success
#   1 — error
#   2 — openssl not found

set -euo pipefail

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
OUT_DIR="${OUT_DIR:-$(pwd)/certs}"
DAYS="${DAYS:-825}"   # Apple/Chrome max validity
CN="${CN:-hw-fault-exporter}"
DRY_RUN=false

log_info()  { echo "[INFO]  ${SCRIPT_NAME}: $*"; }
log_error() { echo "[ERROR] ${SCRIPT_NAME}: $*" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --out-dir) OUT_DIR="$2"; shift ;;
        --days)    DAYS="$2";    shift ;;
        --cn)      CN="$2";      shift ;;
        --dry-run) DRY_RUN=true  ;;
        --help|-h)
            sed -n 's/^# \?//p' "${BASH_SOURCE[0]}" | head -20; exit 0 ;;
        *) log_error "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

command -v openssl >/dev/null 2>&1 || { log_error "openssl not found"; exit 2; }

if "${DRY_RUN}"; then
    log_info "DRY-RUN: would create certs in: ${OUT_DIR}"
    log_info "DRY-RUN:   CA validity:     ${DAYS} days"
    log_info "DRY-RUN:   Server CN:       ${CN}"
    log_info "DRY-RUN:   Client CN:       prometheus-scraper"
    exit 0
fi

mkdir -p "${OUT_DIR}"
chmod 700 "${OUT_DIR}"

OPENSSL_CNF="${OUT_DIR}/openssl.cnf"
cat > "${OPENSSL_CNF}" << CONF
[req]
default_bits       = 4096
prompt             = no
default_md         = sha256
distinguished_name = dn
x509_extensions    = v3_ca

[dn]
C  = US
ST = Washington
O  = arm-linux-fault-resilience
CN = fault-resilience-ca

[v3_ca]
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid:always,issuer
basicConstraints       = critical,CA:true
keyUsage               = critical,digitalSignature,cRLSign,keyCertSign

[v3_server]
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid,issuer
basicConstraints       = CA:FALSE
keyUsage               = critical,digitalSignature,keyEncipherment
extendedKeyUsage       = serverAuth
subjectAltName         = @alt_names

[v3_client]
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid,issuer
basicConstraints       = CA:FALSE
keyUsage               = critical,digitalSignature
extendedKeyUsage       = clientAuth

[alt_names]
DNS.1 = ${CN}
DNS.2 = localhost
DNS.3 = hw-fault-exporter.monitoring.svc.cluster.local
IP.1  = 127.0.0.1
CONF

log_info "Generating CA key + certificate ..."
openssl genrsa -out "${OUT_DIR}/ca.key" 4096 2>/dev/null
openssl req -new -x509 \
    -key "${OUT_DIR}/ca.key" \
    -out "${OUT_DIR}/ca.crt" \
    -days "${DAYS}" \
    -config "${OPENSSL_CNF}" \
    -extensions v3_ca 2>/dev/null
log_info "  CA cert: ${OUT_DIR}/ca.crt"

log_info "Generating server key + CSR + certificate ..."
openssl genrsa -out "${OUT_DIR}/server.key" 4096 2>/dev/null
openssl req -new \
    -key  "${OUT_DIR}/server.key" \
    -out  "${OUT_DIR}/server.csr" \
    -subj "/C=US/ST=Washington/O=arm-linux-fault-resilience/CN=${CN}" 2>/dev/null
openssl x509 -req \
    -in     "${OUT_DIR}/server.csr" \
    -CA     "${OUT_DIR}/ca.crt" \
    -CAkey  "${OUT_DIR}/ca.key" \
    -CAcreateserial \
    -out    "${OUT_DIR}/server.crt" \
    -days   "${DAYS}" \
    -extfile "${OPENSSL_CNF}" \
    -extensions v3_server 2>/dev/null
log_info "  Server cert: ${OUT_DIR}/server.crt"

log_info "Generating client key + CSR + certificate ..."
openssl genrsa -out "${OUT_DIR}/client.key" 4096 2>/dev/null
openssl req -new \
    -key  "${OUT_DIR}/client.key" \
    -out  "${OUT_DIR}/client.csr" \
    -subj "/C=US/ST=Washington/O=arm-linux-fault-resilience/CN=prometheus-scraper" 2>/dev/null
openssl x509 -req \
    -in     "${OUT_DIR}/client.csr" \
    -CA     "${OUT_DIR}/ca.crt" \
    -CAkey  "${OUT_DIR}/ca.key" \
    -CAcreateserial \
    -out    "${OUT_DIR}/client.crt" \
    -days   "${DAYS}" \
    -extfile "${OPENSSL_CNF}" \
    -extensions v3_client 2>/dev/null
log_info "  Client cert: ${OUT_DIR}/client.crt"

# Bundle for trust chain verification
cat "${OUT_DIR}/server.crt" "${OUT_DIR}/ca.crt" > "${OUT_DIR}/server-bundle.crt"

# Lock down private keys
chmod 600 "${OUT_DIR}/ca.key" "${OUT_DIR}/server.key" "${OUT_DIR}/client.key"
chmod 644 "${OUT_DIR}/ca.crt" "${OUT_DIR}/server.crt" "${OUT_DIR}/client.crt" "${OUT_DIR}/server-bundle.crt"

log_info "Verifying certificate chain ..."
openssl verify -CAfile "${OUT_DIR}/ca.crt" "${OUT_DIR}/server.crt" 2>/dev/null
openssl verify -CAfile "${OUT_DIR}/ca.crt" "${OUT_DIR}/client.crt" 2>/dev/null
log_info "  Chain verification: OK"

log_info "Certificate summary:"
log_info "  $(openssl x509 -in "${OUT_DIR}/ca.crt"     -noout -subject -dates 2>/dev/null | tr '\n' '  ')"
log_info "  $(openssl x509 -in "${OUT_DIR}/server.crt" -noout -subject -dates 2>/dev/null | tr '\n' '  ')"
log_info "  $(openssl x509 -in "${OUT_DIR}/client.crt" -noout -subject -dates 2>/dev/null | tr '\n' '  ')"

log_info "Done. Certs written to: ${OUT_DIR}"
