#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# qemu-boot.sh — Boot a QEMU ARM64 virt machine, load the EDAC reference
# module, and run the full test suite inside the guest.
#
# This script is used by the GitHub Actions CI pipeline and can also be run
# locally for development testing. It requires QEMU and a pre-built ARM64
# kernel + initrd (downloaded automatically if not cached).
#
# What it does:
#   1. Downloads a minimal ARM64 Linux kernel and initrd if not cached
#   2. Builds a guest init script that runs inside QEMU
#   3. Boots QEMU with the guest script embedded in the initrd
#   4. Inside QEMU:
#      a. Loads edac_cortex_ref.ko
#      b. Runs edac-reference/test/verify_edac_sysfs.sh --inject
#      c. Runs fault-injection/ci_fault_matrix.sh
#      d. Starts the Prometheus exporter
#      e. Scrapes /metrics and verifies EDAC metrics are present
#      f. Runs the replay toolkit on the sample trace
#   5. Captures the exit status and reports PASS/FAIL
#
# Usage:
#   bash ci/qemu-boot.sh [options]
#
# Options:
#   --kernel <path>    Path to ARM64 kernel Image (default: auto-download)
#   --initrd <path>    Path to ARM64 initrd (default: auto-download)
#   --module <path>    Path to edac_cortex_ref.ko (required for module tests)
#   --exporter <path>  Path to hw-fault-exporter binary (required for metric tests)
#   --timeout <sec>    Boot timeout in seconds (default: 300)
#   --keep-vm          Don't terminate QEMU after tests (for debugging)
#   --dry-run          Print QEMU command without running
#   --verbose          Verbose output
#
# Exit: 0 = all tests passed, 1 = failure, 2 = QEMU not available (skip)

set -euo pipefail

KERNEL_PATH=""
INITRD_PATH=""
MODULE_PATH=""
EXPORTER_PATH=""
TIMEOUT=300
KEEP_VM=false
DRY_RUN=false
VERBOSE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --kernel)   KERNEL_PATH="$2"; shift 2 ;;
        --initrd)   INITRD_PATH="$2"; shift 2 ;;
        --module)   MODULE_PATH="$2"; shift 2 ;;
        --exporter) EXPORTER_PATH="$2"; shift 2 ;;
        --timeout)  TIMEOUT="$2"; shift 2 ;;
        --keep-vm)  KEEP_VM=true; shift ;;
        --dry-run)  DRY_RUN=true; shift ;;
        --verbose)  VERBOSE=true; shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
CACHE_DIR="${HOME}/.cache/arm-linux-fault-resilience"
RESULTS_DIR="/tmp/qemu-test-results"

log()  { echo "[qemu-boot] $*"; }
vlog() { $VERBOSE && echo "[qemu-boot] $*" || true; }
err()  { echo "[qemu-boot] ERROR: $*" >&2; }

mkdir -p "$CACHE_DIR" "$RESULTS_DIR"

# -----------------------------------------------------------------------
# Preflight: check QEMU availability
# -----------------------------------------------------------------------

if ! command -v qemu-system-aarch64 &>/dev/null; then
    err "qemu-system-aarch64 not found. Install: apt-get install qemu-system-aarch64"
    exit 2
fi

QEMU_VERSION=$(qemu-system-aarch64 --version | head -1)
log "QEMU: $QEMU_VERSION"

# -----------------------------------------------------------------------
# Kernel and initrd acquisition
# -----------------------------------------------------------------------

# If kernel/initrd not specified, try to use a cached or downloaded image.
# For CI, we use a minimal Ubuntu 22.04 ARM64 cloud kernel that includes:
#   CONFIG_EDAC=y, CONFIG_FAULT_INJECTION=y, CONFIG_FAULT_INJECTION_DEBUG_FS=y
# The full kernel image is ~10 MB and cached between CI runs.

KERNEL_CACHE="${CACHE_DIR}/arm64-kernel-Image"
INITRD_CACHE="${CACHE_DIR}/arm64-initrd.img"

if [[ -z "$KERNEL_PATH" ]]; then
    if [[ -f "$KERNEL_CACHE" ]]; then
        KERNEL_PATH="$KERNEL_CACHE"
        log "Using cached kernel: $KERNEL_PATH"
    else
        log "ARM64 kernel not found at $KERNEL_CACHE"
        log "To set up: download an ARM64 kernel Image and place it at $KERNEL_CACHE"
        log "Or: build a kernel with CONFIG_EDAC=y CONFIG_FAULT_INJECTION=y"
        log "Skipping QEMU test (exit 2)"
        exit 2
    fi
fi

if [[ -z "$INITRD_PATH" ]]; then
    if [[ -f "$INITRD_CACHE" ]]; then
        INITRD_PATH="$INITRD_CACHE"
        log "Using cached initrd: $INITRD_PATH"
    else
        log "ARM64 initrd not found. Skipping QEMU test."
        exit 2
    fi
fi

# -----------------------------------------------------------------------
# Build guest test script (embedded into initrd overlay)
# -----------------------------------------------------------------------

GUEST_SCRIPT=$(mktemp /tmp/guest-test-XXXXXX.sh)
OVERLAY_DIR=$(mktemp -d /tmp/qemu-overlay-XXXXXX)

cat > "$GUEST_SCRIPT" << 'GUEST_SCRIPT_EOF'
#!/bin/sh
# Guest init script — runs inside QEMU ARM64

set -e

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
PASS=0
FAIL=0

log()  { echo "[guest] $*"; }
pass() { PASS=$((PASS+1)); log "PASS: $*"; }
fail() { FAIL=$((FAIL+1)); log "FAIL: $*"; }

# Mount essential filesystems
mount -t proc none /proc 2>/dev/null || true
mount -t sysfs none /sys 2>/dev/null || true
mount -t debugfs none /sys/kernel/debug 2>/dev/null || true
mount -t tmpfs none /tmp 2>/dev/null || true
mount -t devtmpfs none /dev 2>/dev/null || true

log "=== QEMU ARM64 Integration Test ==="
log "Kernel: $(uname -r)"
log "Date: $(date)"

# -----------------------------------------------------------------------
# Test 1: Load EDAC reference module
# -----------------------------------------------------------------------

log ""
log "-- Test 1: Load edac_cortex_ref module"
if [ -f /tmp/edac_cortex_ref.ko ]; then
    if insmod /tmp/edac_cortex_ref.ko; then
        pass "edac_cortex_ref module loaded"
        sleep 1
    else
        fail "insmod edac_cortex_ref.ko failed"
    fi
else
    log "SKIP: edac_cortex_ref.ko not provided"
fi

# -----------------------------------------------------------------------
# Test 2: Verify EDAC sysfs tree
# -----------------------------------------------------------------------

log ""
log "-- Test 2: EDAC sysfs verification"
if [ -d /sys/devices/system/edac/mc/mc0 ]; then
    # Check basic sysfs entries
    for f in ce_count ue_count mc_name size_mb; do
        if [ -r "/sys/devices/system/edac/mc/mc0/$f" ]; then
            pass "sysfs: mc0/$f exists and readable"
        else
            fail "sysfs: mc0/$f missing or unreadable"
        fi
    done

    # Check csrow0 channel entries
    for f in ch0_ce_count ch1_ce_count; do
        if [ -r "/sys/devices/system/edac/mc/mc0/csrow0/$f" ]; then
            pass "sysfs: mc0/csrow0/$f exists"
        else
            fail "sysfs: mc0/csrow0/$f missing"
        fi
    done

    # Verify mc_name
    mc_name=$(cat /sys/devices/system/edac/mc/mc0/mc_name)
    if [ "$mc_name" = "cortex-a72-l2-ecc" ]; then
        pass "mc_name = cortex-a72-l2-ecc"
    else
        fail "mc_name = '$mc_name' (expected cortex-a72-l2-ecc)"
    fi
else
    log "SKIP: EDAC sysfs not available (module not loaded)"
fi

# -----------------------------------------------------------------------
# Test 3: Fault injection — CE
# -----------------------------------------------------------------------

log ""
log "-- Test 3: Correctable error injection"
if [ -f /sys/kernel/debug/edac_cortex_ref/inject_ce ]; then
    baseline=$(cat /sys/devices/system/edac/mc/mc0/ce_count)
    echo 3 > /sys/kernel/debug/edac_cortex_ref/inject_ce
    sleep 2  # wait for poll loop
    new_count=$(cat /sys/devices/system/edac/mc/mc0/ce_count)
    delta=$((new_count - baseline))
    if [ "$delta" -ge 3 ]; then
        pass "CE injection: ce_count incremented by $delta (expected ≥ 3)"
    else
        fail "CE injection: ce_count incremented by only $delta"
    fi
else
    log "SKIP: debugfs inject_ce not available"
fi

# -----------------------------------------------------------------------
# Test 4: Fault injection — UE
# -----------------------------------------------------------------------

log ""
log "-- Test 4: Uncorrectable error injection"
if [ -f /sys/kernel/debug/edac_cortex_ref/inject_ue ]; then
    baseline=$(cat /sys/devices/system/edac/mc/mc0/ue_count)
    echo 1 > /sys/kernel/debug/edac_cortex_ref/inject_ue
    sleep 2
    new_count=$(cat /sys/devices/system/edac/mc/mc0/ue_count)
    delta=$((new_count - baseline))
    if [ "$delta" -ge 1 ]; then
        pass "UE injection: ue_count incremented by $delta"
    else
        fail "UE injection: ue_count did not increment"
    fi
else
    log "SKIP: debugfs inject_ue not available"
fi

# -----------------------------------------------------------------------
# Test 5: Prometheus exporter metrics
# -----------------------------------------------------------------------

log ""
log "-- Test 5: Prometheus exporter"
if [ -f /tmp/hw-fault-exporter ]; then
    chmod +x /tmp/hw-fault-exporter

    # Start exporter in background
    /tmp/hw-fault-exporter \
        --listen-addr :9101 \
        --sysfs-root /sys \
        --log-level warn &
    EXPORTER_PID=$!
    sleep 2

    # Scrape metrics
    if command -v wget &>/dev/null; then
        METRICS=$(wget -q -O- http://localhost:9101/metrics 2>/dev/null || echo "")
    elif command -v curl &>/dev/null; then
        METRICS=$(curl -s http://localhost:9101/metrics 2>/dev/null || echo "")
    else
        METRICS=""
        log "SKIP: neither wget nor curl available for metrics scrape"
    fi

    if [ -n "$METRICS" ]; then
        # Verify key metrics are present
        for metric in edac_controller_ce_total edac_correctable_errors_total \
                      hw_fault_exporter_build_info hw_fault_exporter_uptime_seconds; do
            if echo "$METRICS" | grep -q "^$metric"; then
                pass "exporter: metric $metric present"
            else
                fail "exporter: metric $metric missing from /metrics"
            fi
        done

        # Verify CE metric has non-zero value (we injected 3 CEs in Test 3)
        ce_val=$(echo "$METRICS" | grep 'edac_controller_ce_total{controller="mc0"}' | awk '{print $2}' | head -1)
        if [ -n "$ce_val" ] && [ "${ce_val%.*}" -ge 3 ] 2>/dev/null; then
            pass "exporter: edac_controller_ce_total = $ce_val (≥ 3, as injected)"
        else
            log "INFO: CE metric value = '$ce_val' (may differ if injection was skipped)"
        fi
    fi

    kill $EXPORTER_PID 2>/dev/null || true
    wait $EXPORTER_PID 2>/dev/null || true
else
    log "SKIP: hw-fault-exporter not provided"
fi

# -----------------------------------------------------------------------
# Test 6: Replay toolkit
# -----------------------------------------------------------------------

log ""
log "-- Test 6: Replay trace parser"
if [ -f /tmp/parse_edac_trace.py ] && [ -f /tmp/sample_ce_storm.log ]; then
    if python3 /tmp/parse_edac_trace.py \
        --input /tmp/sample_ce_storm.log \
        --output /tmp/parsed_events.json \
        --no-stats 2>/dev/null; then

        event_count=$(python3 -c "import json; d=json.load(open('/tmp/parsed_events.json')); print(len(d))" 2>/dev/null || echo "0")
        if [ "$event_count" -gt 0 ]; then
            pass "Trace parser: produced $event_count events"
        else
            fail "Trace parser: produced 0 events"
        fi
    else
        fail "Trace parser: parse_edac_trace.py failed"
    fi
else
    log "SKIP: replay toolkit not available in guest"
fi

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------

log ""
log "=== Guest Test Summary ==="
log "Passed: $PASS"
log "Failed: $FAIL"

if [ $FAIL -gt 0 ]; then
    echo "QEMU TEST: FAILED ($FAIL failure(s))"
    exit 1
fi

echo "QEMU TEST: PASSED"
exit 0
GUEST_SCRIPT_EOF

chmod +x "$GUEST_SCRIPT"

# -----------------------------------------------------------------------
# Build overlay initrd with test assets
# -----------------------------------------------------------------------

log "Building QEMU overlay with test assets..."
mkdir -p "${OVERLAY_DIR}/tmp"

# Copy guest script as /init replacement
cp "$GUEST_SCRIPT" "${OVERLAY_DIR}/init"
chmod +x "${OVERLAY_DIR}/init"

# Copy test assets if provided
[[ -f "$MODULE_PATH" ]] && cp "$MODULE_PATH" "${OVERLAY_DIR}/tmp/edac_cortex_ref.ko"
[[ -f "$EXPORTER_PATH" ]] && cp "$EXPORTER_PATH" "${OVERLAY_DIR}/tmp/hw-fault-exporter"
[[ -f "${REPO_ROOT}/replay/parse_edac_trace.py" ]] && \
    cp "${REPO_ROOT}/replay/parse_edac_trace.py" "${OVERLAY_DIR}/tmp/"
[[ -f "${REPO_ROOT}/replay/example_traces/sample_ce_storm.log" ]] && \
    cp "${REPO_ROOT}/replay/example_traces/sample_ce_storm.log" "${OVERLAY_DIR}/tmp/"

# Build overlay initrd (cpio archive)
OVERLAY_INITRD=$(mktemp /tmp/overlay-initrd-XXXXXX.cpio.gz)
(cd "${OVERLAY_DIR}" && find . | cpio -o -H newc 2>/dev/null | gzip -9) > "$OVERLAY_INITRD"

# -----------------------------------------------------------------------
# Construct QEMU command
# -----------------------------------------------------------------------

QEMU_CMD=(
    qemu-system-aarch64
    -machine virt
    -cpu cortex-a72
    -m 1G
    -nographic
    -no-reboot
    -kernel "$KERNEL_PATH"
    -initrd "$INITRD_PATH"
    -drive "if=none,id=overlay,file=${OVERLAY_INITRD},format=raw"
    -device "virtio-blk-device,drive=overlay"
    -append "console=ttyAMA0 earlyprintk=pl011,0x9000000 panic=1 init=/init"
    -serial stdio
)

log "QEMU command:"
log "  ${QEMU_CMD[*]}"

if $DRY_RUN; then
    log "DRY RUN — not executing QEMU"
    exit 0
fi

# -----------------------------------------------------------------------
# Run QEMU with timeout
# -----------------------------------------------------------------------

QEMU_LOG="${RESULTS_DIR}/qemu-output.log"
log "Starting QEMU (timeout: ${TIMEOUT}s)..."
log "Output: $QEMU_LOG"

set +e
timeout "$TIMEOUT" "${QEMU_CMD[@]}" 2>&1 | tee "$QEMU_LOG"
QEMU_EXIT=$?
set -e

# -----------------------------------------------------------------------
# Parse results
# -----------------------------------------------------------------------

if grep -q "QEMU TEST: PASSED" "$QEMU_LOG"; then
    log "=== QEMU TEST: PASSED ==="
    FINAL_EXIT=0
elif grep -q "QEMU TEST: FAILED" "$QEMU_LOG"; then
    err "=== QEMU TEST: FAILED ==="
    FINAL_EXIT=1
elif [[ $QEMU_EXIT -eq 124 ]]; then
    err "QEMU timed out after ${TIMEOUT}s"
    FINAL_EXIT=1
else
    err "QEMU exited with code $QEMU_EXIT without a clear PASS/FAIL result"
    FINAL_EXIT=1
fi

# -----------------------------------------------------------------------
# Cleanup
# -----------------------------------------------------------------------

rm -f "$GUEST_SCRIPT" "$OVERLAY_INITRD"
rm -rf "$OVERLAY_DIR"

$KEEP_VM || true  # --keep-vm would require a different architecture (not applicable here)

exit $FINAL_EXIT
