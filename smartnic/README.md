# SPDX-License-Identifier: Apache-2.0
# SmartNIC / DPU / IPU — Architecture, Offload, and Fault Resilience

This module covers the architecture, control plane setup, hardware offload
flows, and fault-resilience integration of modern SmartNICs, Data Processing
Units (DPUs), and Infrastructure Processing Units (IPUs). Examples draw on the
three-pillar design common across NVIDIA BlueField-3, AMD Pensando DSC-200, and
hyperscaler implementations such as AWS Nitro and Google Titanium.

Target audience: Staff/Principal kernel engineers at hyperscalers, datacenter
platform teams, and SRE teams responsible for DPU-managed infrastructure.

---

## Table of Contents

1. [The Datacenter Tax](#the-datacenter-tax)
2. [Evolution: Accelerated NIC → DPU](#evolution)
3. [Terminology](#terminology)
4. [Three-Pillar Hardware Design](#three-pillar-hardware-design)
   - [Pillar 1: Programmable Data Path](#pillar-1--programmable-data-path)
   - [Pillar 2: Control Plane SoC](#pillar-2--control-plane-soc)
   - [Pillar 3: Domain-Specific Accelerators](#pillar-3--domain-specific-accelerators)
5. [Hardware Offload Flow Diagrams](#hardware-offload-flow-diagrams)
   - [Normal Host Path (No DPU)](#normal-host-path-no-dpu)
   - [DPU Offload Path](#dpu-offload-path)
   - [NVMe-oF Offload Path](#nvme-of-offload-path)
6. [Fault Resilience on DPUs](#fault-resilience-on-dpus)
7. [Connection to This Repository](#connection-to-this-repository)
8. [Directory Structure](#directory-structure)

---

## The Datacenter Tax

Every server in a modern datacenter devotes a significant fraction of its
physical CPU capacity to "infrastructure functions": tasks that exist to serve
the datacenter fabric rather than the tenant workload running inside the VM or
container.

Measured across production hyperscaler fleets, infrastructure functions
typically consume **20–30% of total host CPU cycles**. The breakdown:

| Function                          | Typical CPU burn  |
|-----------------------------------|-------------------|
| vSwitch (OvS/DPDK fast path)      | 4–8 cores @ 100%  |
| IPsec/TLS offload                 | 2–4 cores @ 100%  |
| NVMe-oF / iSCSI storage I/O       | 1–3 cores @ 100%  |
| Monitoring / telemetry agents     | 0.5–1 core        |
| Hypervisor control plane          | 0.5–1 core        |

On a 128-vCPU instance type, that is 25–38 vCPUs that the customer pays for
but cannot use productively. This "datacenter tax" directly reduces the margin
available to the cloud provider and increases cost-per-unit for tenants.

SmartNICs and DPUs exist to move this tax entirely off the host CPU and onto
dedicated silicon. The tenant pays for compute; the DPU absorbs infrastructure.

---

## Evolution

### Generation 1 — Stateless Accelerated NIC (~2005–2012)

Standard PCIe NIC with hardware offload engines bolted on:
- TCP/UDP checksum offload (CSUM offload)
- TCP Segmentation Offload (TSO) / Generic Receive Offload (GRO)
- Receive-Side Scaling (RSS): spread flows across CPU cores using hardware hash
- SR-IOV: expose multiple Virtual Functions (VFs) to VMs

The host kernel still ran the full network stack. Offload only reduced per-byte
cost for well-known operations; the CPU still processed every packet in
interrupt context.

### Generation 2 — Full Offload / Bump-in-the-Wire (~2013–2018)

NIC firmware and FPGA logic expanded to run match-action forwarding:
- Open vSwitch (OvS) rules pushed into NIC firmware via TC flower / OvS-DPDK
- VXLAN/GENEVE encap/decap in hardware
- ECMP and LAG hashing in hardware
- ACL enforcement at line rate

The host kernel was no longer in the fast path for already-classified flows.
However: the NIC ran no general-purpose OS. Configuration was a sideband
channel (vendor userspace tool → firmware). No isolation guarantee from host.

### Generation 3 — DPU: Separate CPU Complex + OS (~2019–present)

Modern DPU = SmartNIC ASIC + ARM CPU cluster + independent OS:
- The ARM cores run a full embedded Linux distribution
- This "control plane OS" is completely isolated from the host tenant OS
- The host cannot read DPU memory, execute code on DPU ARM cores, or modify
  DPU network policy — even with root on the host
- DPU has its own management Ethernet port (RJ45/SFP) for out-of-band access
- DPU can be updated, rebooted, and reconfigured independently of host

This is the generation that eliminates the datacenter tax. Infrastructure
software runs on DPU ARM Linux, not on tenant CPUs.

---

## Terminology

| Term             | Used by          | Meaning                                              |
|------------------|------------------|------------------------------------------------------|
| SmartNIC         | Industry generic | Any NIC with programmable offload capability         |
| DPU              | NVIDIA           | Data Processing Unit — full ARM complex + ASIC       |
| IPU              | Intel            | Infrastructure Processing Unit — same concept        |
| Infrastructure NIC | Amazon (AWS)   | Nitro card — ASIC + microcontroller, runs Nitro hypervisor |
| MNIC / mNIC      | Broadcom/Marvell | Management NIC, older term for SmartNIC              |
| Liquid Silicon   | Fungible         | Composable ASIC approach to DPU                      |

In this document, "DPU" is used as the generic term for the full three-pillar
design (ARM cores + ASIC + accelerators), regardless of vendor branding.

---

## Three-Pillar Hardware Design

Every production DPU at hyperscale combines three hardware subsystems. Removing
any one pillar degrades the design from DPU to merely a smart NIC.

```
┌─────────────────────────────────────────────────────────────────┐
│                         DPU CHIP                                 │
│                                                                  │
│  ┌──────────────────┐  ┌──────────────────┐  ┌───────────────┐  │
│  │  Programmable    │  │  Control Plane   │  │  Domain-Spec  │  │
│  │  Data Path       │  │  SoC (ARM Linux) │  │  Accelerators │  │
│  │  (ASIC/FPGA)     │  │                  │  │               │  │
│  │                  │  │  8-16 Cortex-A78 │  │  Crypto       │  │
│  │  Match-Action    │  │  LPDDR5 ECC DRAM │  │  NVMe-oF      │  │
│  │  Pipeline        │◄─►  OvS / eBPF     │  │  Compress     │  │
│  │  400G / 800G     │  │  Telemetry       │  │  RegEx        │  │
│  │  <1µs latency    │  │  Policy Engine   │  │               │  │
│  └──────┬───────────┘  └──────────────────┘  └───────────────┘  │
│         │                                                         │
└─────────┼───────────────────────────────────────────────────────┘
          │
     ┌────┴────┐                        ┌──────────────┐
     │ Network │                        │  Host CPU    │
     │  Wire   │                        │  (tenant)    │
     │400G/800G│                        │  PCIe Gen5   │
     └─────────┘                        └──────────────┘
```

### Pillar 1 — Programmable Data Path

The data-path ASIC is the highest-performance component. It operates on every
packet that enters or exits the DPU, making forwarding decisions in hardware
without involving the ARM cores.

**Processing model: Match-Action Pipeline**

Conceptually equivalent to P4's v1model architecture (see
`offload/p4-simple-forward.p4` for a reference implementation):

```
Packet → Parser → Ingress Match-Action → Traffic Manager → Egress Match-Action → Deparser
```

Each stage reads packet headers, looks up tables populated by the control
plane (running on the ARM SoC), and applies actions: forward, drop, encap,
decap, modify DSCP, mirror, etc.

**Protocol support (hardware, not software):**
- Ethernet (IEEE 802.3) — L2 parsing and MAC lookup
- IEEE 802.1Q / 802.1ad — VLAN / QinQ
- IPv4, IPv6 — routing, ACL, ECMP/WCMP hash
- TCP, UDP, SCTP — 5-tuple parsing and tracking (limited stateful)
- VXLAN (UDP/4789) — tunnel encap/decap for overlay networks
- GENEVE (UDP/6081) — flexible tunnel with option fields
- GRE — generic routing encapsulation
- MPLS — label switching for WAN integration
- RoCEv2 — RDMA over Converged Ethernet (ECN, CNP generation in HW)

**Forwarding performance:**
- Current generation (2024): 400 Gbps full-duplex line rate
- Next generation (2025): 800 Gbps with QSFP-DD / OSFP optics
- Deterministic forwarding latency: **<1 µs** end-to-end through the pipeline
- Compare: Linux kernel network stack: 50–100 µs (softirq processing, qdisc,
  skb allocation, TCP stack, socket copy)
- Hardware hash for ECMP across up to 64 ECMP paths, CRC32/CRC16 selectable

**Specific silicon examples:**
- NVIDIA ConnectX-7 ASIC: 400G, supports HW OvS offload + RoCEv2 + RDMA
- AMD Pensando DSC-200: P4-programmable pipeline, Capri ASIC, 100G line rate
- Marvell OCTEON 10: ARM-based DPU with DPDK-accelerated data path, 100G
- Intel IPU E2100: 200G, P4-programmable, runs IDPF virtual function driver

**Flow offload to ASIC:**

The TC (traffic control) subsystem in Linux exposes a hardware offload API:
`TC_SETUP_CLSFLOWER` for TC flower filter offload. When OvS is configured with
`hw-offload=true`, it programs match-action rules into the ASIC via this API.
Rules matched in hardware never traverse the kernel's `softirq` path.

On high-traffic nodes:
- Offloaded flows: 0% host CPU, <1 µs latency
- Slow-path (cache miss, new flow): ~100 µs, one packet per new flow hits host

### Pillar 2 — Control Plane SoC

The ARM SoC is the "brain" of the DPU — a general-purpose compute cluster
running embedded Linux that manages the ASIC data path, enforces policy, and
runs telemetry.

**Typical ARM cluster (2024 products):**
- NVIDIA BlueField-3: 16× ARM Cortex-A78 cores, 32GB LPDDR5 ECC DRAM
- AMD Pensando DSC-200: Custom MIPS cluster (older gen) → ARM in DSC-25
- Marvell OCTEON 10: 24× ARM Neoverse N2 cores, up to 32GB LPDDR5
- Intel IPU E2100: 16× ARM Cortex-A55 cores, 16GB LPDDR5 ECC

**Software running on DPU ARM Linux:**

```
DPU ARM Linux (e.g., Ubuntu 22.04 aarch64 / vendor BSP)
├── ovs-vswitchd          — OvS control plane, programs ASIC flow tables
├── ovsdb-server          — OvS database
├── frr / bird            — BGP/OSPF routing daemon
├── strongswan / wireguard — IPsec/WireGuard key management
├── hw-fault-exporter     — This repo's Prometheus exporter (port 9200)
├── node_exporter         — Standard node metrics (port 9100)
├── containerd            — Container runtime for management workloads
└── custom agents         — Vendor telemetry, firmware update daemon
```

**Security isolation architecture:**

The isolation between DPU ARM Linux and host tenant OS is enforced at multiple
hardware layers:

```
Host Tenant (VM/Container)
     │
     │  PCIe VirtIO-net / VirtIO-blk (data plane only)
     │  IOMMU enforced: host can only DMA to its own memory regions
     ▼
DPU PCIe Endpoint
     │  PCIe Access Control Services (ACS): P2P blocked
     │  IOMMU on DPU side: host VF cannot see DPU ARM memory
     ▼
DPU ARM Linux
     │  No shared memory with host OS
     │  No shared interrupt domain
     │  Separate DRAM, separate page tables
     ▼
DPU Management Network (OOB)
     │  Separate physical Ethernet port (RJ45 or SFP28 1G)
     │  Accessible only from management plane VLAN
     └─ ssh admin@<dpu-mgmt-ip>
```

Even if a tenant VM achieves host root privilege (container escape, hypervisor
bug), it cannot access DPU ARM Linux memory, modify OvS flow tables, or
disable network policy enforcement. The DPU ARM Linux OS is effectively a
hardware root of trust for network policy.

**DPU as a PCIe device from the host perspective:**

The host sees DPU as:
- One or more SR-IOV VFs presenting as VirtIO-net devices
- NVMe PCIe endpoint (for storage offload)
- Optionally: RDMA/RoCE device for high-performance compute workloads

The host kernel driver (e.g., `mlx5_core` for BlueField) communicates with the
DPU firmware via the PCIe command queue, but this channel is limited to
configuration — the host cannot execute arbitrary code on the DPU ARM cores.

**Out-of-band access:**

DPU management is designed for out-of-band (OOB) access:
- **Serial console**: RJ45 management port on the DPU card's bracket, connected
  to the server's baseboard management controller (BMC) or directly to a
  console server. Available even if DPU ARM Linux is hung.
- **Management Ethernet**: Dedicated 1G Ethernet port (physical or virtual via
  BMC). Carries only management traffic — SSH, HTTP(S) API, Prometheus scrape.
- **Redfish API**: Standard BMC-over-HTTP API for firmware updates and health.
- **JTAG / debug header**: For silicon bringup and deep debugging; not exposed
  in production chassis.

### Pillar 3 — Domain-Specific Accelerators

Fixed-function hardware blocks that perform specific operations at line rate
with deterministic latency and no ARM core involvement.

**Crypto Engine:**

Implements AES-256-GCM at full 400Gbps line rate. No ARM CPU cycles consumed.
- GHASH for GCM authentication tag computation (hardware polynomial multiply)
- Key management: keys stored in DPU secure key store, not readable by host
- Protocols supported in hardware:
  - IPsec (ESP in tunnel mode): inline encryption/decryption
  - MACsec (IEEE 802.1AE): L2 wire encryption
  - TLS 1.3 record layer (on selected products, e.g., Mellanox TLS offload)
- Key rotation: DPU ARM Linux daemon negotiates new keys (IKEv2/strongSwan),
  loads into hardware key table without disturbing in-flight traffic

**NVMe-oF Target Offload:**

The most architecturally interesting accelerator. Presents remote storage as if
it were a locally-attached NVMe SSD, without host CPU involvement:

1. Host OS driver issues NVMe read/write command to PCIe BARs
2. DPU NVMe target catches the command at PCIe endpoint (no host interrupt)
3. DPU fetches the data from storage fabric (RoCEv2 / iSER over the network)
4. DPU DMA's the result directly into the host memory region allocated by the
   command, using PCIe DMA

From the host perspective: NVMe SSD latency (~100–200 µs). Reality: data is
fetched from a storage server 1–10ms away, but the DPU hides this behind its
DRAM cache and prefetch logic.

Host CPU involvement per I/O: **zero**. No interrupt, no kernel entry, no copy.

**Data Services Accelerator:**

- **Inline compression**: LZ4 and Zstandard (Zstd) compression/decompression
  at line rate. Applied to storage blocks before sending over the network fabric.
  Compression ratio metadata stored in a per-block header.
- **Deduplication**: SHA-256 or xxHash fingerprint per 4KB block. Fingerprint
  table stored in DPU DRAM (typically 32GB supports ~8 billion 4KB fingerprints
  = ~32TB of deduplicated data). Hardware compares incoming block hashes against
  the table; on match, stores a reference pointer instead of the block.
- **Erasure coding**: Reed-Solomon or XOR parity computation in hardware.
  Typically used for distributed storage (e.g., 6+3 erasure coding across 9
  storage nodes).

**Regular Expression Accelerator:**

Implements a hardware DFA/NFA engine for pattern matching across packet
payloads:
- IDS/IPS signatures (Snort/Suricata rules compiled to hardware automata)
- DLP (Data Loss Prevention) patterns
- Stateful firewall: protocol conformance checking (e.g., HTTP state machine)

Without regex offload: pattern matching is the primary bottleneck for
stateful firewalls at >10Gbps. With DPU regex engine: full line-rate
inspection without host CPU involvement.

---

## Hardware Offload Flow Diagrams

### Normal Host Path (No DPU)

Packet processing without a DPU — all work on host CPU:

```
                         HOST SERVER
  ┌──────────────────────────────────────────────────────────────┐
  │                                                              │
  │  Wire (10/25/100G)                                           │
  │       │                                                      │
  │       ▼                                                      │
  │  ┌─────────┐   PCIe DMA    ┌──────────────────────────────┐ │
  │  │  NIC    │──────────────►│  Kernel RX Ring Buffer       │ │
  │  │(dumb)   │               │  (DMA coherent memory)       │ │
  │  └─────────┘               └──────────┬───────────────────┘ │
  │                                        │                     │
  │                              ┌─────────▼──────────┐         │
  │                              │   NIC Driver        │         │
  │                              │   (mlx5/i40e/bnxt)  │         │
  │                              │   hardirq → NAPI    │  ◄── BOTTLENECK:
  │                              └─────────┬───────────┘  softirq storms
  │                                        │              lock contention
  │                              ┌─────────▼──────────┐         │
  │                              │  netif_rx / GRO     │         │
  │                              │  skb allocation     │  ◄── BOTTLENECK:
  │                              └─────────┬───────────┘  memory alloc,
  │                                        │              cache misses
  │                              ┌─────────▼──────────┐         │
  │                              │  IP Stack           │         │
  │                              │  (routing, iptables)│  ◄── BOTTLENECK:
  │                              └─────────┬───────────┘  iptables O(n)
  │                                        │              rule traversal
  │                              ┌─────────▼──────────┐         │
  │                              │  TCP / UDP          │         │
  │                              │  (socket buffer)    │         │
  │                              └─────────┬───────────┘         │
  │                                        │                     │
  │                              ┌─────────▼──────────┐         │
  │                              │  Application        │         │
  │                              │  (VM / container)   │         │
  │                              └────────────────────┘         │
  │                                                              │
  │  Total latency: 50–100 µs                                    │
  │  CPU cores consumed: 4–8 for vSwitch/IPsec at 25Gbps        │
  └──────────────────────────────────────────────────────────────┘
```

### DPU Offload Path

With a DPU installed — infrastructure traffic never touches host CPU:

```
                    DPU CARD                      HOST SERVER
  ┌──────────────────────────────┐    ┌───────────────────────────┐
  │                              │    │                           │
  │  Wire (400G)                 │    │  ┌────────────────────┐   │
  │       │                      │    │  │  VM / Container    │   │
  │       ▼                      │    │  │  (tenant workload) │   │
  │  ┌──────────────────────┐    │    │  └────────┬───────────┘   │
  │  │  DPU ASIC            │    │    │           │               │
  │  │  Match-Action Parser │    │    │  ┌────────▼───────────┐   │
  │  │                      │    │    │  │  VirtIO-net driver │   │
  │  │  VXLAN/GENEVE decap  │    │    │  │  (virtio.ko)       │   │
  │  │  ECMP hash           │    │    │  └────────┬───────────┘   │
  │  │  ACL enforcement     │    │    │           │               │
  │  │  <1µs decision       │    │    │           │  PCIe VirtQ   │
  │  └──────┬───────────────┘    │    │           │  (DMA ring)   │
  │         │                    │    │           │               │
  │   ┌─────▼──────┐  ┌───────┐  │    │  ┌────────▼───────────┐   │
  │   │ Infra?     │  │Tenant?│  │    │  │  PCIe Endpoint     │   │
  │   │ (OvS ctrl, │  │(data) │  │    │  │  (VirtIO queues)   │◄──┼──DMA──┐
  │   │  BGP, etc) │  │       │  │    │  └────────────────────┘   │       │
  │   └─────┬──────┘  └───┬───┘  │    │                           │       │
  │         │             │      │    └───────────────────────────┘       │
  │  ┌──────▼──────┐      │      │                                        │
  │  │ DPU ARM     │      │      │                                        │
  │  │ Linux       │      └──────┼────────────────────────────────────────┘
  │  │ (OvS, etc.) │      PCIe   │   Tenant packets: line-rate, <1µs
  │  └─────────────┘      DMA    │   Infra packets: DPU only, 0 host CPU
  │                              │
  └──────────────────────────────┘

  KEY INSIGHT: Infrastructure traffic (OvS control, BGP updates, ARP, DHCP,
  telemetry, IPsec rekeying) is processed entirely within DPU ARM Linux.
  It never traverses the PCIe bus to the host.

  Tenant data packets: ASIC classifies → VirtIO DMA to host VM memory directly.
  Host CPU only runs the VM; no NIC driver interrupt, no softirq, no IP stack.
```

### NVMe-oF Offload Path

Storage I/O path with DPU NVMe-oF target offload:

```
  HOST                          DPU                    STORAGE FABRIC
  ┌────────────────┐           ┌──────────────────┐    ┌──────────────┐
  │                │           │                  │    │              │
  │  Application   │           │  NVMe-oF Target  │    │  Storage     │
  │  read(fd, ...)  │           │  Offload Engine  │    │  Server      │
  │       │        │           │                  │    │  (NVMe/TCP   │
  │  ┌────▼──────┐ │           │  ┌────────────┐  │    │   RoCEv2)    │
  │  │  Kernel   │ │  NVMe cmd │  │  Command   │  │    │              │
  │  │  NVMe     │─┼──────────►│  │  Queue     │  │    └──────────────┘
  │  │  driver   │ │  (PCIe    │  │  Parser    │  │           │
  │  │           │ │  BAR mmio)│  └─────┬──────┘  │    ┌──────▼──────┐
  │  └────────────┘ │           │        │         │    │ RoCEv2 RDMA │
  │                 │           │  ┌─────▼──────┐  │    │ (line-rate  │
  │  Host CPU:      │           │  │  Cache     │  │    │  fetch)     │
  │  Issued cmd,    │           │  │  Lookup    │  │    └──────┬──────┘
  │  now idle.      │           │  │  (DPU DRAM)│  │           │
  │  Zero CPU in    │           │  └─────┬──────┘  │    ┌──────▼──────┐
  │  I/O path.      │           │        │ miss     │    │  Block data │
  │                 │           │  ┌─────▼──────┐  │    │  DMA'd back │
  │  ┌────────────┐ │           │  │  RoCEv2    │◄─┼────┤  to DPU     │
  │  │ Host DRAM  │ │  PCIe DMA │  │  Fetch     │  │    └─────────────┘
  │  │ (user buf) │◄┼───────────┤  │  Engine    │  │
  │  └────────────┘ │           │  └────────────┘  │
  │        │        │           │                  │
  │  Completion     │           │  DMA result ─────┼──► Host DRAM (via PCIe)
  │  interrupt      │           │  Post completion ─┼──► Host NVMe CQ
  │        │        │           │                  │
  │  ┌─────▼──────┐ │           └──────────────────┘
  │  │ read()     │ │
  │  │ returns    │ │
  │  └────────────┘ │
  └─────────────────┘

  Host CPU path: issue command → wait → receive completion interrupt → return.
  Everything in between runs on DPU or storage fabric. No memcpy, no I/O thread.
```

---

## Fault Resilience on DPUs

DPUs are themselves fault-resilient compute units, and their design provides
resilience properties unavailable in traditional host-NIC architectures.

### DPU Independence from Host

The DPU ARM Linux OS runs independently of the host OS:

- **Host kernel panic**: DPU continues enforcing network ACLs, forwarding
  tenant traffic, and reporting telemetry to monitoring. The host can be
  rebooted without any DPU state disruption.
- **Host firmware update**: DPU remains live. Tenants see no network disruption
  even if host BIOS/UEFI is updated and host reboots.
- **DPU update**: Host VMs continue receiving network connectivity during DPU
  firmware update — the ASIC data path is not interrupted by ARM Linux reboot.
  Only the DPU control plane is briefly offline; established flows continue.

### DPU Hardware Watchdog

DPU ARM Linux registers a hardware watchdog timer:
- Watchdog timeout: typically 30–60 seconds
- ovs-vswitchd and control plane daemons pet the watchdog via systemd's
  `sd_notify(WATCHDOG=1)` mechanism
- On watchdog expiry: DPU performs a hard reset and boots from its own
  redundant eMMC/NOR flash (A/B boot with U-Boot or UEFI)
- The ASIC data path is preserved during DPU ARM Linux reset via a
  "shadow tables" mechanism — flow tables persist in ASIC SRAM

### EDAC on DPU ARM DRAM

DPU DRAM (LPDDR5, 16–32GB) has hardware ECC:
- Single-bit errors (correctable, CE) detected and corrected transparently
- Double-bit errors (uncorrectable, UE) trigger a DPU reset

The Linux EDAC subsystem (see `edac-reference/`) applies identically to the
DPU ARM Linux kernel. The same `edac_cortex_ref.ko` module works on DPU ARM
cores (same Cortex-A72/A78 silicon). The same `/sys/devices/system/edac/mc*/`
sysfs tree is populated by the DPU kernel.

DPU-specific consideration: DPU DRAM also functions as the packet buffer pool
for the ASIC. High CE rates on DPU DRAM are operationally significant even if
no ARM-side software is affected — corrupted packet buffer memory can cause
subtle packet errors that ECC corrects but that indicate imminent DRAM failure.

**Alert rule**: treat DPU EDAC CE rate > 10/hour as a DPU replacement trigger,
not just a monitoring annotation (see `fault-resilience/dpu-edac-integration.md`).

### DPU to Host Failure Signaling

The DPU can inject PCIe error notifications to inform the host OS of
infrastructure events:
- **PCIe AER (Advanced Error Reporting)**: DPU firmware can assert a
  correctable or uncorrectable PCIe error signal to the host, which triggers
  Linux's `aer_irq` handler and associated Prometheus metrics (see
  `exporter/collectors/aer.go`).
- **Custom MSI interrupt**: Vendor-specific "DPU health event" interrupt to
  host driver; driver writes to a shared memory region the DPU has configured.
- **VirtIO control queue**: DPU can send administrative messages to the host
  VirtIO-net driver, including link-down notifications or error status.

Note: DPU PCIe AER is managed by DPU firmware, not the host's AER handler.
When running `hw-fault-exporter` on the DPU, use `--disable-aer` to avoid
double-counting with host-side AER metrics.

### Link Failure Detection

DPU physical layer (PHY / SerDes) detects link loss in <1 ms:
- **Threshold**: 802.3 specifies link failure detection within 350 ms; DPU
  PHY interrupt latency is typically <1 ms to DPU ARM Linux
- **Host notification**: DPU notifies host VirtIO-net driver of link state
  change via the VirtIO virtqueue control path (<5 ms)
- **BGP withdraw**: OvS/FRR running on DPU ARM initiates BGP withdraw
  immediately on physical link down, before the host kernel even processes the
  VirtIO link-down event
- **ARP/ND eviction**: DPU flushes ARP/ND entries for unreachable neighbors
  before host network stack is involved

This means DPU-managed hosts converge routing failover faster than hosts with
traditional NICs, since the DPU control plane acts on physical events directly.

---

## Connection to This Repository

This module is designed to integrate with all other modules in the repository:

### edac-reference/

The EDAC driver (`edac_cortex_ref.ko`) compiles and loads on DPU ARM Linux
exactly as on any Cortex-A72/A78 host. The DPU Linux kernel (typically 5.15 LTS
or 6.1 LTS) includes the same EDAC core. No modifications are required.

Procedure: copy the module to DPU via `scp`, load with `insmod` or `modprobe`
from the DPU management shell.

### exporter/

The `hw-fault-exporter` Go binary compiles for ARM64 (`GOARCH=arm64
GOOS=linux`). Deploy to DPU management plane to export:
- EDAC CE/UE counts from DPU DRAM
- DPU PCIe AER counters (with `--disable-aer` since DPU manages its own AER)
- DPU ARM core MCE events (if applicable)

The exporter runs on port `:9200` on the DPU management IP to avoid conflict
with the host exporter on `:9100`. See `control-plane/dpu-fault-exporter.service`.

### fault-injection/

Fault injection scripts work on DPU ARM Linux without modification:
- `inject_edac_ce.sh`: inject synthetic CE into DPU EDAC counters for alert
  testing
- `ci/qemu-boot.sh`: start a QEMU ARM64 VM as a DPU simulation environment
  (1 CPU core, 1GB RAM, virtio-net, no DPU-specific hardware needed)

### kernel-hardening/ebpf/

bpftrace scripts run on DPU ARM Linux (kernel 5.15+, BPF enabled):
- `detect-fork-storm.bt`: monitor OvS daemon restarts (unexpected fork loops)
- `detect-io-anomaly.bt`: monitor NVMe-oF target I/O anomalies on DPU
- `detect-oom-pressure.bt`: alert on DPU packet buffer DRAM pressure

### dashboards/

Add DPU targets to the Prometheus scrape configuration. See
`fault-resilience/dpu-edac-integration.md` for the complete scrape config and
alert rules for DPU nodes.

---

## Directory Structure

```
smartnic/
├── README.md                          # This file
├── control-plane/
│   ├── README.md                      # DPU control plane setup guide
│   ├── dpu-fault-exporter.service     # Systemd unit for exporter on DPU
│   └── dpu-setup.sh                   # Setup script for DPU management plane
├── offload/
│   ├── ovs-offload-config.sh          # OvS TC flower hardware offload config
│   └── p4-simple-forward.p4           # P4_16 reference data-path program
├── fault-resilience/
│   └── dpu-edac-integration.md        # Applying EDAC to DPU DRAM
└── tests/
    └── test_smartnic.py               # pytest tests for all module artifacts
```
