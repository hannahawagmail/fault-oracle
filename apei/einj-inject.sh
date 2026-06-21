#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# apei/einj-inject.sh — ACPI EINJ (Error INJection) hardware fault injector.
#
# Requires: CONFIG_ACPI_APEI_EINJ=y, root, and platform EINJ support.
# On ARM bare-metal (SBSA/SBBR compliant) these are exposed via:
#   /sys/kernel/debug/apei/einj/
#
# Usage:
#   sudo ./apei/einj-inject.sh --type mem-correctable [--addr 0x0] [--dry-run]
#
# Error types (ACPI 6.4 Table 18-394):
#   mem-correctable     0x00000001  Memory correctable error
#   mem-uncorrectable   0x00000010  Memory uncorrectable non-fatal
#   mem-fatal           0x00000020  Memory uncorrectable fatal
#   pcie-correctable    0x00000040  PCIe correctable
#   pcie-non-fatal      0x00000080  PCIe uncorrectable non-fatal
#   pcie-fatal          0x00000100  PCIe uncorrectable fatal
#   processor           0x00000008  Processor correctable
set -euo pipefail

EINJ_ROOT="${EINJ_ROOT:-/sys/kernel/debug/apei/einj}"
DRY_RUN=0
ERROR_TYPE="mem-correctable"
PHYS_ADDR=""
APEI_MASK="0xFFFFFFFFFFFF0000"

usage() {
    echo "Usage: $0 --type TYPE [--addr PHYS_ADDR] [--mask MASK] [--dry-run]"
    echo "Types: mem-correctable mem-uncorrectable mem-fatal pcie-correctable pcie-non-fatal pcie-fatal processor"
    exit 1
}

declare -A TYPE_MAP=(
    ["mem-correctable"]="0x00000001"
    ["mem-uncorrectable"]="0x00000010"
    ["mem-fatal"]="0x00000020"
    ["pcie-correctable"]="0x00000040"
    ["pcie-non-fatal"]="0x00000080"
    ["pcie-fatal"]="0x00000100"
    ["processor"]="0x00000008"
)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --type)    ERROR_TYPE="$2";  shift 2 ;;
        --addr)    PHYS_ADDR="$2";   shift 2 ;;
        --mask)    APEI_MASK="$2";   shift 2 ;;
        --dry-run) DRY_RUN=1;        shift ;;
        -h|--help) usage ;;
        *) echo "Unknown argument: $1"; usage ;;
    esac
done

if [[ -z "${TYPE_MAP[$ERROR_TYPE]+x}" ]]; then
    echo "ERROR: Unknown error type '$ERROR_TYPE'" >&2
    echo "Valid types: ${!TYPE_MAP[*]}" >&2
    exit 1
fi

TYPE_HEX="${TYPE_MAP[$ERROR_TYPE]}"

log() { echo "[einj] $*" >&2; }

log "Error type: $ERROR_TYPE ($TYPE_HEX)"
[[ -n "$PHYS_ADDR" ]] && log "Physical addr: $PHYS_ADDR (mask: $APEI_MASK)"
[[ "$DRY_RUN" -eq 1 ]] && log "DRY RUN — no writes to debugfs"

if [[ "$DRY_RUN" -eq 1 ]]; then
    log "Would write: echo $TYPE_HEX > $EINJ_ROOT/error_type"
    [[ -n "$PHYS_ADDR" ]] && log "Would write: echo $PHYS_ADDR > $EINJ_ROOT/param1"
    log "Would write: echo 1 > $EINJ_ROOT/error_inject"
    exit 0
fi

if [[ ! -d "$EINJ_ROOT" ]]; then
    echo "ERROR: EINJ not available at $EINJ_ROOT" >&2
    echo "Ensure: CONFIG_ACPI_APEI_EINJ=y and debugfs mounted at /sys/kernel/debug" >&2
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: EINJ injection requires root" >&2
    exit 1
fi

# Check platform support for this error type
avail=$(cat "$EINJ_ROOT/available_error_type" 2>/dev/null || echo "0")
if [[ "$((avail & TYPE_HEX))" -eq 0 ]]; then
    echo "ERROR: Error type $TYPE_HEX not supported by this platform (available: $avail)" >&2
    exit 1
fi

echo "$TYPE_HEX" > "$EINJ_ROOT/error_type"

if [[ -n "$PHYS_ADDR" ]]; then
    echo "$PHYS_ADDR" > "$EINJ_ROOT/param1"
    echo "$APEI_MASK" > "$EINJ_ROOT/param2"
fi

# Trigger injection
echo 1 > "$EINJ_ROOT/error_inject"
log "Injection triggered. Check dmesg / rasdaemon for results."
