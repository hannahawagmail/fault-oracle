<!-- SPDX-License-Identifier: Apache-2.0 -->
# boot-resilience — ARM Linux Boot Fault Resilience

This module provides scripts, configuration snippets, and documentation for
building robust boot paths on ARM embedded and server hardware. A single bad
firmware update, unexpected power cut, or corrupted boot sector can render a
remote fleet of devices unrecoverable without deliberate resilience measures.

---

## Table of Contents

1. [Why Boot Resilience Matters](#why-boot-resilience-matters)
2. [Hardware Watchdog Timers](#hardware-watchdog-timers)
3. [A/B Rootfs Partitioning](#ab-rootfs-partitioning)
4. [U-Boot Bootcount Mechanism](#u-boot-bootcount-mechanism)
5. [Secure Boot and TrustZone](#secure-boot-and-trustzone)
6. [Quick Start](#quick-start)
7. [File Reference](#file-reference)

---

## Why Boot Resilience Matters

### The Core Problem

ARM-based systems — from single-board computers like Raspberry Pi CM4 and
NXP i.MX8, to server-class platforms like Ampere Altra and AWS Graviton — are
frequently deployed in locations where physical access is difficult or
impossible: factory floors, cell towers, data centers in remote regions,
satellites. Unlike an x86 server where an operator can attach a keyboard and
monitor in a data center row, a bricked ARM device may require shipping the
unit back to a depot for reflash, costing hundreds of dollars per unit plus
days of downtime.

### Failure Scenarios

| Scenario | Without Resilience | With Resilience |
|---|---|---|
| Power cut during OTA rootfs write | Partially written rootfs; unbootable | A/B scheme: old partition untouched |
| Kernel panic on boot after update | System hangs, never recovers | WDT resets, bootcount increments, fallback activates |
| Bad kernel cmdline parameter | Boot loops silently | Bootcount limit triggers altbootcmd |
| Corrupted U-Boot environment | Random bootargs; may brick | Redundant env partition + Secure Boot chain |
| Signed firmware replaced with unsigned | Boots arbitrary code | Secure Boot refuses to load |
| Storage corruption on power loss | Filesystem unreadable | Journaled/CoW fs or read-only rootfs + overlay |

### Scale Multiplier

A fleet of 10,000 IoT devices running an OTA update has a measurable probability
of hitting power loss mid-write, even with a brief update window. At 0.01%
failure rate, that is 1 device. At 0.1%, 10 devices. Without A/B partitioning
and watchdog recovery, every one of those requires manual intervention.

---

## Hardware Watchdog Timers

### What Is a Watchdog Timer?

A hardware watchdog timer (WDT) is a countdown timer implemented in hardware,
completely independent of the CPU cores, the Linux kernel, and userspace. Once
armed, it must be periodically "kicked" (reset) by software. If software stops
kicking the WDT — because the kernel panicked, userspace hung, or a deadlock
occurred — the timer expires and the hardware asserts a reset signal to the
SoC, forcing a full reboot.

### Hardware WDT vs Software WDT

| Attribute | Hardware WDT | Software WDT (daemon only) |
|---|---|---|
| Peripheral location | Dedicated WDT IP block in SoC | Timer in kernel or userspace |
| Survives kernel panic | Yes — hardware is independent | No — kernel panic stops everything |
| Survives kernel hang | Yes | No |
| Survives userspace hang | Yes (if kernel still kicks it) | Depends on daemon |
| Reset mechanism | Hardware reset line to SoC | Software reboot() syscall |
| Linux interface | /dev/watchdog via ioctl | /dev/watchdog or custom |
| Required for production | Yes | Supplemental only |

A software-only watchdog daemon is useful for detecting hung userspace
processes, but it cannot recover from kernel panics. Always use hardware WDT
as the primary mechanism.

### ARM SP805 Watchdog (arm_sp805_wdt)

The ARM SP805 is the standard ARM PrimeCell Watchdog peripheral, described by
ARM document DDI0270B. It is present on many ARM-based SoCs including:

- NXP i.MX6/7/8
- STM32MP1
- TI AM335x (modified variant)
- Many ARM Versatile and Realview reference platforms

**Key registers:**

```
Offset 0x000  WDOGLOAD   — Load value (reload on kick)
Offset 0x004  WDOGVALUE  — Current counter value
Offset 0x008  WDOGCONTROL — Enable, interrupt enable
Offset 0x00C  WDOGINTCLR — Clear interrupt / kick the dog
Offset 0x010  WDOGRIS    — Raw interrupt status
Offset 0x014  WDOGMIS    — Masked interrupt status
Offset 0xC00  WDOGLOCK   — Write 0x1ACCE551 to unlock registers
```

The Linux driver is `drivers/watchdog/sp805_wdt.c`. It registers as a platform
driver and exposes the standard `watchdog_device` interface. Load via:

```bash
modprobe sp805_wdt
```

The driver reads the clock frequency from the device tree node
(`arm,sp805-wdt` compatible string) and computes the timeout register value
accordingly.

### Synopsys DesignWare WDT (dw_wdt)

The Synopsys DesignWare WDT is found on:

- Rockchip RK3288, RK3399, RK3568, RK3588
- HiSilicon Hi3516/Hi3519
- Bitmain BM1684 (Sophon AI)
- Many MIPS and RISC-V platforms as well

Linux driver: `drivers/watchdog/dw_wdt.c`. Compatible string:
`snps,dw-wdt`.

The DW WDT has a pre-programmed timeout table; the driver selects the closest
period to the requested timeout. Minimum timeout is typically 1 second,
maximum up to 2^31 clock cycles.

### Linux /dev/watchdog Interface

All hardware WDT drivers that register through the Linux watchdog framework
expose a character device at `/dev/watchdog` (or `/dev/watchdog0`,
`/dev/watchdog1` for multiple WDTs).

**Key ioctls:**

```c
WDIOC_GETSUPPORT    — struct watchdog_info: identity, firmware, options
WDIOC_GETSTATUS     — get status flags
WDIOC_GETBOOTSTATUS — did WDT cause last reboot?
WDIOC_SETOPTIONS    — WDIOS_ENABLECARD / WDIOS_DISABLECARD
WDIOC_KEEPALIVE     — kick the watchdog
WDIOC_SETTIMEOUT    — set timeout (seconds)
WDIOC_GETTIMEOUT    — get current timeout
WDIOC_GETTIMELEFT   — time remaining before reset
```

**Magic close:** Writing the magic character `V` to `/dev/watchdog` and then
closing the file disarms the WDT cleanly (if `nowayout` is not set). Closing
without writing `V` keeps the WDT armed — essential so that a crashed daemon
does not accidentally disarm the watchdog on its way down.

### QEMU virt Machine

When developing on QEMU's `virt` machine type (common for ARM testing), a
hardware WDT is not present by default. You can add one with:

```
-device virtio-rng-pci  # not WDT — example of device syntax
-device i6300esb,id=watchdog0 -watchdog-action reset
```

The `i6300esb` is an emulated Intel 6300ESB WDT (works fine for driver
testing). Alternatively, use `iTCO_wdt` with QEMU Q35 machine:

```
-machine q35 -device iTCO_wdt
```

The Linux driver `i6300esb.c` or `iTCO_wdt.c` will bind to these devices.

---

## A/B Rootfs Partitioning

### Partition Layout

A robust A/B partition scheme for an ARM board with eMMC storage:

```
Device: /dev/mmcblk0

+-----------+--------+----------+----------------------------------------------+
| Partition | Size   | Label    | Purpose                                      |
+-----------+--------+----------+----------------------------------------------+
| mmcblk0   | raw    | MBR/GPT  | Partition table                              |
| mmcblk0boot0 | 4MB | boot0    | U-Boot SPL (eMMC hardware boot partition)    |
| mmcblk0boot1 | 4MB | boot1    | U-Boot SPL (redundant copy)                  |
| mmcblk0p1 | 64MB   | boot     | FAT32: FIT image, DTB, GRUB/U-Boot env       |
| mmcblk0p2 | 2GB    | rootfs-A | ext4 or EROFS: root filesystem, slot A       |
| mmcblk0p3 | 2GB    | rootfs-B | ext4 or EROFS: root filesystem, slot B       |
| mmcblk0p4 | 512MB  | data     | ext4: persistent user data (/data, /home)    |
| (remaining)| rest  | reserved | OTA staging / factory reset image            |
+-----------+--------+----------+----------------------------------------------+
```

**Notes:**
- The eMMC hardware boot partitions (`boot0`/`boot1`) are separate from GPT
  partitions and are accessed via a special interface. U-Boot SPL lives here.
- The `boot` FAT32 partition holds the bootloader environment and kernel FIT
  images. Having it separate from rootfs means it is never overwritten during
  a rootfs OTA update.
- `data` holds user state, configuration overlays, and application databases.
  It persists across rootfs updates.

### Slot State Machine

Each slot (A or B) is in exactly one state at any time:

```
                         OTA writes to B
                                │
    ┌──────────┐          ┌─────▼──────┐
    │          │          │            │
    │  active  │◄─────────│  standby   │
    │          │  boot ok │            │
    └────┬─────┘          └─────┬──────┘
         │                      │
         │  mark-pending        │ mark-pending
         │                      │
    ┌────▼──────┐         ┌─────▼──────┐
    │           │         │            │
    │  pending  │────────►│  failed    │
    │           │  WDT/   │            │
    └───────────┘  boot   └────────────┘
                   limit
```

**State transitions:**

1. `standby → pending`: OTA agent writes new rootfs to slot B, calls
   `ab-partition-setup.sh --mark-pending B`, then reboots.
2. On next boot: U-Boot sees `slot_B_state=pending`, increments `bootcount`,
   boots from partition B.
3. If boot succeeds: init system calls `ab-partition-setup.sh --mark-active B`,
   state becomes `active`. Old slot A becomes `standby`.
4. If boot fails: `bootcount` reaches `bootlimit`, U-Boot's `altbootcmd`
   runs, marks B as `failed`, boots from A.

### U-Boot Environment Variables

```
slot_a_state=active        # or: standby | pending | failed
slot_b_state=standby       # or: standby | pending | failed
bootslot=A                 # currently selected slot
bootcount=0                # incremented each boot, reset on success
bootlimit=3                # max attempts before fallback
```

---

## U-Boot Bootcount Mechanism

### Overview

U-Boot has built-in bootcount support (`CONFIG_BOOTCOUNT_LIMIT=y`). At every
boot, U-Boot reads the current `bootcount` from its environment (or a dedicated
register in some SoCs) and increments it. If `bootcount >= bootlimit`, U-Boot
executes `altbootcmd` instead of `bootcmd`.

### Key Configuration Options

```makefile
CONFIG_BOOTCOUNT_LIMIT=y        # Enable bootcount feature
CONFIG_BOOTCOUNT_ENV=y          # Store in U-Boot environment (eMMC/NOR)
# Alternative: CONFIG_BOOTCOUNT_RAM=y (volatile, survives warm reset only)
# Alternative: CONFIG_BOOTCOUNT_EXT_SYSRESET=y (hardware register on some SoCs)
CONFIG_BOOTLIMIT=3              # Default boot attempt limit
CONFIG_SYS_BOOTCOUNT_ADDR=...  # For RAM-based: physical address
```

### Flow Diagram

```
Power on
    │
    ▼
U-Boot SPL (from eMMC boot partition)
    │
    ▼
U-Boot proper
    │
    ├─ Read bootcount from env
    ├─ Increment bootcount
    ├─ Save env
    │
    ├─ Is bootcount >= bootlimit?
    │       │ YES                       NO
    │       ▼                            ▼
    │  altbootcmd                   bootcmd
    │  (switch slot,              (boot active slot)
    │   reset bootcount)
    │
    ▼
Linux boots
    │
    ▼
Systemd/init calls verify_boot
    │
    ├─ Health checks pass?
    │       │ YES                       NO
    │       ▼                            ▼
    │  fw_setenv bootcount 0         reboot
    │  fw_setenv slot_X_state active
    │
    ▼
System operational
```

### Redundant Environment Storage

To survive a power cut during environment write, U-Boot supports two
environment copies in flash (`CONFIG_ENV_REDUNDANT=y`). Each copy has a CRC32
and a "flags" byte. On write, the inactive copy is updated first, then the
flags swapped atomically. If power fails mid-write, the old copy with valid
CRC is used.

---

## Secure Boot and TrustZone

### ARM Trusted Firmware Chain of Trust

ARM Trusted Firmware-A (TF-A) implements a layered boot process where each
stage verifies the next using cryptographic signatures. This chain is rooted in
hardware — a one-time-programmable (OTP) register holding the SHA-256 hash of
the Root of Trust Public Key (ROTPK).

```
┌─────────────────────────────────────────────────────────────────┐
│  SoC Power-On Reset                                             │
│                                                                 │
│  BL1 (Boot ROM / First Stage)                                   │
│  ├── Loaded from: on-chip ROM (immutable)                       │
│  ├── Verifies: BL2 signature against ROTPK in OTP fuses         │
│  └── Loads BL2 from: NOR flash / eMMC boot partition            │
│                          │                                      │
│                          ▼                                      │
│  BL2 (Second Stage)                                             │
│  ├── Loaded by BL1, runs in Secure EL1                          │
│  ├── Verifies: BL31, BL32, BL33 certificates                    │
│  ├── Sets up: memory regions, DRAM training                     │
│  └── Loads: BL31 (EL3 runtime), BL32 (OP-TEE), BL33 (U-Boot)  │
│                          │                                      │
│                          ▼                                      │
│  BL31 (EL3 Runtime / ARM Trusted Firmware Runtime)              │
│  ├── Runs permanently in EL3 (highest privilege)                │
│  ├── Provides: SMC (Secure Monitor Call) interface              │
│  ├── Manages: CPU power states (PSCI)                           │
│  └── Transfers control to BL33                                  │
│                          │                                      │
│  BL32 (Trusted OS — OP-TEE)          ← optional                │
│  ├── Runs in Secure EL1                                         │
│  ├── Provides: Trusted Execution Environment (TEE)              │
│  └── Hosts: Trusted Applications (key storage, crypto)         │
│                          │                                      │
│                          ▼                                      │
│  BL33 (Non-Secure World Bootloader — U-Boot)                    │
│  ├── Runs in Non-Secure EL2/EL1                                 │
│  ├── Verifies: Linux kernel FIT image signature                 │
│  ├── Loads: kernel + DTB + initramfs                            │
│  └── Boots Linux                                                │
│                          │                                      │
│                          ▼                                      │
│  Linux Kernel (Non-Secure EL1)                                  │
│  ├── Kernel lockdown mode (if enabled)                          │
│  └── OP-TEE driver: /dev/tee0, tee-supplicant                  │
└─────────────────────────────────────────────────────────────────┘
```

### FIT Image Signature Verification

U-Boot verifies the kernel using a Flattened Image Tree (FIT) signed with RSA.

```bash
# Create FIT image with signature
mkimage -f kernel.its -k keys/ -K u-boot.dtb -r kernel.itb

# FIT image structure (kernel.its):
/dts-v1/;
/ {
    description = "ARM kernel FIT";
    images {
        kernel {
            type = "kernel";
            data = /incbin/("Image.gz");
            algo = "sha256,rsa4096";
            key-name-hint = "dev";
        };
        fdt-1 { type = "flat_dt"; data = /incbin/("board.dtb"); };
    };
    configurations {
        default = "conf-1";
        conf-1 {
            kernel = "kernel";
            fdt = "fdt-1";
            signature { algo = "sha256,rsa4096"; key-name-hint = "dev"; };
        };
    };
};
```

### OP-TEE (Open Portable Trusted Execution Environment)

OP-TEE runs as BL32 in the Secure World and provides:

- **Secure storage**: Keys and secrets encrypted with device-unique hardware key
- **Crypto API**: AES, RSA, ECDSA, SHA operations in isolation from Linux
- **Trusted Applications (TAs)**: Small programs that run in Secure EL1

Linux communicates with OP-TEE via the `/dev/tee0` device and the
`tee-supplicant` daemon (which handles TA loading from the normal-world
filesystem into the TEE).

### Kernel Lockdown Mode

Linux kernel lockdown (`CONFIG_SECURITY_LOCKDOWN_LSM=y`) restricts what root
can do when Secure Boot is active:

- Disables `/dev/mem` and `/dev/kmem` access
- Prevents loading unsigned kernel modules
- Blocks hibernation (state written to unverified storage)
- Restricts eBPF with JIT

Check current state: `cat /sys/kernel/security/lockdown`

---

## Quick Start

### 1. Set Up Hardware Watchdog

```bash
# Check WDT hardware and status
bash wdt-setup.sh --status

# Start keepalive daemon (production use)
sudo bash wdt-setup.sh --device /dev/watchdog0 --timeout 60 --daemon

# Test WDT without committing (dry-run)
bash wdt-setup.sh --test --dry-run
```

### 2. Initialize A/B Partition State

```bash
# Show current slot status
bash ab-partition-setup.sh --status

# Set bootlimit to 3 attempts
sudo bash ab-partition-setup.sh --set-bootlimit 3

# Mark slot B as pending (after OTA write)
sudo bash ab-partition-setup.sh --mark-pending B

# After successful boot on B, confirm it
sudo bash ab-partition-setup.sh --mark-active B
```

### 3. Load U-Boot Bootcount Environment

```bash
# Load the bootcount/A-B env snippet into U-Boot
sudo fw_setenv -s uboot-bootcount.env

# Verify
fw_printenv bootlimit bootcount altbootcmd
```

### 4. Verify Secure Boot Chain

```bash
# Check all secure boot indicators
bash secure-boot-verify.sh

# Check with FIT image path
bash secure-boot-verify.sh --fit-image /boot/kernel.itb
```

### 5. Run Tests

```bash
cd tests/
python3 -m pytest test_boot_resilience.py -v
```

---

## File Reference

| File | Purpose | Lines |
|---|---|---|
| `wdt-setup.sh` | Configure and validate hardware WDT | ~200 |
| `ab-partition-setup.sh` | Manage A/B partition state machine | ~250 |
| `uboot-bootcount.env` | U-Boot env for bootcount-based failover | ~60 |
| `secure-boot-verify.sh` | Verify Secure Boot chain of trust | ~150 |
| `tests/test_boot_resilience.py` | Python tests (no hardware required) | ~200 |

---

## References

- ARM SP805 Watchdog TRM: ARM DDI0270B
- Linux WDT API: `Documentation/watchdog/watchdog-api.rst`
- Linux WDT drivers: `drivers/watchdog/sp805_wdt.c`, `dw_wdt.c`
- ARM Trusted Firmware-A: https://trustedfirmware-a.readthedocs.io/
- OP-TEE Documentation: https://optee.readthedocs.io/
- U-Boot Bootcount: `doc/README.bootcount`
- U-Boot FIT signatures: `doc/uImage.FIT/signature.txt`
- Linux Kernel Lockdown: `Documentation/admin-guide/LSM/lockdown.rst`
