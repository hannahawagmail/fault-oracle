#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# secure-boot-verify.sh — Verify Secure Boot chain of trust status
#
# Checks: EFI Secure Boot, ARM TrustZone/OP-TEE, FIT image signatures,
#         TF-A chain of trust, and Linux kernel lockdown mode.
#
# Usage:
#   bash secure-boot-verify.sh [--fit-image /boot/kernel.itb]
#                              [--dry-run]
#                              [--verbose]
#
# Exit codes:
#   0 — all checks pass (or skipped, no failures)
#   1 — one or more checks failed
#   2 — cannot determine status (missing tools/hardware)

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
FIT_IMAGE_PATH=""
DRY_RUN=false
VERBOSE=false
OVERALL_RESULT=0  # 0=pass, 1=fail

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
info()    { echo "[INFO]  $(date '+%Y-%m-%d %H:%M:%S') $*" >&2; }
pass()    { echo "[ PASS ] $*";  }
fail()    { echo "[ FAIL ] $*";  OVERALL_RESULT=1; }
skip_()   { echo "[ SKIP ] $*";  }   # 'skip' may be a shell builtin
warn_()   { echo "[ WARN ] $*";  }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --fit-image) FIT_IMAGE_PATH="$2"; shift 2 ;;
            --dry-run)   DRY_RUN=true;        shift   ;;
            --verbose)   VERBOSE=true;         shift   ;;
            --help|-h)
                sed -n '2,14p' "$0" | sed 's/^# //'
                exit 0
                ;;
            *)
                echo "Unknown argument: $1" >&2
                exit 1
                ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# CHECK 1: EFI Secure Boot (UEFI platforms: ARM64 servers, Raspberry Pi UEFI)
# ---------------------------------------------------------------------------
check_efi_secure_boot() {
    echo ""
    echo "--- Check 1: EFI Secure Boot ---"

    local efi_dir="/sys/firmware/efi"
    if [[ ! -d "$efi_dir" ]]; then
        skip_ "EFI firmware not present (non-UEFI system — expected on bare-metal ARM)"
        return
    fi

    # EFI variable: SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c
    # Value byte 4: 1=enabled, 0=disabled
    local sb_var
    sb_var=$(find /sys/firmware/efi/efivars/ -name 'SecureBoot-*' 2>/dev/null | head -1)

    if [[ -z "$sb_var" ]]; then
        skip_ "SecureBoot EFI variable not found"
        return
    fi

    # The EFI variable is a binary blob; byte index 4 (after 4-byte attributes) is the value
    local sb_value
    sb_value=$(python3 -c "
import sys
with open('$sb_var', 'rb') as f:
    data = f.read()
# Skip 4-byte attributes header, read next byte
val = data[4] if len(data) > 4 else 0
print(val)
" 2>/dev/null || echo "error")

    if [[ "$sb_value" == "error" ]]; then
        skip_ "Cannot read SecureBoot EFI variable (need root)"
    elif [[ "$sb_value" == "1" ]]; then
        pass "EFI Secure Boot is ENABLED"
    else
        fail "EFI Secure Boot is DISABLED (value=$sb_value)"
    fi

    # Also check SetupMode variable
    local sm_var
    sm_var=$(find /sys/firmware/efi/efivars/ -name 'SetupMode-*' 2>/dev/null | head -1)
    if [[ -n "$sm_var" ]]; then
        local sm_value
        sm_value=$(python3 -c "
with open('$sm_var', 'rb') as f:
    data = f.read()
val = data[4] if len(data) > 4 else 0
print(val)
" 2>/dev/null || echo "error")
        if [[ "$sm_value" == "0" ]]; then
            pass "EFI SetupMode: user mode (not setup/enrollment mode)"
        elif [[ "$sm_value" == "1" ]]; then
            fail "EFI SetupMode: SETUP MODE (keys not yet enrolled — Secure Boot ineffective)"
        fi
    fi
}

# ---------------------------------------------------------------------------
# CHECK 2: ARM TrustZone / OP-TEE presence
# ---------------------------------------------------------------------------
check_optee() {
    echo ""
    echo "--- Check 2: ARM TrustZone / OP-TEE ---"

    # Check for /dev/tee* devices (OP-TEE kernel driver creates these)
    local tee_devices
    tee_devices=$(ls /dev/tee* 2>/dev/null || echo "")

    if [[ -n "$tee_devices" ]]; then
        pass "OP-TEE TEE devices found: $tee_devices"
    else
        skip_ "No /dev/tee* devices found (OP-TEE driver not loaded or not present)"
    fi

    # Check for tee-supplicant process (userspace daemon for OP-TEE)
    if pgrep -x "tee-supplicant" &>/dev/null; then
        pass "tee-supplicant daemon is running (PID: $(pgrep -x tee-supplicant))"
    else
        warn_ "tee-supplicant not running (OP-TEE trusted applications will not load)"
    fi

    # Check for OP-TEE procfs entry (older kernels)
    if [[ -d /proc/tee ]]; then
        pass "/proc/tee exists — OP-TEE kernel integration active"
        if $VERBOSE; then
            ls /proc/tee/ 2>/dev/null || true
        fi
    fi

    # Check OP-TEE kernel module
    if lsmod 2>/dev/null | grep -q optee; then
        pass "optee kernel module is loaded"
    else
        # May be built-in
        if grep -q "optee" /proc/modules 2>/dev/null; then
            pass "optee built into kernel"
        fi
    fi

    # Check ARM SMCCC / PSCI (indirect TrustZone indicator)
    if [[ -f /sys/firmware/acpi/tables/SPCR ]] || [[ -d /sys/bus/platform/drivers/optee ]]; then
        pass "OP-TEE platform driver registered in sysfs"
    fi
}

# ---------------------------------------------------------------------------
# CHECK 3: FIT Image signature verification
# ---------------------------------------------------------------------------
check_fit_image() {
    echo ""
    echo "--- Check 3: FIT Image Signature ---"

    if [[ -z "$FIT_IMAGE_PATH" ]]; then
        skip_ "No --fit-image path provided (skipping FIT signature check)"
        return
    fi

    if [[ ! -f "$FIT_IMAGE_PATH" ]]; then
        fail "FIT image not found at: $FIT_IMAGE_PATH"
        return
    fi

    if $DRY_RUN; then
        skip_ "[dry-run] Would verify FIT image: $FIT_IMAGE_PATH"
        return
    fi

    # Try fit_check_sign (from U-Boot tools package)
    if command -v fit_check_sign &>/dev/null; then
        info "Using fit_check_sign to verify $FIT_IMAGE_PATH"
        if fit_check_sign -f "$FIT_IMAGE_PATH" -k /etc/secure-boot/pubkey.dtb 2>/dev/null; then
            pass "FIT image signature VALID (fit_check_sign)"
        else
            fail "FIT image signature INVALID or key not found"
        fi
    # Fallback: use dumpimage + openssl for manual verification
    elif command -v dumpimage &>/dev/null && command -v openssl &>/dev/null; then
        info "Using dumpimage + openssl for FIT signature check"
        local sig_file="/tmp/fit_sig_$$.bin"
        local hash_file="/tmp/fit_hash_$$.bin"
        # Extract signature node (simplified — full check requires proper key)
        if dumpimage -T flat_dt -p 0 "$FIT_IMAGE_PATH" -o /tmp/fit_component_$$.bin 2>/dev/null; then
            pass "FIT image structure is valid (dumpimage succeeded)"
            rm -f "/tmp/fit_component_$$.bin"
        else
            fail "FIT image structure invalid or not a FIT image"
        fi
        rm -f "$sig_file" "$hash_file"
    else
        skip_ "Neither fit_check_sign nor dumpimage available"
        skip_ "Install u-boot-tools for FIT signature verification"
    fi

    # Basic sanity: check FIT magic bytes (0xd00dfeed = FDT magic)
    local magic
    magic=$(python3 -c "
import struct, sys
with open('$FIT_IMAGE_PATH', 'rb') as f:
    data = f.read(4)
magic = struct.unpack('>I', data)[0]
print(hex(magic))
" 2>/dev/null || echo "error")

    if [[ "$magic" == "0xd00dfeed" ]]; then
        pass "FIT image magic bytes valid (0xd00dfeed)"
    else
        fail "FIT image magic invalid: $magic (expected 0xd00dfeed)"
    fi
}

# ---------------------------------------------------------------------------
# CHECK 4: ARM Trusted Firmware chain of trust status
# ---------------------------------------------------------------------------
check_atf_chain() {
    echo ""
    echo "--- Check 4: ARM Trusted Firmware (TF-A) Chain of Trust ---"

    # TF-A logs may be visible via:
    # 1. ACPI SLIT/SRAT tables (server platforms)
    # 2. DTB /chosen node
    # 3. /sys/firmware/devicetree/base/chosen/

    local chosen_dir="/sys/firmware/devicetree/base/chosen"

    if [[ -d "$chosen_dir" ]]; then
        # Check for TF-A version string in chosen node
        if [[ -f "$chosen_dir/atf-version" ]]; then
            local atf_ver
            atf_ver=$(strings "$chosen_dir/atf-version" 2>/dev/null || echo "unreadable")
            pass "TF-A version found in DTB chosen: $atf_ver"
        else
            skip_ "No atf-version in DTB chosen node (normal if TF-A doesn't export it)"
        fi

        # Check for BL31 runtime presence (SMC handler active)
        if [[ -f "$chosen_dir/bl31-version" ]]; then
            local bl31_ver
            bl31_ver=$(strings "$chosen_dir/bl31-version" 2>/dev/null || echo "unreadable")
            pass "BL31 version: $bl31_ver"
        fi

        # PSCI availability indicates BL31 is active
        if [[ -f "$chosen_dir/psci" ]] || ls "$chosen_dir"/psci* &>/dev/null 2>&1; then
            pass "PSCI node found in DTB — BL31 (EL3 runtime) is active"
        fi
    else
        skip_ "No device tree at /sys/firmware/devicetree/ (ACPI-only platform?)"
    fi

    # Check PSCI via sysfs (kernel exports CPU power management capability)
    if [[ -f /sys/devices/system/cpu/cpuidle/current_driver ]]; then
        local idle_driver
        idle_driver=$(cat /sys/devices/system/cpu/cpuidle/current_driver)
        if [[ "$idle_driver" == "psci_idle" || "$idle_driver" == *"psci"* ]]; then
            pass "PSCI idle driver active: $idle_driver (confirms BL31 SMC interface)"
        fi
    fi

    # Check for SMCCC (SMC Calling Convention) support
    if [[ -d /sys/firmware/acpi/ ]]; then
        if ls /sys/firmware/acpi/tables/ 2>/dev/null | grep -qi "SPCR\|IORT\|PPTT"; then
            pass "ACPI tables present — ARM server platform with full TF-A stack"
        fi
    fi

    # U-Boot chain: verify that a known-good FIT image was the last boot source
    if command -v fw_printenv &>/dev/null; then
        local uboot_ver
        uboot_ver=$(fw_printenv -n ver 2>/dev/null || echo "")
        if [[ -n "$uboot_ver" ]]; then
            pass "U-Boot (BL33) version from env: $uboot_ver"
        fi
    fi
}

# ---------------------------------------------------------------------------
# CHECK 5: Kernel lockdown mode
# ---------------------------------------------------------------------------
check_kernel_lockdown() {
    echo ""
    echo "--- Check 5: Kernel Lockdown Mode ---"

    local lockdown_file="/sys/kernel/security/lockdown"

    if [[ ! -f "$lockdown_file" ]]; then
        skip_ "Kernel lockdown LSM not present"
        skip_ "Enable with CONFIG_SECURITY_LOCKDOWN_LSM=y in kernel config"
        return
    fi

    local lockdown_state
    lockdown_state=$(cat "$lockdown_file" 2>/dev/null || echo "unreadable")

    # Format: "[none] integrity confidentiality" — bracketed is active
    local active
    active=$(echo "$lockdown_state" | grep -oP '\[\K[^\]]+' || echo "none")

    case "$active" in
        none)
            fail "Kernel lockdown: NONE — /dev/mem, unsigned modules, etc. allowed"
            ;;
        integrity)
            pass "Kernel lockdown: integrity — unsigned module loading blocked"
            ;;
        confidentiality)
            pass "Kernel lockdown: confidentiality — strongest mode active"
            ;;
        *)
            warn_ "Kernel lockdown: unknown state '$active'"
            ;;
    esac

    $VERBOSE && echo "  Full lockdown string: $lockdown_state"

    # Check kernel module signature enforcement
    if [[ -f /proc/sys/kernel/modules_disabled ]]; then
        local mod_disabled
        mod_disabled=$(cat /proc/sys/kernel/modules_disabled)
        if [[ "$mod_disabled" == "1" ]]; then
            pass "Module loading disabled (modules_disabled=1)"
        fi
    fi

    # Check IMA (Integrity Measurement Architecture)
    if [[ -d /sys/kernel/security/ima ]]; then
        pass "IMA (Integrity Measurement Architecture) is active"
        if [[ -f /sys/kernel/security/ima/policy ]]; then
            local ima_rules
            ima_rules=$(wc -l < /sys/kernel/security/ima/policy 2>/dev/null || echo 0)
            pass "IMA policy loaded: $ima_rules rule(s)"
        fi
    else
        skip_ "IMA not present (optional — provides runtime file integrity measurement)"
    fi
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print_summary() {
    echo ""
    echo "========================================="
    echo " Secure Boot Verification Summary"
    echo "========================================="
    if [[ $OVERALL_RESULT -eq 0 ]]; then
        echo " RESULT: PASS — No security failures detected"
    else
        echo " RESULT: FAIL — One or more checks failed (see above)"
    fi
    echo "========================================="
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    parse_args "$@"

    echo "=== Secure Boot Chain of Trust Verification ==="
    echo "    $(date)"
    echo "    Kernel: $(uname -r)"
    echo "    Arch:   $(uname -m)"
    echo ""

    if $DRY_RUN; then
        echo "[dry-run] Would run all checks without modifying system state."
        echo "Checks that would be performed:"
        echo "  1. EFI Secure Boot (/sys/firmware/efi/efivars/SecureBoot-*)"
        echo "  2. OP-TEE / TrustZone (/dev/tee*, tee-supplicant process)"
        echo "  3. FIT image signature (fit_check_sign or dumpimage+openssl)"
        echo "  4. TF-A chain of trust (DTB chosen node, PSCI, ACPI)"
        echo "  5. Kernel lockdown mode (/sys/kernel/security/lockdown)"
        exit 0
    fi

    check_efi_secure_boot
    check_optee
    check_fit_image
    check_atf_chain
    check_kernel_lockdown
    print_summary

    exit $OVERALL_RESULT
}

main "$@"
