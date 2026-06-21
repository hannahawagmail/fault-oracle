<!-- SPDX-License-Identifier: Apache-2.0 -->
# Glossary

Definitions of every acronym and term used across this repository, ordered alphabetically.

---

**AER** — *Advanced Error Reporting*. A PCIe capability that allows devices and the
root complex to report correctable, non-fatal uncorrectable, and fatal uncorrectable
errors through standardised register sets. Linux surfaces AER data at
`/sys/bus/pci/devices/<BDF>/aer_dev_*`. See `docs/aer-primer.md`.

**APEI** — *ACPI Platform Error Interface*. An ACPI subsystem that standardises how
firmware communicates hardware errors to the OS. Includes GHES, BERT, EINJ, and ERST
tables. ARM servers use APEI/GHES to deliver Machine Check Events.

**ARM TF-A** — *Trusted Firmware-A*. The open-source reference implementation of the
ARM Trusted Boot architecture. Provides BL1, BL2, BL31 (EL3 runtime firmware), and
optionally BL32 (Secure-EL1 payload, typically OP-TEE).

**ASIL** — *Automotive Safety Integrity Level*. A risk classification in ISO 26262.
ASIL-A (lowest) through ASIL-D (highest). See `docs/adr/` for the safety analysis of
the watchdog and OTA modules.

**ATF** — See *ARM TF-A*.

**BDF** — *Bus:Device.Function*. The PCI address tuple identifying a specific PCIe
endpoint, e.g. `0000:01:00.0`. Used as a label in the AER Prometheus metrics.

**BERT** — *Boot Error Record Table*. An ACPI table that stores hardware errors that
occurred before the OS booted and couldn't be reported at the time.

**BL1/BL2/BL31/BL33** — Boot Loader stages in ARM Trusted Firmware.
- **BL1** — First-stage ROM bootloader; sets up secure world.
- **BL2** — Loads BL31 and BL33; runs in S-EL1.
- **BL31** — EL3 runtime firmware; handles SMC calls and secure monitor.
- **BL33** — Normal-world bootloader (e.g. U-Boot, UEFI); runs in EL2/EL1.

**CE** — *Correctable Error*. A memory or bus error that hardware ECC can detect and
correct without data loss. Tracked by EDAC as `ce_count` per channel. A sustained
CE rate is a leading indicator of DIMM failure.

**CCIX** — *Cache Coherent Interconnect for Accelerators*. A cache-coherent protocol
built on PCIe. Used on Ampere Altra for CPU-to-accelerator RAS integration.

**csrow** — *Chip-Select Row*. An EDAC abstraction for a physical memory rank group
within a memory controller. Corresponds to one or more DRAM chip-select signals.
Sysfs path: `/sys/devices/system/edac/mc<N>/csrow<M>/`.

**DIMM** — *Dual Inline Memory Module*. A standardised DRAM package. In EDAC sysfs
a DIMM is identified by its csrow and channel indices.

**DTB** — *Device Tree Blob*. A compiled binary description of hardware topology passed
from firmware to the kernel at boot, used on ARM platforms instead of ACPI discovery.

**DUE** — *Detected Uncorrectable Error*. A UE that the system detected and handled
(e.g. by panicking or page-offlining) as opposed to a silent data corruption (SDC).

**ECC** — *Error Correcting Code*. A class of codes (commonly SECDED for DRAM) that
can detect and correct single-bit errors and detect (but not correct) multi-bit errors.

**EDAC** — *Error Detection And Correction*. The Linux kernel subsystem (under
`drivers/edac/`) that collects hardware memory error counts from platform EDAC drivers
and exposes them via sysfs. See `docs/edac-primer.md`.

**EINJ** — *Error Injection*. An ACPI/APEI mechanism for injecting simulated hardware
errors through firmware. Used by `fault-injection/inject_aer.sh` on supported platforms.

**EL** — *Exception Level*. ARM privilege levels: EL0 (user), EL1 (kernel), EL2
(hypervisor), EL3 (secure monitor / TF-A BL31).

**FMEA** — *Failure Mode and Effects Analysis*. A systematic method for identifying
failure modes, their causes, and their effects. Used in automotive (ISO 26262) and
other safety-critical contexts.

**GHES** — *Generic Hardware Error Source*. An APEI mechanism through which firmware
delivers hardware error records to the OS at runtime via ACPI error source descriptors.
On ARM64 servers this is the primary path for MCEs.

**HAL** — *Hardware Abstraction Layer*. An interface layer that decouples higher-level
logic from hardware-specific implementations. See Architecture task #21.

**ISO 26262** — Functional safety standard for road vehicles. Defines ASIL levels and
the safety lifecycle for automotive software and hardware.

**LPDDR4X** — *Low Power DDR4 Extended*. A DRAM standard common in embedded and mobile
ARM designs (Raspberry Pi 4, NXP i.MX8). Most LPDDR4X implementations do not expose
ECC to software.

**MCE** — *Machine Check Event* (or *Machine Check Exception*). A hardware-detected
error event reported through the Machine Check Architecture (MCA). On ARM, delivered
via APEI/GHES. On x86, delivered via the MSR-based MCA interface and `/dev/mcelog`.

**MCTP** — *Management Component Transport Protocol*. A protocol used in server BMC
(Baseboard Management Controller) communication over PCIe, SMBus, or USB. Relevant
for out-of-band fault reporting in rack servers.

**numactl** — A Linux utility for controlling NUMA (Non-Uniform Memory Access) policy.
Used in the topology-aware remediation path to exclude failed DIMMs from allocation.

**OCI** — *Open Container Initiative*. The standard governing container image format
and runtime. Container images in this repo follow OCI image spec labels.

**OP-TEE** — *Open Portable Trusted Execution Environment*. An open-source Trusted OS
running in ARM TrustZone (Secure-EL1). Used with TF-A as BL32.

**OTA** — *Over-The-Air*. A software update delivered remotely, typically to an embedded
device. This repository uses RAUC for atomic A/B OTA updates.

**PCR** — *Platform Configuration Register*. A TPM register that accumulates
cryptographic measurements of boot components. Used with TPM-sealed keys to enforce
boot policy (unseal only when PCRs match expected values).

**PCIe** — *Peripheral Component Interconnect Express*. The dominant high-speed serial
bus standard. AER is a PCIe native capability for error reporting.

**PXE** — *Preboot Execution Environment*. Network booting; not directly used in this
repo but relevant for fleet provisioning context.

**RAS** — *Reliability, Availability, and Serviceability*. A collective term for
hardware and software techniques that detect, report, and recover from faults.

**RAUC** — *Robust Auto-Update Controller*. An open-source update framework for
embedded Linux systems. Supports A/B (redundant) partition switching with cryptographic
bundle signatures. Used in `ota-updates/`.

**RAS Extension** — ARM's Reliability, Availability, and Serviceability Extension
(ARMv8.2+). Adds standardised registers (`ERXSTATUS_EL1`, `ERXADDR_EL1`, etc.) for
reporting hardware errors. Alternative to EDAC on newer ARM cores.

**SDC** — *Silent Data Corruption*. A data error that is not detected by any hardware
or software mechanism. The most dangerous class of memory error.

**SECDED** — *Single Error Correction, Double Error Detection*. The most common ECC
code for DRAM. Corrects all single-bit errors; detects (but cannot correct) all
double-bit errors.

**SoC** — *System on Chip*. An integrated circuit containing CPU, memory controller,
I/O interfaces, and often GPU and other peripherals on a single die.

**SPDX** — *Software Package Data Exchange*. A standard for communicating software bill
of materials information. SPDX license identifiers (e.g. `Apache-2.0`) are used as
header comments in all source files in this repository.

**SP805** — ARM Watchdog module (PrimeCell SP805). A hardware watchdog IP block
commonly found in ARM reference designs and SoCs. The `sp805_wdt` Linux driver
controls it.

**sysfs** — The Linux virtual filesystem (`/sys`) that exposes kernel data structures
to userspace. EDAC error counts, PCIe AER counters, and watchdog state are all
accessed via sysfs in this repository.

**TLB** — *Translation Lookaside Buffer*. A cache for virtual-to-physical address
translations. TLB errors are a class of MCE on some architectures.

**TPM** — *Trusted Platform Module*. A hardware chip or firmware implementation
providing secure key storage, attestation, and PCR measurement. Used in the
TPM-backed OTA signing key flow (task #39).

**TrustZone** — ARM's hardware security extension. Divides the SoC into a Secure World
(running TF-A and OP-TEE) and a Normal World (running Linux).

**UE** — *Uncorrectable Error*. A memory error that ECC cannot correct. Typically
causes a kernel panic or machine check to prevent data corruption. Tracked by EDAC
as `ue_count` per csrow.

**U-Boot** — *Das U-Boot*. A widely used open-source bootloader for embedded systems.
Implements the A/B boot switch (`fw_setenv`/`fw_printenv`) used by `boot-resilience/`.

**vmcore** — A kernel crash dump file created by `kdump` when the kernel panics.
Contains a snapshot of RAM at the time of the crash. Used for post-mortem analysis
with `crash` or `gdb`.

**WDT** — *Watchdog Timer*. A hardware counter that resets or reboots the system if
not periodically refreshed ("kicked") by software. Used in `boot-resilience/wdt-setup.sh`
to ensure the system recovers from software hangs.
