#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# compat/cloud-probe.sh — Auto-detect cloud ARM64 instance type and print
# which hw-fault-exporter collectors will be active.
#
# Usage:
#   bash compat/cloud-probe.sh [--json]

set -euo pipefail
JSON_OUT=false
[[ "${1:-}" == "--json" ]] && JSON_OUT=true

detect_instance() {
    # Try DMI product name (available on bare-metal and some VMs)
    if [[ -f /sys/class/dmi/id/product_name ]]; then
        cat /sys/class/dmi/id/product_name 2>/dev/null || echo "unknown"
    else
        echo "unknown"
    fi
}

check_sysfs() {
    local path="$1"
    [[ -e "${path}" ]] && echo "1" || echo "0"
}

PRODUCT="$(detect_instance)"
EDAC="$(check_sysfs /sys/devices/system/edac/mc)"
MCE="$(check_sysfs /sys/firmware/acpi/errors)"
AER="$(check_sysfs /sys/bus/pci/devices)"
THERMAL="$(check_sysfs /sys/class/thermal/thermal_zone0)"
CPUFREQ="$(check_sysfs /sys/devices/system/cpu/cpu0/cpufreq)"
PMU_CMN="$(check_sysfs /sys/bus/event_source/devices/arm_cmn_0)"
PMU_DSU="$(check_sysfs /sys/bus/event_source/devices/arm_dsu_0)"
PMU=$(( PMU_CMN | PMU_DSU ))

if "${JSON_OUT}"; then
    cat << JSONOUT
{
  "product": "${PRODUCT}",
  "collectors": {
    "edac":    ${EDAC},
    "mce":     ${MCE},
    "aer":     ${AER},
    "thermal": ${THERMAL},
    "cpufreq": ${CPUFREQ},
    "pmu":     ${PMU}
  }
}
JSONOUT
else
    echo "=== hw-fault-exporter Cloud Probe ==="
    echo "  Product:  ${PRODUCT}"
    echo "  EDAC:     $([ "${EDAC}" = "1" ] && echo AVAILABLE || echo absent)"
    echo "  MCE:      $([ "${MCE}"  = "1" ] && echo AVAILABLE || echo absent)"
    echo "  AER:      $([ "${AER}"  = "1" ] && echo AVAILABLE || echo absent)"
    echo "  Thermal:  $([ "${THERMAL}" = "1" ] && echo AVAILABLE || echo absent)"
    echo "  cpufreq:  $([ "${CPUFREQ}" = "1" ] && echo AVAILABLE || echo absent)"
    echo "  PMU:      $([ "${PMU}"  = "1" ] && echo AVAILABLE || echo absent)"
    echo ""
    if [[ "${EDAC}" = "0" && "${MCE}" = "0" ]]; then
        echo "  Note: EDAC/MCE absent — likely running on a cloud VM."
        echo "  Recommended flags: --no-edac --no-mce --no-aer --log-level=warn"
    fi
fi

# ── eBPF availability check ───────────────────────────────────────────────────
check_ebpf() {
    local btf="/sys/kernel/btf/vmlinux"
    local ver
    ver=$(uname -r | cut -d. -f1-2 | tr -d '[:alpha:]')
    local maj minor
    maj=$(echo "$ver" | cut -d. -f1)
    min=$(echo "$ver" | cut -d. -f2)

    if [ ! -f "$btf" ]; then
        echo "absent"
        return
    fi
    if [ "$maj" -lt 5 ] || { [ "$maj" -eq 5 ] && [ "$min" -lt 8 ]; }; then
        echo "absent"
        return
    fi
    # Check ras tracepoints exist
    if ls /sys/kernel/debug/tracing/events/ras/ >/dev/null 2>&1; then
        echo "AVAILABLE"
    else
        echo "absent"
    fi
}
