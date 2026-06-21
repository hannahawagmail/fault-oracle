#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# wdt-setup.sh — Configure and validate hardware watchdog timer
#
# Supports: ARM SP805 (sp805_wdt), Synopsys DW WDT (dw_wdt),
#           generic Linux /dev/watchdog interface
#
# Usage:
#   bash wdt-setup.sh [--device /dev/watchdog0] [--timeout 60] [--daemon]
#                     [--test] [--status] [--dry-run]
#
# Exit codes:
#   0 — success / check passed
#   1 — error
#   2 — no WDT hardware found (caller should treat as skip/warning)

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
WDT_DEVICE="/dev/watchdog0"
WDT_TIMEOUT=60
MODE=""
DRY_RUN=false
PID_FILE="/var/run/wdt-keepalive.pid"
LOG_TAG="wdt-setup"

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
info()  { echo "[INFO]  $(date '+%Y-%m-%d %H:%M:%S') $*" >&2; }
warn()  { echo "[WARN]  $(date '+%Y-%m-%d %H:%M:%S') $*" >&2; }
error() { echo "[ERROR] $(date '+%Y-%m-%d %H:%M:%S') $*" >&2; }
pass()  { echo "[ PASS ] $*"; }
fail()  { echo "[ FAIL ] $*"; }
skip()  { echo "[ SKIP ] $*"; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --device)    WDT_DEVICE="$2";  shift 2 ;;
            --timeout)   WDT_TIMEOUT="$2"; shift 2 ;;
            --status)    MODE="status";    shift   ;;
            --test)      MODE="test";      shift   ;;
            --daemon)    MODE="daemon";    shift   ;;
            --dry-run)   DRY_RUN=true;     shift   ;;
            --help|-h)
                sed -n '2,12p' "$0" | sed 's/^# //'
                exit 0
                ;;
            *)
                error "Unknown argument: $1"
                exit 1
                ;;
        esac
    done

    if [[ -z "$MODE" ]]; then
        error "No mode specified. Use --status, --test, or --daemon."
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Detect WDT kernel driver from device tree or sysfs
# ---------------------------------------------------------------------------
detect_wdt_driver() {
    local driver=""

    # 1. Check device tree compatible strings for known WDT IPs
    local dt_compat="/sys/firmware/devicetree/base"
    if [[ -d "$dt_compat" ]]; then
        # Walk all compatible files looking for known WDT strings
        while IFS= read -r -d '' compat_file; do
            local compat
            compat=$(strings "$compat_file" 2>/dev/null || true)
            if echo "$compat" | grep -qE "arm,sp805"; then
                driver="sp805_wdt"
                break
            elif echo "$compat" | grep -qE "snps,dw-wdt"; then
                driver="dw_wdt"
                break
            fi
        done < <(find "$dt_compat" -name "compatible" -print0 2>/dev/null)
    fi

    # 2. Fallback: check platform devices already bound in sysfs
    if [[ -z "$driver" ]]; then
        for d in /sys/bus/platform/drivers/sp805-wdt/*/; do
            [[ -d "$d" ]] && driver="sp805_wdt" && break
        done
        for d in /sys/bus/platform/drivers/dw_wdt/*/; do
            [[ -d "$d" ]] && driver="dw_wdt" && break
        done
    fi

    # 3. Check if any watchdog device already exists (driver already loaded)
    if [[ -z "$driver" ]] && ls /dev/watchdog* &>/dev/null; then
        driver="unknown (device present)"
    fi

    echo "$driver"
}

# ---------------------------------------------------------------------------
# Load the appropriate kernel module
# ---------------------------------------------------------------------------
load_wdt_module() {
    local driver="$1"
    case "$driver" in
        sp805_wdt)
            info "Loading sp805_wdt module..."
            $DRY_RUN && { info "[dry-run] Would run: modprobe sp805_wdt"; return 0; }
            modprobe sp805_wdt || {
                warn "modprobe sp805_wdt failed — may already be built-in or unavailable"
            }
            ;;
        dw_wdt)
            info "Loading dw_wdt module..."
            $DRY_RUN && { info "[dry-run] Would run: modprobe dw_wdt"; return 0; }
            modprobe dw_wdt || {
                warn "modprobe dw_wdt failed — may already be built-in or unavailable"
            }
            ;;
        *)
            info "Driver '$driver' — no explicit modprobe needed or driver unknown"
            ;;
    esac
}

# ---------------------------------------------------------------------------
# --status mode: report WDT state
# ---------------------------------------------------------------------------
mode_status() {
    echo "=== Watchdog Timer Status ==="
    echo ""

    # Detect driver
    local driver
    driver=$(detect_wdt_driver)
    if [[ -n "$driver" ]]; then
        pass "WDT driver detected: $driver"
    else
        skip "No known WDT driver found in device tree or sysfs"
    fi

    # List /dev/watchdog* devices
    echo ""
    echo "Watchdog devices:"
    local found_dev=false
    for dev in /dev/watchdog /dev/watchdog[0-9]*; do
        if [[ -c "$dev" ]]; then
            echo "  $dev  (character device $(stat -c '%t:%T' "$dev" 2>/dev/null || echo 'unknown'))"
            found_dev=true
        fi
    done
    $found_dev || { warn "No /dev/watchdog* devices found"; }

    # Report timeout using WDIOC_GETTIMEOUT via Python (bash cannot do ioctls natively)
    echo ""
    echo "Watchdog timeout (from ioctl WDIOC_GETTIMEOUT):"
    if [[ -c "$WDT_DEVICE" ]]; then
        python3 - "$WDT_DEVICE" <<'PYEOF' 2>/dev/null || warn "Could not query timeout (need root or device access)"
import sys, fcntl, struct, os
dev = sys.argv[1]
WDIOC_GETTIMEOUT  = 0x80045706
WDIOC_GETTIMELEFT = 0x80045708
WDIOC_GETSUPPORT  = 0x80285700
try:
    fd = os.open(dev, os.O_RDWR)
    buf = struct.pack('I', 0)
    result = fcntl.ioctl(fd, WDIOC_GETTIMEOUT, bytearray(4))
    timeout = struct.unpack('I', result)[0]
    print(f"  Device:  {dev}")
    print(f"  Timeout: {timeout} seconds")
    try:
        result2 = fcntl.ioctl(fd, WDIOC_GETTIMELEFT, bytearray(4))
        timeleft = struct.unpack('I', result2)[0]
        print(f"  Time left: {timeleft} seconds")
    except Exception:
        pass
    # Write magic 'V' to close cleanly (disarm if nowayout not set)
    os.write(fd, b'V')
    os.close(fd)
except PermissionError:
    print("  Permission denied — run as root")
    sys.exit(1)
except OSError as e:
    print(f"  Error: {e}")
    sys.exit(1)
PYEOF
    else
        warn "Device $WDT_DEVICE not found"
    fi

    # Show loaded WDT-related kernel modules
    echo ""
    echo "Loaded WDT kernel modules:"
    lsmod 2>/dev/null | grep -iE "wdt|watchdog" || echo "  (none or built-in)"

    # QEMU note
    if grep -qi "qemu\|virtio" /sys/devices/virtual/dmi/id/sys_vendor 2>/dev/null \
       || grep -qi "QEMU" /proc/cpuinfo 2>/dev/null; then
        echo ""
        echo "NOTE: QEMU detected. To add a WDT in QEMU:"
        echo "  For virt/ARM: qemu-system-aarch64 ... -device i6300esb,id=wdt0 -watchdog-action reset"
        echo "  For Q35/x86: -machine q35 ... -device iTCO_wdt"
    fi
}

# ---------------------------------------------------------------------------
# --test mode: validate WDT behavior
# ---------------------------------------------------------------------------
mode_test() {
    echo "=== Watchdog Timer Test ==="
    echo ""

    if $DRY_RUN; then
        echo "[dry-run] Would perform the following test sequence:"
        echo "  1. Open $WDT_DEVICE (arms the hardware watchdog)"
        echo "  2. Set timeout to ${WDT_TIMEOUT}s via WDIOC_SETTIMEOUT ioctl"
        echo "  3. Write keepalive byte to reset the timer"
        echo "  4. Verify timeout reads back correctly via WDIOC_GETTIMEOUT"
        echo "  5. Write 'V' (magic close) to disarm WDT before closing"
        echo "  [dry-run] No hardware interaction performed."
        exit 0
    fi

    if ! [[ -c "$WDT_DEVICE" ]]; then
        warn "Device $WDT_DEVICE not found"
        echo "Available devices:"
        ls /dev/watchdog* 2>/dev/null || echo "  (none)"
        exit 2
    fi

    info "Opening $WDT_DEVICE and running test sequence..."
    python3 - "$WDT_DEVICE" "$WDT_TIMEOUT" <<'PYEOF'
import sys, fcntl, struct, os, time

dev     = sys.argv[1]
timeout = int(sys.argv[2])

WDIOC_SETOPTIONS  = 0x40045704
WDIOC_KEEPALIVE   = 0x80005705
WDIOC_SETTIMEOUT  = 0xc0045706
WDIOC_GETTIMEOUT  = 0x80045706
WDIOS_ENABLECARD  = 0x0001
WDIOS_DISABLECARD = 0x0002

def ioctl_rw(fd, req, val=0):
    buf = bytearray(struct.pack('I', val))
    result = fcntl.ioctl(fd, req, buf)
    return struct.unpack('I', result)[0]

try:
    fd = os.open(dev, os.O_RDWR)
    print(f"  [OK] Opened {dev}")
except PermissionError:
    print("  [FAIL] Permission denied — run as root")
    sys.exit(1)
except OSError as e:
    print(f"  [FAIL] Cannot open {dev}: {e}")
    sys.exit(1)

try:
    # Set timeout
    result = ioctl_rw(fd, WDIOC_SETTIMEOUT, timeout)
    print(f"  [OK] WDIOC_SETTIMEOUT requested={timeout}s, applied={result}s")
    actual_timeout = result

    # Read back timeout
    readback = ioctl_rw(fd, WDIOC_GETTIMEOUT)
    if readback == actual_timeout:
        print(f"  [OK] WDIOC_GETTIMEOUT readback={readback}s — consistent")
    else:
        print(f"  [WARN] WDIOC_GETTIMEOUT readback={readback}s differs from applied={actual_timeout}s")

    # Send keepalive
    os.write(fd, b'1')
    print(f"  [OK] Keepalive byte written successfully")

    # Brief wait, then send another keepalive
    time.sleep(0.5)
    os.write(fd, b'1')
    print(f"  [OK] Second keepalive written after 0.5s delay")

    print(f"")
    print(f"  Watchdog is ARMED. System would reset in ~{actual_timeout}s without keepalives.")
    print(f"  Writing magic 'V' to disarm before close (nowayout=0 required)...")
    os.write(fd, b'V')
    os.close(fd)
    print(f"  [OK] Closed cleanly with magic 'V' — WDT disarmed")

except OSError as e:
    print(f"  [FAIL] ioctl error: {e}")
    # Try to close cleanly even on error
    try:
        os.write(fd, b'V')
        os.close(fd)
    except Exception:
        pass
    sys.exit(1)
PYEOF
}

# ---------------------------------------------------------------------------
# --daemon mode: start a background keepalive process
# ---------------------------------------------------------------------------
mode_daemon() {
    echo "=== Watchdog Keepalive Daemon ==="
    echo ""

    if $DRY_RUN; then
        echo "[dry-run] Would launch background watchdog keepalive daemon:"
        echo "  Device:        ${WDT_DEVICE}"
        echo "  Timeout:       ${WDT_TIMEOUT}s"
        echo "  Kick interval: $(( WDT_TIMEOUT / 2 ))s"
        echo "[dry-run] No process started."
        exit 0
    fi

    if ! [[ -c "$WDT_DEVICE" ]]; then
        warn "Device $WDT_DEVICE not found — cannot start daemon"
        exit 2
    fi

    # Check if daemon is already running
    if [[ -f "$PID_FILE" ]]; then
        local old_pid
        old_pid=$(cat "$PID_FILE")
        if kill -0 "$old_pid" 2>/dev/null; then
            warn "Daemon already running with PID $old_pid (from $PID_FILE)"
            exit 1
        else
            info "Stale PID file found, removing: $PID_FILE"
            rm -f "$PID_FILE"
        fi
    fi

    # Compute keepalive interval: kick every half the timeout
    local kick_interval=$(( WDT_TIMEOUT / 2 ))
    [[ $kick_interval -lt 1 ]] && kick_interval=1

    info "Starting keepalive daemon for $WDT_DEVICE"
    info "Timeout: ${WDT_TIMEOUT}s, kick interval: ${kick_interval}s"



    # Launch the daemon as a background Python process
    python3 - "$WDT_DEVICE" "$WDT_TIMEOUT" "$kick_interval" "$PID_FILE" &
    DAEMON_PID=$!
    disown $DAEMON_PID

    sleep 0.3
    if kill -0 $DAEMON_PID 2>/dev/null; then
        pass "Keepalive daemon started with PID $DAEMON_PID"
        echo "$DAEMON_PID" > "$PID_FILE"
        info "PID written to $PID_FILE"
    else
        fail "Daemon process exited immediately — check $WDT_DEVICE permissions"
        exit 1
    fi

    python3 - "$WDT_DEVICE" "$WDT_TIMEOUT" "$kick_interval" "$PID_FILE" <<'PYEOF' &
import sys, os, fcntl, struct, time, signal, logging

dev           = sys.argv[1]
timeout       = int(sys.argv[2])
kick_interval = int(sys.argv[3])
pid_file      = sys.argv[4]

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [wdt-keepalive] %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('wdt')

WDIOC_SETTIMEOUT = 0xc0045706
running = True

def handle_term(signum, frame):
    global running
    log.info("Caught signal %d — disarming watchdog and exiting", signum)
    running = False

signal.signal(signal.SIGTERM, handle_term)
signal.signal(signal.SIGINT,  handle_term)

try:
    fd = os.open(dev, os.O_RDWR)
    log.info("Opened %s, setting timeout=%ds", dev, timeout)
    buf = bytearray(struct.pack('I', timeout))
    fcntl.ioctl(fd, WDIOC_SETTIMEOUT, buf)

    log.info("Keepalive daemon active, kicking every %ds", kick_interval)
    while running:
        os.write(fd, b'1')
        log.debug("Keepalive sent")
        time.sleep(kick_interval)

    # Disarm on clean shutdown
    os.write(fd, b'V')
    os.close(fd)
    log.info("Watchdog disarmed and closed cleanly")
except Exception as e:
    log.error("Fatal error: %s", e)
    sys.exit(1)
finally:
    try:
        os.unlink(pid_file)
    except Exception:
        pass
PYEOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    parse_args "$@"

    # Detect and optionally load the WDT driver before any mode
    local driver
    driver=$(detect_wdt_driver)
    if [[ -n "$driver" && "$driver" != "unknown"* ]]; then
        info "Detected WDT driver: $driver"
        load_wdt_module "$driver"
    fi

    case "$MODE" in
        status) mode_status ;;
        test)   mode_test   ;;
        daemon) mode_daemon  ;;
    esac
}

main "$@"
