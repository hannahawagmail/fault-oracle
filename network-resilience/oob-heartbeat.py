#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
oob-heartbeat.py — Out-of-band heartbeat sender for main ARM SoC

Sends structured heartbeat frames over UART to a secondary MCU watchdog.
Frame: [0xAA, 0x55, seq_hi, seq_lo, status_flags, cpu_load_pct, 0xFF, crc8]

Status flags:
  bit 0: kernel healthy (tainted == 0)
  bit 1: EDAC CE rate normal (ce_count delta < 100 in last interval)
  bit 2: memory pressure ok (MemAvailable > 10% of MemTotal)
  bit 3: watchdog process alive (PID file exists and process running)

Usage:
  python3 oob-heartbeat.py [--port /dev/ttyS1] [--baud 115200]
                           [--interval 5] [--dry-run] [--status]
"""
import argparse
import glob
import os
import sys
import time


# ---------------------------------------------------------------------------
# CRC-8/MAXIM (Dallas 1-Wire), polynomial 0x31, init 0x00
# ---------------------------------------------------------------------------
def crc8(data: bytes) -> int:
    """Compute CRC-8 using polynomial 0x31 (Dallas/Maxim 1-Wire), init 0x00."""
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = (crc << 1) ^ 0x31
            else:
                crc <<= 1
            crc &= 0xFF
    return crc


# ---------------------------------------------------------------------------
# Frame construction
# ---------------------------------------------------------------------------
def build_frame(seq: int, status_flags: int, cpu_load: int) -> bytes:
    """
    Build an 8-byte heartbeat frame.

    Layout:
      [0] 0xAA        sync byte 0
      [1] 0x55        sync byte 1
      [2] seq_hi      high byte of 16-bit sequence number
      [3] seq_lo      low byte of 16-bit sequence number
      [4] status_flags  bitmask of health checks
      [5] cpu_load_pct  0-100 integer
      [6] 0xFF        end marker
      [7] crc8        CRC-8/MAXIM over bytes 0-6
    """
    seq &= 0xFFFF  # clamp to 16-bit
    cpu_load = max(0, min(100, cpu_load))
    payload = bytes([
        0xAA,
        0x55,
        (seq >> 8) & 0xFF,
        seq & 0xFF,
        status_flags & 0xFF,
        cpu_load,
        0xFF,
    ])
    checksum = crc8(payload)
    return payload + bytes([checksum])


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------
def read_cpu_load() -> int:
    """
    Read /proc/stat twice (1 second apart) and return CPU usage as 0-100.
    Returns 0 if /proc/stat is unavailable.
    """
    def _read_stat():
        try:
            with open("/proc/stat", "r") as f:
                line = f.readline()  # first line: "cpu  ..."
            parts = line.split()
            # user, nice, system, idle, iowait, irq, softirq, steal
            fields = [int(x) for x in parts[1:]]
            idle = fields[3] if len(fields) > 3 else 0
            total = sum(fields)
            return idle, total
        except OSError:
            return 0, 1

    idle1, total1 = _read_stat()
    time.sleep(1)
    idle2, total2 = _read_stat()

    delta_total = total2 - total1
    delta_idle = idle2 - idle1
    if delta_total <= 0:
        return 0
    usage = int(100 * (1.0 - delta_idle / delta_total))
    return max(0, min(100, usage))


def check_kernel_health() -> bool:
    """
    Returns True if the kernel taint flags are zero (no known issues).
    Reads /proc/sys/kernel/tainted.
    """
    try:
        with open("/proc/sys/kernel/tainted", "r") as f:
            return f.read().strip() == "0"
    except OSError:
        # If we cannot read the file, assume healthy (e.g., older kernels)
        return True


def check_edac_health(prev_ce: int) -> tuple:
    """
    Read correctable error counts from all EDAC memory controllers.
    Returns (healthy: bool, new_total: int).
    Healthy if the delta from prev_ce is < 100 (burst threshold).
    Returns (True, prev_ce) if EDAC sysfs is not present.
    """
    pattern = "/sys/devices/system/edac/mc*/ce_count"
    paths = glob.glob(pattern)
    if not paths:
        # EDAC not compiled in or no ECC memory — treat as healthy
        return True, prev_ce

    total = 0
    for path in paths:
        try:
            with open(path, "r") as f:
                total += int(f.read().strip())
        except (OSError, ValueError):
            pass

    delta = total - prev_ce
    healthy = delta < 100
    return healthy, total


def check_memory_pressure() -> bool:
    """
    Parse /proc/meminfo for MemAvailable and MemTotal.
    Returns True if MemAvailable > 10% of MemTotal.
    Returns True if /proc/meminfo is unavailable.
    """
    mem_total = None
    mem_available = None
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_available = int(line.split()[1])
                if mem_total is not None and mem_available is not None:
                    break
    except OSError:
        return True  # cannot read — assume ok

    if mem_total is None or mem_available is None or mem_total == 0:
        return True

    return mem_available > (mem_total * 0.10)


def check_wdt_alive() -> bool:
    """
    Check if /var/run/wdt-keepalive.pid exists and the PID therein is running.
    Returns False if the file is missing or the process is gone.
    """
    pid_file = "/var/run/wdt-keepalive.pid"
    try:
        with open(pid_file, "r") as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # signal 0: check existence, no actual signal sent
        return True
    except FileNotFoundError:
        return False
    except (ValueError, ProcessLookupError, PermissionError):
        return False


# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------
def print_status(prev_ce: int) -> int:
    """Print each health check and return the current EDAC CE total."""
    kernel_ok = check_kernel_health()
    edac_ok, new_ce = check_edac_health(prev_ce)
    mem_ok = check_memory_pressure()
    wdt_ok = check_wdt_alive()
    cpu = 0  # skip 1-second sleep for status mode
    try:
        with open("/proc/stat", "r") as f:
            line = f.readline()
        parts = [int(x) for x in line.split()[1:]]
        total = sum(parts)
        idle = parts[3] if len(parts) > 3 else 0
        cpu = max(0, min(100, int(100 * (1.0 - idle / total)))) if total else 0
    except OSError:
        pass

    flags = (
        (0x01 if kernel_ok else 0x00) |
        (0x02 if edac_ok else 0x00) |
        (0x04 if mem_ok else 0x00) |
        (0x08 if wdt_ok else 0x00)
    )

    print(f"  kernel_healthy   : {'OK' if kernel_ok else 'FAIL'}")
    print(f"  edac_ce_normal   : {'OK' if edac_ok else 'FAIL'} (total CE={new_ce})")
    print(f"  memory_ok        : {'OK' if mem_ok else 'FAIL'}")
    print(f"  wdt_alive        : {'OK' if wdt_ok else 'FAIL'}")
    print(f"  cpu_load_instant : {cpu}%")
    print(f"  status_flags     : 0x{flags:02X}  ({flags:08b}b)")
    return new_ce


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="OOB heartbeat sender — ARM SoC → MCU watchdog"
    )
    parser.add_argument("--port", default="/dev/ttyS1",
                        help="Serial port (default: /dev/ttyS1)")
    parser.add_argument("--baud", type=int, default=115200,
                        help="Baud rate (default: 115200)")
    parser.add_argument("--interval", type=int, default=5,
                        help="Heartbeat interval in seconds (default: 5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build one frame, print hex, then exit")
    parser.add_argument("--status", action="store_true",
                        help="Print health check results and exit")
    args = parser.parse_args()

    if args.status:
        print("=== OOB Heartbeat Health Status ===")
        print_status(prev_ce=0)
        sys.exit(0)

    if args.dry_run:
        # Collect health quickly (no 1s CPU sample)
        kernel_ok = check_kernel_health()
        edac_ok, ce_count = check_edac_health(0)
        mem_ok = check_memory_pressure()
        wdt_ok = check_wdt_alive()
        flags = (
            (0x01 if kernel_ok else 0x00) |
            (0x02 if edac_ok else 0x00) |
            (0x04 if mem_ok else 0x00) |
            (0x08 if wdt_ok else 0x00)
        )
        frame = build_frame(seq=0, status_flags=flags, cpu_load=0)
        print("DRY-RUN: heartbeat frame (hex):", frame.hex(" ").upper())
        print(f"  sync=AA 55, seq=0x0000, flags=0x{flags:02X}, cpu=0x00, end=FF, "
              f"crc=0x{frame[7]:02X}")
        sys.exit(0)

    # Normal operation: open serial port and loop
    try:
        import serial  # pyserial
    except ImportError:
        print("ERROR: pyserial not installed. Run: pip3 install pyserial", file=sys.stderr)
        sys.exit(1)

    try:
        ser = serial.Serial(args.port, baudrate=args.baud, timeout=1)
    except serial.SerialException as exc:
        print(f"ERROR: Cannot open {args.port}: {exc}", file=sys.stderr)
        sys.exit(1)

    seq = 0
    prev_ce = 0
    print(f"OOB heartbeat running: port={args.port}, baud={args.baud}, "
          f"interval={args.interval}s", file=sys.stderr)

    try:
        while True:
            cpu_load = read_cpu_load()  # includes 1s sleep internally

            kernel_ok = check_kernel_health()
            edac_ok, prev_ce = check_edac_health(prev_ce)
            mem_ok = check_memory_pressure()
            wdt_ok = check_wdt_alive()

            flags = (
                (0x01 if kernel_ok else 0x00) |
                (0x02 if edac_ok else 0x00) |
                (0x04 if mem_ok else 0x00) |
                (0x08 if wdt_ok else 0x00)
            )

            frame = build_frame(seq=seq, status_flags=flags, cpu_load=cpu_load)
            ser.write(frame)
            ser.flush()

            print(f"[seq={seq:05d}] flags=0x{flags:02X} cpu={cpu_load}% "
                  f"frame={frame.hex(' ').upper()}", file=sys.stderr)

            seq = (seq + 1) & 0xFFFF

            # Sleep remaining time (read_cpu_load already spent ~1s)
            remaining = args.interval - 1
            if remaining > 0:
                time.sleep(remaining)

    except KeyboardInterrupt:
        print("\nHeartbeat stopped by user.", file=sys.stderr)
    finally:
        ser.close()


if __name__ == "__main__":
    main()
