#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# dpu-setup.sh — Set up hw-fault-exporter and monitoring on DPU control plane
#
# Run this script ON the DPU ARM Linux OS (not the host).
# Access: ssh admin@<dpu-management-ip>   or   serial console on DPU mgmt port
#
# Compatible with: NVIDIA BlueField-3, Marvell OCTEON 10, AMD Pensando DSC-200
#
# Usage:
#   bash dpu-setup.sh [--detect] [--install-exporter] [--status]
#                     [--configure-scrape <host-ip>] [--run-edac-test] [--dry-run]
#
# Examples:
#   bash dpu-setup.sh --detect
#   bash dpu-setup.sh --dry-run --install-exporter
#   bash dpu-setup.sh --configure-scrape 10.0.0.1
#   bash dpu-setup.sh --status
#   bash dpu-setup.sh --run-edac-test

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
EXPORTER_BINARY_URL="https://github.com/hanna-hawa/fault-oracle/releases/latest/download/hw-fault-exporter-linux-arm64"
EXPORTER_INSTALL_DIR="/opt/hw-fault-exporter"
EXPORTER_BINARY="${EXPORTER_INSTALL_DIR}/hw-fault-exporter"
SERVICE_NAME="hw-fault-exporter-dpu"
SERVICE_FILE_SRC="$(dirname "$0")/dpu-fault-exporter.service"
SERVICE_FILE_DST="/etc/systemd/system/${SERVICE_NAME}.service"
EXPORTER_LISTEN_PORT=9200

# Known DPU vendor IDs (PCI)
VENDOR_NVIDIA="0x15b3"   # NVIDIA / Mellanox (BlueField)
VENDOR_PENSANDO="0x1dd8" # Pensando / AMD (DSC-200)
VENDOR_MARVELL="0x177d"  # Marvell (OCTEON 10)

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
DRY_RUN=false

# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------
log_info()  { echo "[INFO]  $*"; }
log_warn()  { echo "[WARN]  $*" >&2; }
log_error() { echo "[ERROR] $*" >&2; }

# run_cmd: print command; in --dry-run mode only print, otherwise execute.
run_cmd() {
    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "[DRY-RUN] $*"
    else
        "$@"
    fi
}

# graceful_skip: print message and exit 2 when a feature is unavailable.
graceful_skip() {
    log_warn "$*"
    log_warn "Skipping — exit code 2 (feature unavailable)."
    exit 2
}

# require_root: most install operations need root.
require_root() {
    if [[ "${EUID}" -ne 0 ]] && [[ "${DRY_RUN}" == "false" ]]; then
        log_error "This operation requires root. Re-run with sudo or as root."
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# --detect
# ---------------------------------------------------------------------------
cmd_detect() {
    log_info "=== DPU Detection ==="

    # CPU model
    local cpu_model
    cpu_model=$(grep -m1 "model name" /proc/cpuinfo 2>/dev/null | cut -d: -f2 | xargs || echo "unknown")
    log_info "CPU model     : ${cpu_model}"

    # Core count
    local core_count
    core_count=$(nproc 2>/dev/null || grep -c "^processor" /proc/cpuinfo 2>/dev/null || echo "unknown")
    log_info "ARM cores     : ${core_count}"

    # DRAM
    local mem_total
    mem_total=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{printf "%.1f GB", $2/1048576}' || echo "unknown")
    log_info "DRAM total    : ${mem_total}"

    # OS version
    local os_ver
    os_ver=$(grep PRETTY_NAME /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '"' || echo "unknown")
    log_info "OS version    : ${os_ver}"

    # DPU vendor detection via PCI
    local found_vendor="unknown"
    local found_device_path=""
    if [[ -d /sys/bus/pci/devices ]]; then
        for dev_path in /sys/bus/pci/devices/*/vendor; do
            [[ -f "${dev_path}" ]] || continue
            local vid
            vid=$(cat "${dev_path}" 2>/dev/null || true)
            case "${vid}" in
                "${VENDOR_NVIDIA}")
                    found_vendor="NVIDIA / Mellanox (BlueField)"
                    found_device_path="${dev_path%/vendor}"
                    ;;
                "${VENDOR_PENSANDO}")
                    found_vendor="AMD Pensando (DSC-200)"
                    found_device_path="${dev_path%/vendor}"
                    ;;
                "${VENDOR_MARVELL}")
                    found_vendor="Marvell (OCTEON 10)"
                    found_device_path="${dev_path%/vendor}"
                    ;;
            esac
        done
    fi

    log_info "DPU vendor    : ${found_vendor}"
    if [[ -n "${found_device_path}" ]]; then
        log_info "PCI device    : ${found_device_path##*/}"
    fi

    # Sanity check: if we are not on arm64, warn that DPU features may be absent.
    local arch
    arch=$(uname -m 2>/dev/null || echo "unknown")
    log_info "Architecture  : ${arch}"
    if [[ "${arch}" != "aarch64" ]] && [[ "${arch}" != "arm64" ]]; then
        log_warn "Architecture is '${arch}', not arm64. This host may not be a DPU."
        return 0
    fi

    log_info "Detection complete."
}

# ---------------------------------------------------------------------------
# --install-exporter
# ---------------------------------------------------------------------------
cmd_install_exporter() {
    require_root

    # Verify architecture
    local arch
    arch=$(uname -m 2>/dev/null || echo "unknown")
    if [[ "${arch}" != "aarch64" ]] && [[ "${arch}" != "arm64" ]]; then
        graceful_skip "Architecture is '${arch}'; this installer is for arm64 DPU only."
    fi

    log_info "=== Installing hw-fault-exporter on DPU ==="

    # Create install directory
    run_cmd mkdir -p "${EXPORTER_INSTALL_DIR}"

    # Download binary
    log_info "Downloading exporter binary from:"
    log_info "  ${EXPORTER_BINARY_URL}"
    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "[DRY-RUN] curl -fsSL '${EXPORTER_BINARY_URL}' -o '${EXPORTER_BINARY}'"
    else
        if command -v curl &>/dev/null; then
            curl -fsSL "${EXPORTER_BINARY_URL}" -o "${EXPORTER_BINARY}"
        elif command -v wget &>/dev/null; then
            wget -q "${EXPORTER_BINARY_URL}" -O "${EXPORTER_BINARY}"
        else
            log_error "Neither curl nor wget is available. Cannot download binary."
            exit 1
        fi
        chmod 0755 "${EXPORTER_BINARY}"
        log_info "Binary installed to ${EXPORTER_BINARY}"
    fi

    # Copy service file
    if [[ -f "${SERVICE_FILE_SRC}" ]]; then
        run_cmd cp "${SERVICE_FILE_SRC}" "${SERVICE_FILE_DST}"
        log_info "Service file installed to ${SERVICE_FILE_DST}"
    else
        log_warn "Service file not found at ${SERVICE_FILE_SRC}; skipping copy."
        log_warn "Copy dpu-fault-exporter.service manually to ${SERVICE_FILE_DST}"
    fi

    # Reload systemd and enable service
    run_cmd systemctl daemon-reload
    run_cmd systemctl enable --now "${SERVICE_NAME}"

    log_info "Service '${SERVICE_NAME}' enabled and started."
    log_info "Check status: systemctl status ${SERVICE_NAME}"
    log_info "View metrics: curl http://localhost:${EXPORTER_LISTEN_PORT}/metrics"
}

# ---------------------------------------------------------------------------
# --status
# ---------------------------------------------------------------------------
cmd_status() {
    log_info "=== hw-fault-exporter DPU Status ==="

    # Service status
    if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
        log_info "Service '${SERVICE_NAME}': RUNNING"
        systemctl status "${SERVICE_NAME}" --no-pager --lines=10 || true
    elif systemctl list-unit-files "${SERVICE_NAME}.service" &>/dev/null 2>&1; then
        log_warn "Service '${SERVICE_NAME}': INSTALLED but not running"
        systemctl status "${SERVICE_NAME}" --no-pager --lines=5 || true
    else
        log_warn "Service '${SERVICE_NAME}': NOT INSTALLED"
    fi

    echo ""
    log_info "=== EDAC Controller Status ==="

    local mc_count
    mc_count=$(ls /sys/devices/system/edac/mc/ 2>/dev/null | wc -l || echo 0)
    log_info "EDAC memory controllers: ${mc_count}"

    if [[ "${mc_count}" -gt 0 ]]; then
        log_info "Correctable errors (CE):"
        for f in /sys/devices/system/edac/mc*/ce_count; do
            [[ -f "${f}" ]] || continue
            local mc_dir="${f%/ce_count}"
            local mc_name="${mc_dir##*/}"
            local ce_val
            ce_val=$(cat "${f}" 2>/dev/null || echo "N/A")
            log_info "  ${mc_name}/ce_count = ${ce_val}"
        done

        log_info "Uncorrectable errors (UE):"
        for f in /sys/devices/system/edac/mc*/ue_count; do
            [[ -f "${f}" ]] || continue
            local mc_dir="${f%/ue_count}"
            local mc_name="${mc_dir##*/}"
            local ue_val
            ue_val=$(cat "${f}" 2>/dev/null || echo "N/A")
            log_info "  ${mc_name}/ue_count = ${ue_val}"
        done
    else
        log_warn "No EDAC controllers found. EDAC module may not be loaded."
        log_warn "Load with: insmod edac_cortex_ref.ko nr_csrows=2 nr_channels=2"
    fi

    echo ""
    log_info "=== Prometheus Metrics ==="

    if command -v curl &>/dev/null; then
        local metric_count
        metric_count=$(curl -s --max-time 5 "http://localhost:${EXPORTER_LISTEN_PORT}/metrics" 2>/dev/null \
            | grep -c "^edac_" || echo 0)
        log_info "EDAC metrics exposed: ${metric_count}"
    else
        log_warn "curl not available; cannot check metrics endpoint."
    fi
}

# ---------------------------------------------------------------------------
# --configure-scrape <host-ip>
# ---------------------------------------------------------------------------
cmd_configure_scrape() {
    local host_ip="${1:-}"
    if [[ -z "${host_ip}" ]]; then
        log_error "--configure-scrape requires <host-ip> argument."
        exit 1
    fi

    # Determine DPU management IP from routing table
    local dpu_mgmt_ip
    dpu_mgmt_ip=$(ip route get "${host_ip}" 2>/dev/null \
        | grep -oP 'src \K[\d.]+' | head -1 || true)
    if [[ -z "${dpu_mgmt_ip}" ]]; then
        # Fallback: use first non-loopback IP
        dpu_mgmt_ip=$(ip -4 addr show 2>/dev/null \
            | grep -oP '(?<=inet )\d+\.\d+\.\d+\.\d+' \
            | grep -v "^127\." | head -1 || echo "DPU_MGMT_IP")
    fi

    log_info "DPU management IP detected: ${dpu_mgmt_ip}"
    log_info "Host IP (scrape target):    ${host_ip}"
    echo ""
    cat <<EOF
# Add to host prometheus.yml scrape_configs:
- job_name: 'dpu-fault-exporter'
  static_configs:
    - targets: ['${dpu_mgmt_ip}:${EXPORTER_LISTEN_PORT}']
  labels:
    role: 'dpu'
    host_ip: '${host_ip}'
EOF
}

# ---------------------------------------------------------------------------
# --run-edac-test
# ---------------------------------------------------------------------------
cmd_run_edac_test() {
    log_info "=== EDAC Integration Test on DPU ==="

    # Locate the reference kernel module
    local module_path=""
    local script_dir
    script_dir="$(dirname "$(realpath "$0")")"

    if [[ -f "${script_dir}/edac_cortex_ref.ko" ]]; then
        module_path="${script_dir}/edac_cortex_ref.ko"
    elif [[ -f "${script_dir}/../edac-reference/edac_cortex_ref.ko" ]]; then
        module_path="${script_dir}/../edac-reference/edac_cortex_ref.ko"
    fi

    if [[ -z "${module_path}" ]]; then
        graceful_skip "edac_cortex_ref.ko not found in '${script_dir}' or '${script_dir}/../edac-reference/'. Build it first."
    fi

    log_info "Found module: ${module_path}"

    # Load module
    log_info "Loading edac_cortex_ref.ko ..."
    if ! insmod "${module_path}" nr_csrows=2 nr_channels=2; then
        log_error "insmod failed. Check dmesg for details."
        exit 1
    fi

    # Run verification script
    local verify_script="${script_dir}/../edac-reference/test/verify_edac_sysfs.sh"
    local test_result=0
    if [[ -f "${verify_script}" ]]; then
        log_info "Running ${verify_script} --inject ..."
        if bash "${verify_script}" --inject; then
            log_info "PASS: EDAC sysfs verification succeeded."
        else
            log_error "FAIL: EDAC sysfs verification failed."
            test_result=1
        fi
    else
        log_warn "Verification script not found at ${verify_script}."
        log_warn "Skipping injection test — checking sysfs manually."
        if [[ -d /sys/devices/system/edac/mc0 ]]; then
            log_info "PASS: /sys/devices/system/edac/mc0 exists after insmod."
        else
            log_error "FAIL: /sys/devices/system/edac/mc0 not found after insmod."
            test_result=1
        fi
    fi

    # Unload module
    log_info "Removing edac_cortex_ref module ..."
    rmmod edac_cortex_ref 2>/dev/null || log_warn "rmmod failed; module may already be unloaded."

    if [[ "${test_result}" -eq 0 ]]; then
        log_info "EDAC test: PASS"
    else
        log_error "EDAC test: FAIL"
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
    grep "^# Usage:" "$0" -A3 | sed 's/^# //'
    echo ""
    echo "Options:"
    echo "  --detect                   Identify DPU hardware, ARM cores, DRAM, OS"
    echo "  --install-exporter         Install hw-fault-exporter binary and service"
    echo "  --status                   Show exporter, EDAC, and metrics status"
    echo "  --configure-scrape <ip>    Print Prometheus scrape config for host at <ip>"
    echo "  --run-edac-test            Load reference EDAC module, run injection test"
    echo "  --dry-run                  Print commands without executing (for --install-*)"
    echo "  -h, --help                 Show this help"
}

# ---------------------------------------------------------------------------
# Main argument parsing
# ---------------------------------------------------------------------------
if [[ $# -eq 0 ]]; then
    usage
    exit 0
fi

ACTION=""
SCRAPE_HOST_IP=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --detect)
            ACTION="detect"
            shift
            ;;
        --install-exporter)
            ACTION="install-exporter"
            shift
            ;;
        --status)
            ACTION="status"
            shift
            ;;
        --configure-scrape)
            ACTION="configure-scrape"
            if [[ -n "${2:-}" ]] && [[ "${2}" != --* ]]; then
                SCRAPE_HOST_IP="$2"
                shift 2
            else
                log_error "--configure-scrape requires <host-ip> argument."
                exit 1
            fi
            ;;
        --run-edac-test)
            ACTION="run-edac-test"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            log_error "Unknown argument: $1"
            usage
            exit 1
            ;;
    esac
done

case "${ACTION}" in
    detect)             cmd_detect ;;
    install-exporter)   cmd_install_exporter ;;
    status)             cmd_status ;;
    configure-scrape)   cmd_configure_scrape "${SCRAPE_HOST_IP}" ;;
    run-edac-test)      cmd_run_edac_test ;;
    *)
        log_error "No action specified."
        usage
        exit 1
        ;;
esac
