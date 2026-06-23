#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# inject_aer.sh — Inject PCIe AER errors and verify counter response.
#
# Supports two injection backends:
#   1. aer-inject (kernel module) — userspace tool for injecting AER events via
#      /dev/aer-inject. Available in kernel tools/pci/aer-inject.c. Suitable for
#      QEMU virt and real hardware with PCIe devices.
#   2. ACPI EINJ — firmware-mediated injection via /sys/kernel/debug/apei/einj/.
#      Available on ARM servers with ACPI EINJ table. More invasive.
#
# The script auto-detects which backend is available and selects it.
#
# Usage:
#   sudo bash inject_aer.sh [options]
#
# Options:
#   --device <BDF>       PCIe device BDF (e.g., 0000:01:00.0). Default: first device with AER.
#   --type correctable   Error type: correctable | nonfatal | fatal (default: correctable)
#   --error BadTLP       Specific error name (default: BadTLP for correctable)
#   --backend auto       Backend: auto | aer-inject | einj (default: auto)
#   --no-verify          Skip counter verification
#   --verbose            Verbose output
#
# Exit: 0 = success, 1 = failure, 2 = no AER-capable devices or backends found (skipped)

set -euo pipefail

DEVICE=""
ERROR_TYPE="correctable"
ERROR_NAME="BadTLP"
BACKEND="auto"
VERIFY=true
VERBOSE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --device)     DEVICE="$2"; shift 2 ;;
        --type)       ERROR_TYPE="$2"; shift 2 ;;
        --error)      ERROR_NAME="$2"; shift 2 ;;
        --backend)    BACKEND="$2"; shift 2 ;;
        --no-verify)  VERIFY=false; shift ;;
        --verbose)    VERBOSE=true; shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

log()  { echo "[inject_aer] $*"; }
vlog() { $VERBOSE && echo "[inject_aer] $*" || true; }
err()  { echo "[inject_aer] ERROR: $*" >&2; }

# ----- Preflight checks ----------------------------------------------------

# If backend is explicitly set to "none", skip everything (dry-run for CI).
# This check must come BEFORE the root check so non-root CI can verify the exit code.
if [[ "$BACKEND" == "none" ]]; then
    log "Backend explicitly set to 'none' — DRY-RUN: would inject AER error (type=$ERROR_TYPE)."
    exit 0
fi

if [[ $EUID -ne 0 ]]; then
    err "Must be root"
    exit 1
fi

# ----- Find AER-capable PCIe device ----------------------------------------

find_aer_device() {
    for dev_path in /sys/bus/pci/devices/*/aer_dev_correctable; do
        [[ -f "$dev_path" ]] || continue
        bdf=$(echo "$dev_path" | grep -oP "[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]")
        [[ -n "$bdf" ]] && echo "$bdf" && return 0
    done
    return 1
}

if [[ -z "$DEVICE" ]]; then
    log "No --device specified, searching for AER-capable PCIe devices..."
    if DEVICE=$(find_aer_device); then
        log "Found AER-capable device: $DEVICE"
    else
        log "No AER-capable PCIe devices found. Skipping AER injection."
        exit 2
    fi
fi

AER_CORR_PATH="/sys/bus/pci/devices/${DEVICE}/aer_dev_correctable"
AER_NONFATAL_PATH="/sys/bus/pci/devices/${DEVICE}/aer_dev_nonfatal"
AER_FATAL_PATH="/sys/bus/pci/devices/${DEVICE}/aer_dev_fatal"

if [[ ! -f "$AER_CORR_PATH" ]]; then
    err "Device ${DEVICE} does not have AER capability (no aer_dev_correctable)"
    exit 1
fi

# ----- Detect injection backend --------------------------------------------

detect_backend() {
    # Check for aer-inject module / device
    if [[ -c "/dev/aer-inject" ]] || modinfo aer-inject &>/dev/null; then
        echo "aer-inject"
        return 0
    fi
    # Check for ACPI EINJ
    if [[ -f "/sys/kernel/debug/apei/einj/error_inject" ]]; then
        echo "einj"
        return 0
    fi
    echo "none"
}

if [[ "$BACKEND" == "auto" ]]; then
    BACKEND=$(detect_backend)
    log "Auto-detected backend: $BACKEND"
fi

case "$BACKEND" in
    aer-inject)
        # Load aer-inject module if not already loaded
        if [[ ! -c "/dev/aer-inject" ]]; then
            vlog "Loading aer-inject module..."
            modprobe aer-inject || {
                err "Failed to load aer-inject module. Is it built? (CONFIG_PCIEAER_INJECT=m)"
                exit 1
            }
        fi
        ;;
    einj)
        if [[ ! -f "/sys/kernel/debug/apei/einj/error_inject" ]]; then
            err "ACPI EINJ not available"
            exit 1
        fi
        ;;
    none)
        log "No AER injection backend available."
        log "To enable: build aer-inject (CONFIG_PCIEAER_INJECT=m) or use a platform with ACPI EINJ."
        exit 2
        ;;
    *)
        err "Unknown backend: $BACKEND"
        exit 1
        ;;
esac

# ----- Map error type to hardware bits ------------------------------------

# AER correctable error status bits (PCIe spec §6.2.3.2.1)
declare -A CE_BITS=(
    [ReceiverError]=0x00000001
    [BadTLP]=0x00000040
    [BadDLLP]=0x00000080
    [Rollover]=0x00000100
    [Timeout]=0x00001000
    [NonFatalErr]=0x00002000
    [CorrIntErr]=0x00004000
    [HeaderOF]=0x00008000
)

# AER uncorrectable error status bits (PCIe spec §6.2.3.2.3)
declare -A UCE_BITS=(
    [DataLinkProtocol]=0x00000010
    [SurpriseDown]=0x00000020
    [PoisonedTLP]=0x00001000
    [FlowControl]=0x00002000
    [CompletionTimeout]=0x00004000
    [CompleterAbort]=0x00008000
    [UnexpectedCompletion]=0x00010000
    [ReceiverOverflow]=0x00020000
    [MalformedTLP]=0x00040000
    [ECRCError]=0x00080000
    [UnsupportedRequest]=0x00100000
)

get_ce_bit() {
    echo "${CE_BITS[$1]:-0x00000040}"  # default: BadTLP
}

get_uce_bit() {
    echo "${UCE_BITS[$1]:-0x00004000}"  # default: CompletionTimeout
}

# ----- Read baseline counters ----------------------------------------------

get_error_count() {
    local path="$1"
    local name="$2"
    grep -E "^${name} " "$path" 2>/dev/null | awk '{print $2}' || echo "0"
}

case "$ERROR_TYPE" in
    correctable)
        BASELINE=$(get_error_count "$AER_CORR_PATH" "$ERROR_NAME")
        vlog "Baseline ${ERROR_NAME} correctable: $BASELINE"
        ;;
    nonfatal)
        BASELINE=$(get_error_count "$AER_NONFATAL_PATH" "$ERROR_NAME")
        ;;
    fatal)
        BASELINE=$(get_error_count "$AER_FATAL_PATH" "$ERROR_NAME")
        ;;
esac

BASELINE_TOTAL=$(cat "$AER_CORR_PATH" | grep TOTAL_ERR_COR | awk '{print $2}' 2>/dev/null || echo "0")
DMESG_OFFSET=$(dmesg | wc -l)

# ----- Inject --------------------------------------------------------------

log "Injecting ${ERROR_TYPE} AER error '${ERROR_NAME}' on device ${DEVICE} via ${BACKEND}"

case "$BACKEND" in
    aer-inject)
        case "$ERROR_TYPE" in
            correctable)
                CE_STATUS=$(get_ce_bit "$ERROR_NAME")
                # Write injection request to /dev/aer-inject
                # Format: binary struct aer_error_inj (see kernel aer_inject.c)
                # Use the aer-inject userspace tool if available, otherwise write directly
                if command -v aer-inject &>/dev/null; then
                    aer-inject --id "${DEVICE}" \
                                --error correctable \
                                --ce_status "${CE_STATUS}" \
                        && log "aer-inject tool invoked successfully" \
                        || { err "aer-inject tool failed"; exit 1; }
                else
                    # Fallback: write raw injection structure via Python
                    python3 - <<PYEOF
import struct, os
# struct aer_error_inj: id[13], pad[3], ce_status, ue_status, severity, header_log[4×4]
bdf = "${DEVICE}".encode() + b'\x00' * (13 - len("${DEVICE}"))
ce_status = ${CE_STATUS}
ue_status = 0
severity = 0  # correctable
hdr = (0, 0, 0, 0)
packed = struct.pack('<13s3sIII4I', bdf[:13], b'\x00'*3,
                    ce_status, ue_status, severity, *hdr)
with open('/dev/aer-inject', 'wb') as f:
    f.write(packed)
PYEOF
                    log "Raw AER injection written to /dev/aer-inject"
                fi
                ;;
            nonfatal|fatal)
                UCE_STATUS=$(get_uce_bit "$ERROR_NAME")
                SEVERITY=$([[ "$ERROR_TYPE" == "fatal" ]] && echo "1" || echo "0")
                if command -v aer-inject &>/dev/null; then
                    aer-inject --id "${DEVICE}" \
                                --error "${ERROR_TYPE}" \
                                --ue_status "${UCE_STATUS}" \
                                --severity "${SEVERITY}"
                else
                    python3 - <<PYEOF
import struct
bdf = "${DEVICE}".encode() + b'\x00' * (13 - len("${DEVICE}"))
packed = struct.pack('<13s3sIII4I', bdf[:13], b'\x00'*3,
                    0, ${UCE_STATUS}, ${SEVERITY}, 0, 0, 0, 0)
with open('/dev/aer-inject', 'wb') as f:
    f.write(packed)
PYEOF
                fi
                ;;
        esac
        ;;

    einj)
        # ACPI EINJ error type codes
        case "$ERROR_TYPE" in
            correctable) EINJ_TYPE=0x00000008 ;;  # PCIe correctable
            nonfatal)    EINJ_TYPE=0x00000010 ;;  # PCIe uncorrectable non-fatal
            fatal)       EINJ_TYPE=0x00000020 ;;  # PCIe uncorrectable fatal
        esac
        EINJ_ROOT="/sys/kernel/debug/apei/einj"
        printf "0x%08x\n" "$EINJ_TYPE" > "${EINJ_ROOT}/error_type"
        echo 0 > "${EINJ_ROOT}/flags"
        echo 1 > "${EINJ_ROOT}/error_inject"
        log "EINJ injection triggered (type=0x$(printf '%08x' $EINJ_TYPE))"
        ;;
esac

# Wait for kernel AER handler
sleep 2

# ----- Verification --------------------------------------------------------

if ! $VERIFY; then
    log "Skipping verification (--no-verify)"
    exit 0
fi

FAIL=0

# Verify AER counter incremented
case "$ERROR_TYPE" in
    correctable)
        NEW_COUNT=$(get_error_count "$AER_CORR_PATH" "$ERROR_NAME")
        NEW_TOTAL=$(cat "$AER_CORR_PATH" | grep TOTAL_ERR_COR | awk '{print $2}' 2>/dev/null || echo "0")
        DELTA=$(( NEW_COUNT - BASELINE ))
        DELTA_TOTAL=$(( NEW_TOTAL - BASELINE_TOTAL ))
        if [[ "$DELTA" -ge 1 || "$DELTA_TOTAL" -ge 1 ]]; then
            log "PASS: AER correctable counter incremented (${ERROR_NAME}: $BASELINE → $NEW_COUNT)"
        else
            err "FAIL: AER correctable counter did not increment"
            FAIL=$((FAIL+1))
        fi
        ;;
    nonfatal|fatal)
        NEW_COUNT=$(get_error_count "$AER_NONFATAL_PATH" "$ERROR_NAME" || echo "0")
        DELTA=$(( NEW_COUNT - BASELINE ))
        if [[ "$DELTA" -ge 1 ]]; then
            log "PASS: AER non-fatal counter incremented"
        else
            err "FAIL: AER non-fatal counter did not increment (delta=$DELTA)"
            FAIL=$((FAIL+1))
        fi
        ;;
esac

# Verify dmesg AER log entry
if dmesg | tail -n +$((DMESG_OFFSET+1)) | grep -qiE "AER|pcie.*error|aer_dev"; then
    log "PASS: AER event found in dmesg"
else
    err "FAIL: No AER log entry found in dmesg"
    FAIL=$((FAIL+1))
fi

if [[ $FAIL -gt 0 ]]; then
    err "AER injection verification: $FAIL check(s) failed"
    exit 1
fi

log "AER injection complete — all checks passed"
exit 0
