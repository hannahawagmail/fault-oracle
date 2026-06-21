<!-- SPDX-License-Identifier: Apache-2.0 -->
# DPU EDAC Integration — Applying Fault Resilience to SmartNIC ARM Cores

This document explains how the EDAC (Error Detection and Correction) fault
resilience techniques from the main `edac-reference/` module apply to the ARM
Linux control plane OS running on a DPU (Data Processing Unit). The DPU hosts
its own DRAM, ARM cores, and Linux kernel — the same EDAC sysfs interface
works identically here.

---

## Table of Contents

1. [DPU DRAM Topology](#dpu-dram-topology)
2. [Loading edac_cortex_ref.ko on DPU ARM Linux](#loading-edac_cortex_refko)
3. [Expected sysfs Tree on DPU](#expected-sysfs-tree-on-dpu)
4. [Prometheus Scrape Configuration](#prometheus-scrape-configuration)
5. [Alert Rules for DPU Nodes](#alert-rules-for-dpu-nodes)
6. [CE Rate and Packet Corruption Risk](#ce-rate-and-packet-corruption-risk)
7. [Fault Injection on DPU Using QEMU](#fault-injection-on-dpu-using-qemu)
8. [DPU-Specific Kernel Considerations](#dpu-specific-kernel-considerations)
9. [Feature Matrix: DPU vs Host](#feature-matrix-dpu-vs-host)

---

## DPU DRAM Topology

A modern DPU integrates two distinct memory subsystems that serve different
functions. Understanding their separation is essential before configuring EDAC
monitoring.

### Packet Buffer Pool (ASIC-Managed)

The DPU ASIC maintains a large pool of LPDDR5 ECC DRAM dedicated to the data
plane. Typical allocation by generation:

| DPU                      | Packet Buffer DRAM | ECC type        |
|--------------------------|--------------------|-----------------|
| NVIDIA BlueField-3       | 32 GB LPDDR5       | SECDED (72-bit) |
| AMD Pensando DSC-200     | 8 GB LPDDR4        | SECDED (72-bit) |
| Marvell OCTEON 10 CN106  | 16 GB LPDDR5       | SECDED (72-bit) |

**This memory is NOT visible to the Linux kernel.** The ASIC firmware manages
it directly via DMA engines. ECC error counts for packet buffer DRAM are
accessible only through vendor-specific management APIs (e.g., NVIDIA `mst`
tools for BlueField, Pensando `penctl` for DSC-200). Linux EDAC does not cover
this pool.

### ARM Core Heap (Linux-Managed)

Each DPU also allocates a portion of LPDDR5 ECC DRAM for the ARM core cluster
to use as its working memory. This is the memory that runs the DPU Linux OS,
user-space control plane applications (OvS, BGP daemons, telemetry agents),
and the `hw-fault-exporter` binary.

| DPU                      | ARM DRAM allocation | DRAM type  |
|--------------------------|---------------------|------------|
| NVIDIA BlueField-3       | 16 GB (of 32 GB)    | LPDDR5 ECC |
| AMD Pensando DSC-200     | 4 GB (of 8 GB)      | LPDDR4 ECC |
| Marvell OCTEON 10 CN106  | 8 GB (of 16 GB)     | LPDDR5 ECC |

**This is the DRAM that Linux EDAC monitors.** The ARM cores (typically
Cortex-A72 or Cortex-A78) include a DDR memory controller with ECC support.
When the DPU kernel boots with `edac_cortex_ref.ko` loaded, it exposes CE/UE
counts through the standard `/sys/devices/system/edac/` interface — identically
to how it works on a host ARM64 server.

The key implication: **DPU EDAC only covers control-plane memory correctness,
not packet-buffer correctness.** A DPU ARM core memory error could corrupt
OvS flow tables or BPF programs. A packet-buffer memory error requires
vendor-specific monitoring and is out of scope for Linux EDAC.

---

## Loading edac_cortex_ref.ko on DPU ARM Linux

The procedure is identical to loading the module on a host ARM64 system because
the DPU ARM cores are standard Cortex-A72 or Cortex-A78 IP blocks — the same
silicon that appears in Ampere Altra and AWS Graviton3 host CPUs.

### Prerequisites

- DPU running ARM Linux (aarch64 kernel, typically 5.10 LTS or 5.15 LTS)
- Root access via SSH on DPU management port or serial console
- `edac_cortex_ref.ko` built for the specific DPU kernel version
  (kernel version must match: `uname -r` on DPU = build target)

### Load the Module

```bash
# Transfer module to DPU (from build host)
scp edac_cortex_ref.ko admin@<dpu-mgmt-ip>:/tmp/

# On the DPU
ssh admin@<dpu-mgmt-ip>

# Load with 2 chip-select rows, 2 channels (typical DPU memory topology)
sudo insmod /tmp/edac_cortex_ref.ko nr_csrows=2 nr_channels=2

# Verify module loaded and EDAC controller registered
dmesg | tail -20 | grep -i edac
ls /sys/devices/system/edac/mc/
```

### Persist Across Reboots

```bash
# Install to kernel module directory
sudo cp /tmp/edac_cortex_ref.ko \
    /lib/modules/$(uname -r)/extra/edac_cortex_ref.ko
sudo depmod -a

# Load at boot via modprobe configuration
echo "edac_cortex_ref nr_csrows=2 nr_channels=2" \
    | sudo tee /etc/modprobe.d/edac-dpu.conf

# Enable module loading at boot
echo "edac_cortex_ref" | sudo tee /etc/modules-load.d/edac.conf
```

---

## Expected sysfs Tree on DPU

The EDAC sysfs layout on a DPU ARM Linux system is identical to a host ARM64
system. This is intentional — the same Cortex-A72/A78 memory controller
generates the same sysfs interface.

After loading `edac_cortex_ref.ko`, the following tree appears:

```
/sys/devices/system/edac/
└── mc/
    └── mc0/                          ← Memory controller 0 (ARM DDR controller)
        ├── ce_count                  ← Total correctable errors since boot
        ├── ue_count                  ← Total uncorrectable errors since boot
        ├── ce_noinfo_count           ← CE with no address info
        ├── ue_noinfo_count           ← UE with no address info
        ├── mc_name                   ← "Cortex-A72 EDAC" (reference module)
        ├── size_mb                   ← Total DRAM under this controller (MB)
        ├── edac_mode                 ← "SECDED" (single-bit correct, double-bit detect)
        ├── mem_type                  ← "LPDDR5" or "LPDDR4" depending on DPU
        ├── dev_type                  ← "x8" (device width in bits)
        ├── csrow0/                   ← Chip-select row 0
        │   ├── ce_count
        │   ├── ue_count
        │   ├── size_mb
        │   ├── ch0_ce_count          ← Channel 0 correctable errors
        │   ├── ch1_ce_count          ← Channel 1 correctable errors
        │   ├── ch0_dimm_label        ← "DPU-LPDDR5-CH0"
        │   └── ch1_dimm_label        ← "DPU-LPDDR5-CH1"
        └── csrow1/                   ← Chip-select row 1
            ├── ce_count
            ├── ue_count
            └── ...
```

Read CE count manually:

```bash
cat /sys/devices/system/edac/mc0/ce_count
cat /sys/devices/system/edac/mc0/csrow0/ch0_ce_count
```

---

## Prometheus Scrape Configuration

The `hw-fault-exporter` running on the DPU listens on port 9200 (distinct from
the host exporter on 9100). The host Prometheus server scrapes both.

Add this block to the **host's** `prometheus.yml` under `scrape_configs`:

```yaml
scrape_configs:
  # --- Host hw-fault-exporter (port 9100) ---
  - job_name: 'hw-fault-exporter-host'
    static_configs:
      - targets: ['localhost:9100']
    labels:
      role: 'host'

  # --- DPU hw-fault-exporter (port 9200) ---
  - job_name: 'dpu-fault-exporter'
    static_configs:
      - targets: ['<dpu-mgmt-ip>:9200']
    relabel_configs:
      - target_label: role
        replacement: 'dpu'
      - target_label: dpu_vendor
        replacement: 'bluefield3'   # or 'pensando' / 'octeon10'
    metric_relabel_configs:
      - source_labels: [__name__]
        regex: 'edac_.*'
        target_label: subsystem
        replacement: 'dram_ecc'
```

Replace `<dpu-mgmt-ip>` with the DPU management interface IP (the dedicated
RJ45 or SFP management port, not the data-plane port).

The `relabel_configs` block attaches `role="dpu"` to every metric scraped from
the DPU exporter. This label is used by the alert rules below to distinguish
DPU DRAM errors from host DRAM errors in the same Prometheus instance.

---

## Alert Rules for DPU Nodes

Add these rules to your `alert_rules.yml` (or the `dpu_alerts.yml` group):

```yaml
groups:
  - name: dpu_fault_resilience
    rules:
      # High correctable error rate on DPU DRAM.
      # CE rate > 5/hour sustained for 10 minutes indicates accelerating wear.
      # DPU DRAM errors can corrupt OvS flow tables and BPF programs.
      - alert: DPUHighCERate
        expr: rate(edac_correctable_errors_total{role="dpu"}[1h]) > 5
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "High CE rate on DPU {{ $labels.instance }}"
          description: >
            DPU DRAM ECC errors may indicate packet buffer corruption risk.
            CE rate is {{ $value | humanize }} errors/second on {{ $labels.instance }}.
            Consider scheduling DPU replacement if rate exceeds 100/hour.

      # Any uncorrectable error on DPU DRAM is critical.
      # UE cannot be corrected and means data corruption has occurred.
      - alert: DPUUncorrectableError
        expr: increase(edac_uncorrectable_errors_total{role="dpu"}[5m]) > 0
        for: 0m
        labels:
          severity: critical
        annotations:
          summary: "Uncorrectable DRAM error on DPU {{ $labels.instance }}"
          description: >
            DPU experienced an uncorrectable ECC error. Control plane state
            may be corrupted. Immediate DPU drain and replacement required.

      # hw-fault-exporter on DPU has stopped reporting.
      # Could indicate DPU crash, network partition, or exporter failure.
      - alert: DPUManagementPlaneDown
        expr: absent(hw_fault_exporter_uptime_seconds{role="dpu"})
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "DPU management plane unreachable: {{ $labels.instance }}"
          description: >
            hw-fault-exporter on DPU has not reported for 2 minutes.
            Check: ssh admin@{{ $labels.instance }} systemctl status hw-fault-exporter-dpu

      # DPU exporter running but EDAC module not loaded.
      - alert: DPUEDACModuleNotLoaded
        expr: edac_memory_controllers_total{role="dpu"} == 0
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "EDAC module not loaded on DPU {{ $labels.instance }}"
          description: >
            The hw-fault-exporter is running but reports 0 EDAC memory controllers.
            Load the module: insmod edac_cortex_ref.ko nr_csrows=2 nr_channels=2
```

---

## CE Rate and Packet Corruption Risk

### The Two Memory Pools Revisited

Although Linux EDAC only covers the ARM core heap, the risk model for a DPU
includes both memory pools:

| Memory pool          | ECC coverage        | Linux EDAC visible | Corruption risk on error         |
|----------------------|---------------------|--------------------|----------------------------------|
| ARM core heap        | LPDDR5 SECDED       | Yes                | OvS tables, kernel state         |
| Packet buffer (ASIC) | LPDDR5 SECDED       | No (firmware)      | In-flight packet payloads        |

### How CE Rates Signal DRAM Health

LPDDR5 SECDED ECC corrects single-bit errors silently. Each correction is
logged as a CE event. CE events are normal at low rates (< 1/hour) due to
cosmic ray single-event upsets and normal background radiation in datacenter
environments. High sustained CE rates indicate:

1. **Accelerated DRAM cell wear** — typically from high temperature, high
   refresh rate, or manufacturing defects manifesting at end of DRAM lifetime.
2. **Rowhammer susceptibility** — repeated access to adjacent rows causes bit
   flips in neighboring cells; CE events are the first observable symptom.
3. **DIMM solder joint failure** — partial contact causes intermittent
   single-bit errors that appear as elevated CE rate before full failure.

### DPU-Specific Risk: Packet Buffer Adjacency

In some DPU architectures (notably BlueField-3), the ARM core heap and packet
buffer pool share the same physical LPDDR5 die, only separated by address range.
A manufacturing defect in a DRAM row that spans both regions will manifest as CE
events visible in Linux EDAC (ARM core heap side) before the same defect affects
the packet buffer pool.

This means DPU EDAC CE rate is a leading indicator of packet buffer DRAM health,
even though the packet buffer is not directly monitored by Linux EDAC.

### Threshold Guidance

| CE rate         | Interpretation                    | Recommended action                       |
|-----------------|-----------------------------------|------------------------------------------|
| 0–1 /hour       | Normal background                 | No action                                |
| 1–10 /hour      | Elevated; monitor closely         | Increase monitoring frequency to 1-min   |
| 10–100 /hour    | High; DRAM degrading              | Schedule DPU replacement within 30 days  |
| > 100 /hour     | Critical; imminent failure        | Drain DPU immediately; replace < 24h     |
| Any UE event    | Uncorrectable; data loss occurred | Emergency drain; replace immediately     |

---

## Fault Injection on DPU Using QEMU

The CI pipeline in `ci/qemu-boot.sh` launches a QEMU ARM64 VM that simulates
the DPU ARM Linux control plane. This is used to test EDAC module loading,
sysfs verification, and exporter metric collection without physical DPU hardware.

### QEMU Command for DPU Control Plane Simulation

```bash
# From the repository root:
bash ci/qemu-boot.sh \
    --arch arm64 \
    --cpus 1 \
    --memory 1G \
    --kernel vmlinuz-arm64 \
    --initrd initrd-arm64 \
    --append "console=ttyAMA0 edac_cortex_ref.nr_csrows=2 edac_cortex_ref.nr_channels=2" \
    --module edac-reference/edac_cortex_ref.ko \
    --test    edac-reference/test/verify_edac_sysfs.sh
```

Key parameters for DPU simulation:
- `--cpus 1` — DPU ARM cluster is presented as single-socket; BlueField-3 has
  16 Cortex-A78 cores but they appear as a single NUMA node to the guest
- `--memory 1G` — sufficient for control plane; production DPU uses 4–16 GB
- The QEMU `virt` machine type includes a virtio-based memory controller that
  the reference EDAC module can attach to via its stub registration path

### Running the Full DPU EDAC Test Sequence in QEMU

```bash
# Launch QEMU, run tests, capture exit code
bash ci/qemu-boot.sh --arch arm64 --cpus 1 --memory 1G \
    --test-sequence "
        insmod /modules/edac_cortex_ref.ko nr_csrows=2 nr_channels=2
        bash /tests/verify_edac_sysfs.sh --inject
        /opt/hw-fault-exporter/hw-fault-exporter --listen-addr :9200 --disable-aer &
        sleep 2
        curl -s http://localhost:9200/metrics | grep -c '^edac_'
    "
```

Expected output: EDAC sysfs verification passes, and the exporter exposes at
least 4 `edac_*` metric families (`ce_count`, `ue_count`, `mc_info`,
`scrape_duration_seconds`).

---

## DPU-Specific Kernel Considerations

### debugfs Access Restrictions

DPU Linux kernels from vendors (NVIDIA BFB images, AMD DSC OS images) are often
built with kernel lockdown enabled or with debugfs mounted with restricted
permissions. The EDAC reference module uses the standard `edac_mc_*` kernel
APIs, not debugfs directly, so lockdown mode does not prevent module loading.
However, the `/sys/kernel/debug/edac/` path (if present) may be inaccessible.

Check before loading:

```bash
# Check if CONFIG_DEBUG_FS is enabled
grep CONFIG_DEBUG_FS /boot/config-$(uname -r) 2>/dev/null \
    || zcat /proc/config.gz 2>/dev/null | grep CONFIG_DEBUG_FS

# Check kernel lockdown mode
cat /sys/kernel/security/lockdown 2>/dev/null || echo "lockdown: not present"
```

If lockdown mode is `integrity` or `confidentiality`, unsigned kernel modules
cannot be loaded. You must either:
1. Sign the module with the DPU vendor's kernel signing key (contact vendor)
2. Use a development/unlocked BFB image from NVIDIA (requires support contract)
3. Use QEMU simulation for CI; test on physical DPU only in controlled env

### CONFIG_EDAC Requirement

The DPU kernel must be compiled with `CONFIG_EDAC=y` and `CONFIG_EDAC_SUPPORT=y`
for the `edac_mc_*` APIs to be available. Check:

```bash
grep -E "CONFIG_EDAC" /boot/config-$(uname -r) 2>/dev/null | head -10
```

NVIDIA BFB images (BlueField Reference BSP) include `CONFIG_EDAC=y` starting
from BFB 3.9.0 (kernel 5.15). Earlier BFB versions may require a custom kernel
build. AMD Pensando DSC OS includes `CONFIG_EDAC=y` from DSC OS 1.40+.

### ARM64 Memory Error Routing (GHES vs EDAC)

On physical DPUs, some hardware error events are routed through GHES (Generic
Hardware Error Source) via ACPI HEST tables rather than through the ARM EDAC
path. GHES errors appear in `/sys/bus/platform/devices/GHES*/`. The reference
EDAC module uses a software-injection path that works regardless of GHES.

In production, configure both paths:

```bash
# EDAC sysfs (software-visible CE/UE counts)
cat /sys/devices/system/edac/mc0/ce_count

# GHES events (hardware-reported; may include different error classes)
cat /sys/bus/platform/devices/GHES*/error_count 2>/dev/null || true
```

The `hw-fault-exporter` collects both paths and deduplicates events using a
5-second sliding window to avoid double-counting errors reported by both.

---

## Feature Matrix: DPU vs Host

The following table summarizes which fault-resilience features work identically
on DPU and which require adaptation or are unavailable.

| Feature                              | Host ARM64          | DPU ARM Linux       | Notes                                              |
|--------------------------------------|---------------------|---------------------|----------------------------------------------------|
| EDAC sysfs (ce_count, ue_count)      | Identical           | Identical           | Same kernel API; same sysfs path                   |
| edac_cortex_ref.ko loading           | Identical           | Identical           | Same Cortex-A72/A78 core; same module parameters   |
| EDAC fault injection (software)      | Identical           | Identical           | Uses edac_mc_handle_ce() stub; no hardware needed  |
| hw-fault-exporter metrics            | Port 9100           | Port 9200           | Different port to avoid conflict                   |
| PCIe AER error collection            | Supported           | Disabled (--disable-aer) | DPU firmware manages PCIe AER, not Linux kernel |
| Prometheus scrape                    | localhost:9100      | dpu-mgmt-ip:9200    | Requires network access to DPU mgmt interface      |
| Packet buffer DRAM monitoring        | N/A                 | Not via EDAC        | Use vendor CLI (mst/penctl) for packet buffer ECC  |
| Kernel lockdown / module signing     | Usually off         | May be enforced     | Check BFB/DSC OS lockdown mode before insmod       |
| QEMU-based CI testing                | Supported           | Supported           | Same arm64 QEMU; --cpus 1, --memory 1G for DPU sim |
| GHES hardware error routing          | Optional (ACPI)     | Vendor-dependent    | BlueField-3 uses GHES; check HEST table            |
| Prometheus alert rules               | edac_alerts.yml     | dpu_alerts.yml      | Add role="dpu" label filter to all alert exprs     |
| OOM protection (OOMScoreAdjust)      | -900 (host unit)    | -900 (DPU unit)     | Identical; DPU is more memory-constrained          |
| Systemd WatchdogSec                  | 60s                 | 60s                 | Identical; both use sd_notify protocol             |
| Log forwarding to host               | N/A                 | journald → syslog   | DPU journal can forward to host rsyslog via UDP    |
