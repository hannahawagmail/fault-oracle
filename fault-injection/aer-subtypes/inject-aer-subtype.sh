#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# fault-injection/aer-subtypes/inject-aer-subtype.sh
# Inject a specific AER error subtype via the kernel EINJ interface.
# Requires: root, kernel with CONFIG_ACPI_APEI_EINJ, EINJ debug FS mounted.
#
# Usage:
#   bash inject-aer-subtype.sh --bdf 0000:01:00.0 --type pcie-correctable [--dry-run]

set -euo pipefail
BDF=""
TYPE="pcie-correctable"
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bdf)  BDF="$2";  shift ;;
        --type) TYPE="$2"; shift ;;
        --dry-run) DRY_RUN=true ;;
        *) echo "Unknown: $1" >&2; exit 1 ;;
    esac
    shift
done

EINJ_DIR="/sys/kernel/debug/apei/einj"
[[ -d "${EINJ_DIR}" ]] || { echo "EINJ not available (mount debugfs or enable CONFIG_ACPI_APEI_EINJ)"; exit 2; }
[[ -n "${BDF}" ]] || { echo "--bdf required"; exit 1; }

declare -A TYPE_MASK=(
    [pcie-correctable]="0x00000040"
    [pcie-non-fatal]="0x00000080"
    [pcie-fatal]="0x00000100"
)
MASK="${TYPE_MASK[${TYPE}]:-0x00000040}"

if "${DRY_RUN}"; then
    echo "DRY-RUN: would inject AER ${TYPE} on ${BDF} (mask=${MASK})"
    exit 0
fi

echo "${MASK}" > "${EINJ_DIR}/error_type"
echo "${BDF}"  > "${EINJ_DIR}/param1"
echo 1         > "${EINJ_DIR}/error_inject"
echo "Injected AER ${TYPE} on ${BDF}"
