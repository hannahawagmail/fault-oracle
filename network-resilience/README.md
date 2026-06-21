# SPDX-License-Identifier: Apache-2.0
# Network Resilience Module

This module implements a layered network-fault-resilience strategy for ARM
Linux systems: automatic connection fallback through NetworkManager, a
monitoring daemon that detects and triggers failover, and an out-of-band (OOB)
hardware watchdog heartbeat that survives kernel panics.

---

## Connection Fallback Priority Model

Network connections are arranged in three tiers, ordered by reliability and
cost. NetworkManager (NM) tries connections from highest priority to lowest.
When a higher-priority connection becomes available again, NM restores it
automatically.

```
Priority 100  eth0 / wired Ethernet     ← always tried first
    │
    ▼  (if eth0 down or DHCP fails)
Priority 50   WiFi (WPA2-PSK)           ← used when no wire
    │
    ▼  (if WiFi unavailable or too far)
Priority 10   LTE / 4G modem            ← last resort; metered
```

When all three fail, the system has no WAN connectivity. A separate local AP
can be configured (autoconnect-priority=1) to allow engineering access via
ad-hoc WiFi — this is out of scope for this module but follows the same NM
keyfile pattern.

### autoconnect-priority

The `autoconnect-priority` field in the `[connection]` section of an NM
keyfile controls the order in which NM tries available connections at boot or
after a disconnect event. Higher integer value = tried first. NM scans all
`autoconnect=true` profiles, sorts them descending by priority, and attempts
activation starting from the top.

If a profile fails after `autoconnect-retries` attempts, NM marks it
temporarily disabled and moves to the next in priority order.

```ini
[connection]
autoconnect=true
autoconnect-priority=100   # try this one first
autoconnect-retries=3      # give up after 3 attempts, try next profile
```

### route-metric and the Kernel Routing Table

Each NM connection installs one or more routes into the kernel routing table.
The `route-metric` value in `[ipv4]` maps directly to the metric field in
those routes — visible in `ip route show` and in `/proc/net/route`.

**Lower metric = more preferred route.** When multiple interfaces are up
simultaneously (e.g., eth0 + LTE tethering), the kernel forwards packets via
the interface with the lowest-metric default route:

```
$ ip route show
default via 192.168.1.1 dev eth0 proto dhcp metric 100   ← used (lowest)
default via 10.0.0.1   dev wwan0 proto dhcp metric 300   ← standby
```

The metric hierarchy in this module:

| Interface | route-metric | Notes                              |
|-----------|-------------|------------------------------------|
| eth0      | 100         | Preferred; fast, unmetered         |
| WiFi      | 200         | Secondary; may have interference   |
| LTE/wwan0 | 300         | Fallback; metered, slower          |

Separation of 100 between tiers ensures future intermediate profiles can be
inserted (e.g., a second Ethernet at metric 150) without renumbering.

---

## NetworkManager Connection Profiles

All profiles live in `nm-connection-profiles/` and follow the NM keyfile
format. Deploy to `/etc/NetworkManager/system-connections/` with mode `600`
(NM refuses to load world-readable keyfiles containing credentials).

### 10-primary-eth.nmconnection

Primary wired connection on `eth0`. DHCP-assigned address; DNS overridden to
`8.8.8.8` and `8.8.4.4` for consistency across environments. Route metric 100.

### 20-wifi-backup.nmconnection

WPA2-PSK WiFi on any band. The `psk=` field contains a placeholder —
**replace it before deployment** using:

```bash
nmcli connection modify wifi-backup wifi-security.psk "REAL_PASSWORD"
```

NM stores the real PSK with 600 permissions. Route metric 200.

### 30-lte-fallback.nmconnection

GSM/LTE connection managed by ModemManager. NM auto-detects the modem via
D-Bus; no `/dev/ttyUSB0` path is hard-coded. Set the correct APN for your
carrier. IPv6 is disabled (`method=ignore`) because many LTE APNs do not
provision IPv6 and the long DAD timeout delays connection establishment.
Route metric 300, 5 retries to tolerate slow modem registration.

### Deployment

```bash
PROFILE_DIR=/etc/NetworkManager/system-connections
for f in nm-connection-profiles/*.nmconnection; do
    cp "$f" "$PROFILE_DIR/"
    chmod 600 "$PROFILE_DIR/$(basename $f)"
done
nmcli connection reload
nmcli connection show   # verify profiles are loaded
```

---

## connection-monitor.sh

A Bash daemon that periodically pings a target IP and triggers NM failover
after a configurable number of consecutive failures. Also exports Prometheus
metrics for observability.

### Usage

```bash
# Show current NM state and routing table, then exit:
bash connection-monitor.sh --status

# Run as a daemon — check every 30s, fail over after 3 misses:
bash connection-monitor.sh --monitor --interval 30 --failure-threshold 3

# Immediately trigger one failover cycle (for testing):
bash connection-monitor.sh --failover --dry-run

# All options:
bash connection-monitor.sh --monitor \
    --ping-target 8.8.8.8 \
    --interval 30 \
    --failure-threshold 3 \
    --dry-run
```

### Failover Logic

1. `cmd_status()` — prints NM connection table and default route. Falls back to
   parsing `/proc/net/route` (hex, little-endian) when the `ip` command is
   unavailable.
2. `cmd_monitor()` — ping loop; increments a streak counter on each miss;
   calls `cmd_failover()` when the streak reaches the threshold; resets the
   streak to 0 after failover or on recovery.
3. `cmd_failover()` — calls `nmcli connection down id <current>` then
   `nmcli connection up id <next>`. Logs via `logger(1)` to syslog at
   `daemon.warning` (success) or `daemon.crit` (exhausted). Persists a
   cumulative counter to `/tmp/nm-failover-count`.

### Prometheus Metrics

Written to `/var/lib/prometheus/node-exporter/network.prom` on every
monitor iteration and on clean exit:

```
network_ping_failure_streak          N   # consecutive ping failures
network_active_connection_priority   N   # autoconnect-priority of active conn
network_connection_failovers_total   N   # total failovers since process start
```

These are consumed by the Prometheus `node_exporter` textfile collector.

### Running as a systemd Service

```ini
[Unit]
Description=Network Connection Monitor
After=NetworkManager.service network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/connection-monitor.sh --monitor --interval 30 --failure-threshold 3
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## Out-of-Band (OOB) Monitoring

### The Problem: Kernel Panic + Watchdog Race

The Linux kernel software watchdog (`/dev/watchdog`) works by requiring a
userspace process to write to it periodically. If userspace freezes, the
kernel WDT fires a reset. But there is a critical gap:

- A kernel panic may crash the kernel before the WDT can trigger.
- Kernel panic handlers can themselves hang (e.g., waiting on a stuck spinlock
  during the oops handler).
- Memory corruption can corrupt the WDT driver state in DRAM before reset.

In these cases the system is hard-hung and will not recover without external
intervention. Software alone cannot fix this.

### The OOB Solution: Secondary MCU

A secondary microcontroller (ESP32, STM32, or any small MCU) is connected to
the main SoC via UART. It runs firmware that:

1. Listens for structured heartbeat frames from the Linux userspace daemon.
2. Maintains a 30-second countdown timer, reset on each valid frame.
3. If the timer expires, physically toggles the SoC's RESET_N pin (or a power
   relay) to force a cold boot.

Because the MCU runs its own RTOS (or bare-metal loop) on its own CPU with its
own power supply rail, it is unaffected by anything that happens to the Linux
kernel. The MCU is the last resort that no kernel bug can defeat.

### Relationship to BMC at Server Scale

At server scale this same concept is a Baseboard Management Controller (BMC):

| Concept          | Embedded ARM board         | Server                        |
|------------------|---------------------------|-------------------------------|
| OOB processor    | ESP32 / STM32              | BMC SoC (AST2600, etc.)       |
| OOB OS           | FreeRTOS / bare metal      | OpenBMC / AMI MegaRAC         |
| Heartbeat path   | UART / GPIO                | LPC / eSPI / dedicated NIC    |
| Reset mechanism  | RESET_N GPIO / power relay | IPMI chassis control          |
| Remote access    | Physical serial port       | iDRAC / iLO / OpenBMC web UI  |

iDRAC (Dell), iLO (HPE), and OpenBMC (Meta, Google, IBM, etc.) are all
implementations of the same principle: a secondary, independently-powered
computer that monitors and controls the primary CPU. The OOB heartbeat here is
a miniaturised BMC for cost-sensitive embedded systems.

### Heartbeat Frame

The UART protocol sends an 8-byte frame every 5 seconds (configurable):

```
[0xAA][0x55][seq_hi][seq_lo][status_flags][cpu_load][0xFF][crc8]
```

See `oob-monitor-protocol.md` for the full frame specification, CRC algorithm,
timeout and reset sequence, and emergency SysRq injection procedure.

---

## oob-heartbeat.py

Python 3 daemon that runs on the ARM SoC and sends heartbeat frames over UART.

### Usage

```bash
# Print health check status and exit (no UART needed):
python3 oob-heartbeat.py --status

# Print one frame in hex and exit (no UART needed, no pyserial required):
python3 oob-heartbeat.py --dry-run

# Run normally (requires pyserial: pip3 install pyserial):
python3 oob-heartbeat.py --port /dev/ttyS1 --baud 115200 --interval 5

# Custom port and interval:
python3 oob-heartbeat.py --port /dev/ttyAMA0 --baud 115200 --interval 10
```

### Health Checks (status_flags bits)

| Bit | Check                | Source                                        |
|-----|----------------------|-----------------------------------------------|
|  0  | kernel_healthy       | `/proc/sys/kernel/tainted` == `0`             |
|  1  | edac_ce_normal       | EDAC CE delta < 100 per interval              |
|  2  | memory_ok            | MemAvailable > 10% of MemTotal                |
|  3  | wdt_alive            | `/var/run/wdt-keepalive.pid` PID is running   |

### Running as a systemd Service

```ini
[Unit]
Description=OOB Heartbeat Sender
After=sysinit.target

[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/oob-heartbeat.py \
    --port /dev/ttyS1 --baud 115200 --interval 5
Restart=always
RestartSec=2
# Run early in boot, before most services, so the MCU is never starved
StartLimitIntervalSec=0

[Install]
WantedBy=multi-user.target
```

---

## Quick-Start

```bash
# 1. Deploy NM profiles
sudo cp nm-connection-profiles/*.nmconnection /etc/NetworkManager/system-connections/
sudo chmod 600 /etc/NetworkManager/system-connections/*.nmconnection
sudo nmcli connection reload

# 2. Set WiFi password securely
sudo nmcli connection modify wifi-backup wifi-security.psk "YOUR_WIFI_PASSWORD"

# 3. Verify profile priorities
nmcli -f NAME,AUTOCONNECT-PRIORITY,STATE connection show

# 4. Check current connectivity status
bash connection-monitor.sh --status

# 5. Start OOB heartbeat (dry-run first)
python3 oob-heartbeat.py --dry-run
python3 oob-heartbeat.py --status
# Then run for real:
sudo python3 oob-heartbeat.py --port /dev/ttyS1

# 6. Run tests
cd tests
python3 -m pytest test_network_resilience.py -v
```

---

## File Index

| File                                          | Description                                  |
|-----------------------------------------------|----------------------------------------------|
| `nm-connection-profiles/10-primary-eth.nmconnection`  | Primary Ethernet (priority 100)      |
| `nm-connection-profiles/20-wifi-backup.nmconnection`  | WiFi backup (priority 50)            |
| `nm-connection-profiles/30-lte-fallback.nmconnection` | LTE fallback (priority 10)           |
| `connection-monitor.sh`                       | Connectivity monitor and failover daemon     |
| `oob-heartbeat.py`                            | OOB UART heartbeat sender (Python 3)         |
| `oob-monitor-protocol.md`                     | OOB frame format and protocol specification  |
| `tests/test_network_resilience.py`            | pytest test suite                            |
