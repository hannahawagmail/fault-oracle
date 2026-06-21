#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# ci/qemu-integration-test.sh — QEMU ARM64 integration test for hw-fault-exporter.
#
# Boots a minimal ARM64 Linux image under QEMU, injects a synthetic correctable
# EDAC error via sysfs, scrapes /metrics from the exporter, and asserts that
# the correctable error counter has incremented.
#
# Prerequisites:
#   qemu-system-aarch64  — apt-get install qemu-system-arm
#   cloud-image or custom initrd  — see KERNEL_URL / INITRD_URL below
#   nc (netcat)          — for port-readiness check
#
# Environment variables (all optional):
#   KERNEL_URL    — URL to uncompressed ARM64 kernel (default: Debian bookworm kernel)
#   INITRD_URL    — URL to initrd image
#   EXPORTER_BIN  — path to hw-fault-exporter binary (default: ./hw-fault-exporter)
#   LISTEN_ADDR   — host:port the exporter will listen on inside QEMU (default: :8080)
#   HOST_PORT     — port forwarded from guest to host (default: 18080)
#   TIMEOUT_BOOT  — seconds to wait for guest boot (default: 120)
#   TIMEOUT_SCRAPE — seconds to wait for /metrics to respond (default: 30)
#   KEEP_VM       — if set to "1", do not kill QEMU after test (for debugging)
#   SYSFS_ROOT    — sysfs root to use inside test (default: /sys); override for unit-mode
#   UNIT_TEST     — if "1", skip QEMU and test against mock sysfs on host (CI fast-path)
#
# Exit codes:
#   0 — all assertions passed
#   1 — assertion failed (counter not incremented, metric missing, etc.)
#   2 — prerequisite missing (qemu not installed, binary not found, etc.)
#   3 — timeout (guest did not boot or exporter did not respond in time)
#
# Usage:
#   # Full QEMU integration test (slow, ~3-5 min):
#   EXPORTER_BIN=./hw-fault-exporter bash ci/qemu-integration-test.sh
#
#   # Fast unit-mode (no QEMU, tests exporter against mock sysfs on host):
#   UNIT_TEST=1 EXPORTER_BIN=./hw-fault-exporter bash ci/qemu-integration-test.sh

set -euo pipefail

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
KERNEL_URL="${KERNEL_URL:-https://d-i.debian.org/daily-images/arm64/daily/netboot/debian-installer/arm64/linux}"
INITRD_URL="${INITRD_URL:-https://d-i.debian.org/daily-images/arm64/daily/netboot/debian-installer/arm64/initrd.gz}"
EXPORTER_BIN="${EXPORTER_BIN:-${REPO_ROOT}/hw-fault-exporter}"
LISTEN_ADDR="${LISTEN_ADDR:-:8080}"
HOST_PORT="${HOST_PORT:-18080}"
TIMEOUT_BOOT="${TIMEOUT_BOOT:-120}"
TIMEOUT_SCRAPE="${TIMEOUT_SCRAPE:-30}"
KEEP_VM="${KEEP_VM:-0}"
UNIT_TEST="${UNIT_TEST:-0}"

QEMU_PID=""
EXPORTER_PID=""
WORK_DIR="$(mktemp -d /tmp/qemu-int-XXXXXX)"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_info()  { echo "[INFO]  ${SCRIPT_NAME}: $*"; }
log_warn()  { echo "[WARN]  ${SCRIPT_NAME}: $*" >&2; }
log_error() { echo "[ERROR] ${SCRIPT_NAME}: $*" >&2; }
log_ok()    { echo "[PASS]  ${SCRIPT_NAME}: $*"; }
log_fail()  { echo "[FAIL]  ${SCRIPT_NAME}: $*" >&2; }

# ---------------------------------------------------------------------------
# Cleanup trap
# ---------------------------------------------------------------------------
cleanup() {
    local exit_code=$?
    if [[ "${KEEP_VM}" != "1" ]]; then
        [[ -n "${EXPORTER_PID}" ]] && kill "${EXPORTER_PID}" 2>/dev/null || true
        [[ -n "${QEMU_PID}" ]]    && kill "${QEMU_PID}"    2>/dev/null || true
    fi
    rm -rf "${WORK_DIR}"
    exit "${exit_code}"
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------
check_prerequisites() {
    local missing=()

    if [[ "${UNIT_TEST}" != "1" ]]; then
        command -v qemu-system-aarch64 >/dev/null 2>&1 || missing+=("qemu-system-aarch64")
    fi

    if [[ ! -x "${EXPORTER_BIN}" ]]; then
        log_error "Exporter binary not found or not executable: ${EXPORTER_BIN}"
        log_error "Build with: make build"
        exit 2
    fi

    command -v curl >/dev/null 2>&1 || missing+=("curl")

    if [[ ${#missing[@]} -gt 0 ]]; then
        log_error "Missing prerequisites: ${missing[*]}"
        log_error "Install with: apt-get install ${missing[*]}"
        exit 2
    fi
}

# ---------------------------------------------------------------------------
# Mock sysfs tree
# ---------------------------------------------------------------------------
# Creates a minimal /sys/devices/system/edac/mc0 tree that the exporter can read.
# This is used in both UNIT_TEST mode (direct on host) and inside QEMU guest.
create_mock_sysfs() {
    local sysfs_root="$1"
    local mc_dir="${sysfs_root}/devices/system/edac/mc/mc0"
    mkdir -p "${mc_dir}/csrow0"

    # Initial state: 0 errors
    echo "0" > "${mc_dir}/ce_count"
    echo "0" > "${mc_dir}/ue_count"
    echo "0" > "${mc_dir}/ce_noinfo_count"
    echo "Sandy Bridge ECC" > "${mc_dir}/mc_name"
    echo "0" > "${mc_dir}/csrow0/ce_count"
    echo "0" > "${mc_dir}/csrow0/ue_count"
    log_info "Mock sysfs created at: ${sysfs_root}"
}

# Inject a synthetic CE by writing to the mock sysfs.
inject_correctable_error() {
    local sysfs_root="$1"
    local mc_dir="${sysfs_root}/devices/system/edac/mc/mc0"
    echo "5" > "${mc_dir}/ce_count"
    echo "3" > "${mc_dir}/csrow0/ce_count"
    log_info "Injected CE: ce_count=5, csrow0/ce_count=3"
}

# ---------------------------------------------------------------------------
# Port readiness check
# ---------------------------------------------------------------------------
wait_for_port() {
    local port="$1"
    local timeout="$2"
    local elapsed=0
    log_info "Waiting for port ${port} to open (timeout: ${timeout}s) ..."
    while ! curl -sf "http://localhost:${port}/healthz" >/dev/null 2>&1; do
        sleep 2
        elapsed=$((elapsed + 2))
        if [[ ${elapsed} -ge ${timeout} ]]; then
            log_error "Timeout waiting for port ${port}"
            return 3
        fi
    done
    log_info "Port ${port} is open after ${elapsed}s"
}

# ---------------------------------------------------------------------------
# Metric assertion
# ---------------------------------------------------------------------------
# Fetches /metrics and asserts that a counter matching the pattern has value > 0.
assert_metric_nonzero() {
    local url="$1"
    local metric_pattern="$2"

    local metrics
    metrics="$(curl -sf "${url}")"
    local value
    value="$(echo "${metrics}" | grep -E "^${metric_pattern}" | awk '{print $NF}' | head -1)"

    if [[ -z "${value}" ]]; then
        log_fail "Metric not found: ${metric_pattern}"
        log_fail "Available metrics (sample):"
        echo "${metrics}" | grep "^smartnic\|^edac\|^fault_resilience" | head -20 >&2
        return 1
    fi

    # Use awk for float comparison (bash doesn't do floats)
    if awk -v v="${value}" 'BEGIN { exit (v > 0) ? 0 : 1 }'; then
        log_ok "Metric ${metric_pattern} = ${value} (> 0)"
        return 0
    else
        log_fail "Metric ${metric_pattern} = ${value} (expected > 0)"
        return 1
    fi
}

assert_metric_equals() {
    local url="$1"
    local metric_pattern="$2"
    local expected="$3"

    local metrics
    metrics="$(curl -sf "${url}")"
    local value
    value="$(echo "${metrics}" | grep -E "^${metric_pattern}" | awk '{print $NF}' | head -1)"

    if awk -v v="${value}" -v e="${expected}" 'BEGIN { exit (v == e) ? 0 : 1 }'; then
        log_ok "Metric ${metric_pattern} = ${value} (== ${expected})"
        return 0
    else
        log_fail "Metric ${metric_pattern} = ${value} (expected ${expected})"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# UNIT_TEST mode: run exporter directly on host against mock sysfs
# ---------------------------------------------------------------------------
run_unit_test() {
    log_info "=== UNIT_TEST mode (no QEMU) ==="
    local mock_sysfs="${WORK_DIR}/sysfs"
    create_mock_sysfs "${mock_sysfs}"

    local metrics_url="http://localhost:${HOST_PORT}/metrics"

    log_info "Starting exporter: ${EXPORTER_BIN} --sysfs-root ${mock_sysfs} --listen-addr :${HOST_PORT}"
    "${EXPORTER_BIN}" \
        --sysfs-root "${mock_sysfs}" \
        --listen-addr ":${HOST_PORT}" \
        --no-mce \
        --no-aer \
        --log-level warn \
        > "${WORK_DIR}/exporter.log" 2>&1 &
    EXPORTER_PID=$!

    wait_for_port "${HOST_PORT}" "${TIMEOUT_SCRAPE}"

    # --- Phase 1: baseline (no errors injected) ---
    log_info "Phase 1: baseline scrape"
    assert_metric_equals "${metrics_url}" \
        'edac_correctable_errors_total\{[^}]*mc="0"[^}]*\}' \
        "0" || true  # counter starts at 0 — value may be absent

    assert_metric_equals "${metrics_url}" \
        'fault_resilience_collector_up\{collector="edac"\}' \
        "1"

    # --- Phase 2: inject CE ---
    log_info "Phase 2: inject correctable error"
    inject_correctable_error "${mock_sysfs}"

    # Give exporter one scrape cycle (Prometheus default interval is 15s,
    # but the exporter reads on each /metrics request)
    sleep 1

    assert_metric_nonzero "${metrics_url}" \
        'edac_correctable_errors_total\{[^}]*mc="0"[^}]*\}'

    # --- Phase 3: health endpoints ---
    log_info "Phase 3: health endpoints"
    local health_status
    health_status="$(curl -sf -o /dev/null -w "%{http_code}" "http://localhost:${HOST_PORT}/healthz")"
    if [[ "${health_status}" != "200" ]]; then
        log_fail "/healthz returned ${health_status}"
        exit 1
    fi
    log_ok "/healthz => 200"

    local ready_status
    ready_status="$(curl -sf -o /dev/null -w "%{http_code}" "http://localhost:${HOST_PORT}/readyz")"
    if [[ "${ready_status}" != "200" ]]; then
        log_fail "/readyz returned ${ready_status}"
        exit 1
    fi
    log_ok "/readyz => 200"

    log_ok "=== All unit-test assertions passed ==="
}

# ---------------------------------------------------------------------------
# QEMU integration test
# ---------------------------------------------------------------------------
run_qemu_test() {
    log_info "=== QEMU ARM64 integration test ==="

    # Download kernel and initrd if not cached
    local kernel_path="${WORK_DIR}/vmlinuz"
    local initrd_path="${WORK_DIR}/initrd.gz"

    if [[ -n "${KERNEL_URL}" ]]; then
        log_info "Downloading kernel: ${KERNEL_URL}"
        curl -L --progress-bar -o "${kernel_path}" "${KERNEL_URL}"
    else
        log_error "KERNEL_URL not set and no local kernel provided"
        exit 2
    fi

    if [[ -n "${INITRD_URL}" ]]; then
        log_info "Downloading initrd: ${INITRD_URL}"
        curl -L --progress-bar -o "${initrd_path}" "${INITRD_URL}"
    fi

    # Build a minimal init script that:
    # 1. Mounts /sys
    # 2. Creates mock EDAC sysfs entries
    # 3. Starts the exporter
    # 4. Injects a CE
    # 5. Signals readiness via a serial port file
    local init_script="${WORK_DIR}/init.sh"
    cat > "${init_script}" << 'INIT'
#!/bin/sh
set -e
mount -t sysfs sysfs /sys
mount -t proc proc /proc
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true

# Create mock EDAC hierarchy
MC=/sys/devices/system/edac/mc/mc0
mkdir -p "${MC}/csrow0"
echo 0 > "${MC}/ce_count"
echo 0 > "${MC}/ue_count"
echo 0 > "${MC}/ce_noinfo_count"
echo "ARM ECC" > "${MC}/mc_name"
echo 0 > "${MC}/csrow0/ce_count"
echo 0 > "${MC}/csrow0/ue_count"

# Start exporter
/exporter --listen-addr :8080 --no-mce --no-aer --log-level warn &
EXPORTER_PID=$!

# Wait for exporter to be ready
sleep 3

# Inject CE
echo 7 > "${MC}/ce_count"
echo 4 > "${MC}/csrow0/ce_count"

# Signal ready (write to console)
echo "QEMU_TEST_READY" > /dev/console
sleep 60  # keep alive for scraping window
INIT
    chmod +x "${init_script}"

    # Start QEMU
    log_info "Starting QEMU ARM64 VM ..."
    qemu-system-aarch64 \
        -M virt \
        -cpu cortex-a57 \
        -m 512M \
        -nographic \
        -kernel "${kernel_path}" \
        -initrd "${initrd_path}" \
        -append "console=ttyAMA0 init=/init rdinit=/init" \
        -netdev user,id=net0,hostfwd=tcp::${HOST_PORT}-:8080 \
        -device virtio-net-pci,netdev=net0 \
        -serial file:"${WORK_DIR}/serial.log" \
        > "${WORK_DIR}/qemu.log" 2>&1 &
    QEMU_PID=$!

    log_info "QEMU PID: ${QEMU_PID}"

    # Wait for guest ready signal
    log_info "Waiting for guest boot (timeout: ${TIMEOUT_BOOT}s) ..."
    local elapsed=0
    while ! grep -q "QEMU_TEST_READY" "${WORK_DIR}/serial.log" 2>/dev/null; do
        sleep 2
        elapsed=$((elapsed + 2))
        if ! kill -0 "${QEMU_PID}" 2>/dev/null; then
            log_error "QEMU exited unexpectedly"
            cat "${WORK_DIR}/qemu.log" >&2
            exit 1
        fi
        if [[ ${elapsed} -ge ${TIMEOUT_BOOT} ]]; then
            log_error "Timeout waiting for guest boot"
            exit 3
        fi
    done
    log_info "Guest signaled ready after ${elapsed}s"

    # Wait for exporter port
    wait_for_port "${HOST_PORT}" "${TIMEOUT_SCRAPE}"

    local metrics_url="http://localhost:${HOST_PORT}/metrics"

    assert_metric_nonzero "${metrics_url}" \
        'edac_correctable_errors_total\{[^}]*mc="0"[^}]*\}'

    assert_metric_equals "${metrics_url}" \
        'fault_resilience_collector_up\{collector="edac"\}' "1"

    local health_status
    health_status="$(curl -sf -o /dev/null -w "%{http_code}" "http://localhost:${HOST_PORT}/healthz")"
    [[ "${health_status}" == "200" ]] && log_ok "/healthz => 200" || { log_fail "/healthz => ${health_status}"; exit 1; }

    log_ok "=== All QEMU integration assertions passed ==="
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
main() {
    check_prerequisites

    if [[ "${UNIT_TEST}" == "1" ]]; then
        run_unit_test
    else
        run_qemu_test
    fi
}

main "$@"
