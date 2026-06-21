<!-- SPDX-License-Identifier: Apache-2.0 -->
# Out-of-Band Monitoring Protocol — UART Heartbeat

This document specifies the heartbeat protocol used between the main ARM SoC
running Linux and a secondary microcontroller (MCU) watchdog (e.g., ESP32 or
STM32). The MCU operates independently of the Linux kernel and can physically
reset the SoC if the heartbeat stops.

---

## Why Out-of-Band Monitoring?

Software watchdogs (kernel WDT driver, `/dev/watchdog`) run inside the same
kernel they are supposed to monitor. If the kernel panics and the panic handler
itself hangs — or if a memory corruption event races with the WDT reset path —
the watchdog may never fire. The OOB MCU solves this: it is a completely
separate execution environment connected only via UART. No amount of kernel
corruption can prevent the MCU from noticing a silent UART line.

At server scale this is implemented as a Baseboard Management Controller (BMC):
- **iDRAC** (Dell), **iLO** (HPE), **OpenBMC** (open standard)
- The BMC has its own CPU, flash, and Ethernet port
- It can read power states, sensor values, and force power cycles over IPMI

The OOB heartbeat described here is a miniaturised BMC concept for ARM
embedded systems where a full BMC is cost-prohibitive.

---

## Hardware Connections

```
ARM SoC (Linux)              MCU Watchdog (ESP32 / STM32)
┌─────────────────┐          ┌──────────────────────────┐
│   /dev/ttyS1    │──TX─────▶│ UART RX (GPIO16 on ESP32)│
│   (115200 8N1)  │◀─RX──────│ UART TX (GPIO17 on ESP32)│
│                 │          │                          │
│                 │          │  RESET_N ───────────────▶│ SoC RESET_N pin
│                 │          │  (GPIO output, active-low)│
└─────────────────┘          └──────────────────────────┘
```

- The MCU's RESET_N output is wired to the SoC hardware reset pin or to a
  power relay controlling the SoC's supply rail.
- The TX/RX cross-connect is standard UART; no hardware flow control is used.
- A 3.3 V logic level is assumed on both sides; add a level shifter if the SoC
  UART runs at a different voltage.

---

## UART Parameters

| Parameter | Value         |
|-----------|---------------|
| Baud rate | 115200        |
| Data bits | 8             |
| Parity    | None          |
| Stop bits | 1             |
| Flow ctrl | None          |
| SoC port  | /dev/ttyS1    |

---

## Heartbeat Frame Format (8 bytes)

```
Offset  Size  Name           Description
------  ----  ----           -----------
0       1     sync_0         Always 0xAA — frame start marker
1       1     sync_1         Always 0x55 — frame start marker
2       1     seq_hi         High byte of 16-bit sequence number (big-endian)
3       1     seq_lo         Low byte of 16-bit sequence number (big-endian)
4       1     status_flags   Bitmask of health indicators (see below)
5       1     cpu_load_pct   CPU utilisation 0–100, one-second average
6       1     end_marker     Always 0xFF — frame end sentinel
7       1     crc8           CRC-8/MAXIM over bytes 0–6 (see CRC section)
```

### Sync Bytes (0xAA, 0x55)

The two-byte sync sequence `0xAA 0x55` is chosen because it alternates all
bits and is unlikely to appear accidentally mid-stream. The MCU uses it to
re-synchronise framing after a noise burst or UART overrun.

### Sequence Number

A 16-bit big-endian counter that increments by 1 each frame and wraps from
65535 back to 0. The MCU checks for monotonically increasing sequence numbers.
A gap larger than 5 consecutive sequence numbers triggers an alert log;
a gap larger than the timeout period triggers a reset.

### Status Flags (byte 4)

| Bit | Mask | Name             | Source                                      |
|-----|------|------------------|---------------------------------------------|
|  0  | 0x01 | kernel_healthy   | /proc/sys/kernel/tainted == 0               |
|  1  | 0x02 | edac_ce_normal   | /sys/…/edac/mc*/ce_count delta < 100        |
|  2  | 0x04 | memory_ok        | MemAvailable > 10% of MemTotal              |
|  3  | 0x08 | wdt_alive        | PID in /var/run/wdt-keepalive.pid is alive  |
| 4-7 | 0xF0 | reserved         | Must be 0 in this version                   |

A status_flags value of `0x0F` means all four health checks pass.

### CPU Load (byte 5)

Computed by reading `/proc/stat` twice, one second apart:

```
delta_idle  = idle2  - idle1
delta_total = total2 - total1
cpu_load    = round(100 * (1 - delta_idle / delta_total))
```

The MCU can log this value for trend analysis but does not use it for the
reset decision — that is driven solely by frame absence.

### End Marker (0xFF)

The `0xFF` byte before the CRC gives the MCU a second consistency check when
scanning for frame boundaries: a valid frame must have `0xFF` at offset 6.

### CRC-8/MAXIM (byte 7)

- Algorithm: CRC-8/MAXIM (also called CRC-8/1-Wire or CRC-8/DALLAS)
- Polynomial: 0x31 (reversed representation of 0x8C)
- Initial value: 0x00
- Input/output reflection: none (straight, MSB-first)
- Computed over bytes 0–6 inclusive (all 7 bytes before the CRC)

Reference implementation (Python):
```python
def crc8(data: bytes) -> int:
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc << 1) ^ 0x31 if crc & 0x80 else crc << 1
            crc &= 0xFF
    return crc
```

---

## Timeout and Reset Logic

The MCU maintains a countdown timer, reset to **30 seconds** on every valid
received frame. If the timer expires without a valid frame:

1. The MCU logs the timeout to its own flash or UART debug output.
2. The MCU executes the power cycle sequence below.

The 30-second window is deliberately longer than the maximum heartbeat interval
(5 seconds by default) to tolerate:
- Kernel scheduler latency during high load
- Temporary UART driver stall during I/O storms
- Brief kernel freezes that recover on their own

---

## Power Cycle Sequence

When the MCU decides a reset is necessary, it executes:

```
1. Assert RESET_N LOW   (active-low reset, GPIO driven low)
2. Hold for 100 ms      (ensures SoC power rail collapses or reset is registered)
3. Release RESET_N HIGH (allow SoC to begin power-on sequence)
4. Wait 2000 ms         (allow SoC to reach BIOS/bootloader)
5. If still no heartbeat after 30s from this point:
   - Pulse RESET_N LOW again for 100 ms (second attempt)
   - Wait 2000 ms
6. After 3 failed reset attempts: assert power relay OFF for 5s, then ON
```

The power relay (if present) controls the SoC's 5 V or 12 V supply rail via a
GPIO-driven FET or mechanical relay. This is the final escalation path when
RESET_N is insufficient (e.g., the SoC's reset sequencer is itself hung).

---

## Emergency SysRq Injection

Before executing a hard reset, the MCU may attempt a graceful kernel reboot by
injecting an ASCII SysRq command via the UART's TX line:

```
MCU sends: 0x62 0x0A   (ASCII 'b' followed by newline)
```

With `echo b > /proc/sysrq-trigger` semantics, this triggers an immediate
kernel reboot without syncing filesystems. The kernel must have
`CONFIG_MAGIC_SYSRQ=y` and the UART must be registered as a sysrq-capable
console (e.g., `console=ttyS1,115200` in the kernel command line, with
`CONFIG_SERIAL_SYSRQ=y`).

SysRq injection is the preferred first step because it allows the kernel to
run its reboot notifier chain (gives drivers a chance to quiesce hardware)
rather than a cold cut of power. If no heartbeat resumes within 10 seconds
after injection, the MCU proceeds with the hard power cycle.

---

## Example: Python Heartbeat Sender (main SoC side)

```python
#!/usr/bin/env python3
# Minimal sender — see oob-heartbeat.py for the full implementation
import serial, time, struct

def crc8(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc << 1) ^ 0x31 if crc & 0x80 else crc << 1
            crc &= 0xFF
    return crc

def build_frame(seq, flags, cpu):
    payload = bytes([0xAA, 0x55, seq >> 8, seq & 0xFF, flags, cpu, 0xFF])
    return payload + bytes([crc8(payload)])

port = serial.Serial("/dev/ttyS1", 115200, timeout=1)
seq = 0
while True:
    frame = build_frame(seq, 0x0F, 42)   # flags=all-ok, cpu=42%
    port.write(frame)
    seq = (seq + 1) & 0xFFFF
    time.sleep(5)
```

---

## Example: ESP32/Arduino Receiver Pseudocode

```cpp
// Minimal MCU receiver — illustrative, not production-ready
#include <HardwareSerial.h>

#define RESET_PIN    4       // GPIO connected to SoC RESET_N
#define TIMEOUT_MS   30000  // 30 seconds

uint8_t crc8(uint8_t *data, uint8_t len) {
    uint8_t crc = 0x00;
    for (uint8_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (uint8_t b = 0; b < 8; b++)
            crc = (crc & 0x80) ? (crc << 1) ^ 0x31 : (crc << 1);
    }
    return crc;
}

uint8_t buf[8];
uint8_t pos = 0;
unsigned long lastValid = millis();

void loop() {
    while (Serial2.available()) {
        uint8_t c = Serial2.read();
        if (pos == 0 && c != 0xAA) continue;   // hunt for sync_0
        if (pos == 1 && c != 0x55) { pos = 0; continue; } // sync_1 mismatch
        buf[pos++] = c;
        if (pos == 8) {
            pos = 0;
            if (buf[6] == 0xFF && crc8(buf, 7) == buf[7]) {
                // Valid frame received
                lastValid = millis();
                uint8_t flags = buf[4];
                uint8_t cpu   = buf[5];
                // Log or react to flags here
            }
        }
    }
    if (millis() - lastValid > TIMEOUT_MS) {
        // Heartbeat timeout — execute reset
        digitalWrite(RESET_PIN, LOW);
        delay(100);
        digitalWrite(RESET_PIN, HIGH);
        lastValid = millis();   // reset timer; wait for SoC to come back
    }
}
```

---

## Security Considerations

- The UART is a physical connection; no encryption is applied. Physical access
  to the UART pins implies physical access to the system.
- Do not expose the OOB UART on any network interface.
- The MCU firmware should be stored in read-only flash after deployment.
- Consider a shared HMAC if the UART is routed through a potentially
  untrusted backplane.

---

## Versioning

This document describes protocol version **1.0**. Future versions may expand
the frame by using the reserved bits in status_flags or by adding a 16-byte
variant with a 4-byte CRC-32 for higher integrity assurance.
