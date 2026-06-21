# Architecture

This document describes the engineering design of the arm-linux-fault-resilience reference
implementation: why each layer exists, how the layers compose, and what failure modes each
layer is designed to catch.

---

## 1. Problem Statement

Hardware faults in a datacenter fleet fall into two categories with very different properties:

**Correctable errors (CE)** are detected and silently fixed by hardware — ECC DRAM corrects
single-bit flips, ARM L1/L2 caches can correct certain SRAM faults. No data is lost. But a
rising CE rate on a specific DIMM or cache line is the strongest leading indicator that an
uncorrectable fault is approaching. Without a reporting pipeline, the CE rate is invisible.

**Uncorrectable errors (UE)** cannot be fixed by hardware. The kernel must choose between
killing the affected process, offlining the affected memory page, or — if no recovery path
is registered — panicking. On a workload node running a long-running distributed job, a kernel
panic mid-job is a hard reset with no checkpointing: the job fails, the slot is wasted, and
the cascade begins as upstream components wait for results that will never arrive.

The core engineering problem is not that hardware faults exist — they always will — but that:

1. CE events are **invisible** unless explicitly plumbed from hardware through the kernel and
   into an observability pipeline.
2. UE recovery paths are **untested** in most CI systems because the events are too rare to
   occur naturally in test runs.
3. Postmortem analysis of past faults requires **replay capability** because the kernel state
   that exposed the bug is gone by the time the incident is investigated.

This repository addresses all three.

---

## 2. Detection Layer

### 2.1 EDAC (Error Detection And Correction)

The Linux EDAC subsystem (`drivers/edac/`) is the canonical kernel interface for memory
controller error reporting. It provides:

- A standardized sysfs tree under `/sys/devices/system/edac/mc<N>/` with per-controller,
  per-csrow (chip-select row), and per-channel counters for both CE and UE events.
- A kernel-level notification path via `edac_mc_handle_error()` that drivers call when
  hardware signals an error.
- An optional polling loop for controllers that report errors in status registers rather than
  via interrupt.

The reference driver in `edac-reference/edac_cortex_ref.c` implements the poll-based model:
it registers a timer that reads a simulated status register and calls into the EDAC core,
which increments the appropriate sysfs counters and logs to the kernel ring buffer.

```
Hardware ECC logic
      │
      │  (status register or interrupt)
      ▼
edac_cortex_ref.c (this repo)
      │
      │  edac_mc_handle_error()
      ▼
EDAC core (drivers/edac/edac_mc.c)
      │
      ├──► /sys/devices/system/edac/mc0/ce_count
      ├──► /sys/devices/system/edac/mc0/ue_count
      ├──► /sys/devices/system/edac/mc0/csrow0/ch0_ce_count
      └──► kernel ring buffer (dmesg)
```

### 2.2 Machine Check Architecture (MCA / MCE)

ARM servers with RAS extensions expose machine-check style error reporting through ACPI APEI
(Advanced Platform Error Interface). The kernel's GHES (Generic Hardware Error Source) driver
consumes HEST (Hardware Error Source Table) entries and surfaces them through:

- `/sys/firmware/acpi/` entries for firmware-first errors
- The kernel's MCE log (`/dev/mcelog` on x86; GHES notifications on ARM64)
- `tracepoint: ras:mc_event` for in-kernel consumers

The `exporter/collectors/mce.go` collector reads the GHES-exposed counters and translates them
into Prometheus metrics.

### 2.3 PCIe Advanced Error Reporting (AER)

PCIe AER is defined in the PCIe Base Specification §6.2. Each PCIe device (endpoint or bridge)
with an AER capability register set exposes:

- A correctable error status register (lane errors, flow-control errors, etc.)
- An uncorrectable error status register (malformed TLPs, unexpected completions, ECRC errors)
- A root port error command register for interrupt vs. polling control

The kernel's `drivers/pci/pcie/aer.c` handles AER events and logs to dmesg. The sysfs
interface exposes cumulative counters per device. The `exporter/collectors/aer.go` collector
walks `/sys/bus/pci/devices/*/aer_*` and emits per-device, per-error-type metrics.

---

## 3. Recovery Paths

Recovery path correctness is what distinguishes a resilient system from one that merely detects
faults. The kernel provides three standard recovery mechanisms; the reference driver exercises
all three.

### 3.1 Correctable Error Recovery

No data loss has occurred. The correct response is:

1. Increment the CE counter for the affected location (controller / csrow / channel).
2. Log to the kernel ring buffer at KERN_WARNING.
3. If the CE rate for a location exceeds a configurable threshold, page-offline the affected
   memory region via `memory_failure()` to prevent future access.

The threshold-based page-offline is the key resilience step: it converts a predictive CE storm
into a proactive evacuation before the UE occurs.

### 3.2 Uncorrectable Error Recovery

Data loss has occurred or cannot be ruled out. The kernel response depends on context:

- **User-space process context:** `memory_failure()` sends SIGBUS to the affected process.
  The process can register a SIGBUS handler to checkpoint and exit cleanly.
- **Kernel context:** If the fault affects kernel memory, the options are narrow.
  `fixup_exception()` can recover from faults in copy-to/from-user paths.
  For faults in critical kernel data structures, a controlled panic is preferable to
  silent corruption.
- **Idle page:** `memory_failure()` can offline the page without signal if no process
  holds a reference to it.

### 3.3 PCIe Link-Level Recovery

For correctable AER errors (receiver errors, bad TLP, etc.), the PCIe link retrains
automatically. The kernel AER driver logs the event and resets the device error counters.

For uncorrectable AER errors, the kernel invokes the AER recovery sequence:

1. `pci_channel_io_frozen` — notifies the device driver that the link is down.
2. Driver's `.error_detected()` callback — driver resets internal state.
3. `pci_channel_io_normal` — link is back; driver's `.resume()` callback.

If the driver does not implement the AER callbacks, the device is taken offline permanently.

---

## 4. Validation: Fault Injection

A recovery path that has never been exercised is equivalent to not having one. Hardware faults
are too rare in normal operation to provide meaningful CI coverage. The kernel's fault injection
framework (`lib/fault-inject.c`, `Documentation/fault-injection/`) solves this by providing
a debugfs interface to force error paths to execute.

### 4.1 Kernel Fault Injection Interface

The relevant debugfs knobs (under `/sys/kernel/debug/fail_function/` or per-driver paths):

```
/sys/kernel/debug/edac/mc0/fake_inject           # EDAC CE/UE injection
/sys/kernel/debug/einj/error_inject              # ACPI EINJ hardware error injection
/sys/kernel/debug/aer-inject                     # PCIe AER injection (aer-inject module)
```

Each injection script in `fault-injection/` sets the appropriate debugfs knobs, triggers the
injection, then reads back the sysfs counters to verify the recovery path ran and the counter
incremented correctly.

### 4.2 CI Matrix Design

`fault-injection/ci_fault_matrix.sh` iterates the Cartesian product of:

- Fault type: `{CE, UE, AER_correctable, AER_uncorrectable}`
- Controller/device: `{mc0, mc1, ...}` (enumerated dynamically from sysfs)
- Recovery assertion: counter increment, dmesg log, page-offline (for UE)

This ensures every combination is exercised in CI, not just the common path.

---

## 5. Observability Pipeline

```
Kernel sysfs / procfs / debugfs
          │
          │  (scrape every 15s)
          ▼
hw-fault-exporter (Go, this repo)
          │
          │  HTTP /metrics — Prometheus text format
          ▼
Prometheus (time-series storage)
          │
          ├──► Grafana (dashboards, this repo)
          └──► Alertmanager (PagerDuty / Slack)
```

### 5.1 Exporter Architecture

The exporter follows the Prometheus multi-collector pattern. Each subsystem has an independent
`Collector` implementation that:

1. Opens and reads the relevant sysfs paths.
2. Emits `CounterVec` metrics (cumulative, monotonically increasing — correct for fault counters
   that the kernel never resets without a reboot).
3. Reports its own scrape duration and error count so the exporter's health is itself observable.

**Metric naming convention:**

| Metric | Type | Labels | Source |
|--------|------|--------|--------|
| `edac_correctable_errors_total` | Counter | `controller`, `csrow`, `channel` | `/sys/devices/system/edac/mc<N>/csrow<M>/ch<K>_ce_count` |
| `edac_uncorrectable_errors_total` | Counter | `controller`, `csrow`, `channel` | `/sys/devices/system/edac/mc<N>/csrow<M>/ue_count` |
| `mce_events_total` | Counter | `severity`, `bank` | GHES / mcelog |
| `pcie_aer_correctable_total` | Counter | `device`, `error_type` | `/sys/bus/pci/devices/*/aer_dev_correctable` |
| `pcie_aer_uncorrectable_total` | Counter | `device`, `error_type` | `/sys/bus/pci/devices/*/aer_dev_nonfatal` |
| `hw_fault_exporter_scrape_duration_seconds` | Gauge | `collector` | Internal |
| `hw_fault_exporter_scrape_errors_total` | Counter | `collector` | Internal |

### 5.2 Cardinality Considerations

At fleet scale, metric cardinality is a Prometheus scaling concern. The label set is designed
to keep cardinality bounded:

- `controller` is bounded by the number of memory controllers per server (typically 2–8).
- `csrow` and `channel` are bounded by DIMM topology (typically ≤ 16 csrows, ≤ 4 channels).
- `device` for PCIe uses the BDF (bus:device.function) string, bounded by PCIe topology.
- `error_type` for AER uses the fixed set of AER error names from the PCIe spec.

No per-address or per-page labels are used — those would create unbounded cardinality.

---

## 6. Postmortem Replay

When an unusual fault pattern occurs in production (or in CI), the ability to reproduce it
deterministically is as important as fixing it. `replay/parse_edac_trace.py` parses a kernel
ring buffer trace that contains EDAC log lines into a structured event sequence:

```json
[
  {
    "timestamp_ns": 1700000000000000000,
    "type": "CE",
    "controller": "mc0",
    "csrow": 0,
    "channel": 0,
    "grain": 8,
    "syndrome": "0x0000000000000001",
    "msg": "EDAC MC0: 1 CE on unknown label (mc:0 page:0x1234 offset:0x0 grain:8 syndrome:0x1)"
  },
  ...
]
```

`replay_kernel_state.sh` takes the parsed JSON and re-injects the event sequence through the
fault-injection layer at the original inter-event timing, allowing an engineer to observe the
kernel's response to the same sequence of faults that occurred in the original incident.

**Why this matters:** many EDAC-related bugs are timing-dependent — a CE storm that precedes
a UE by 200ms may exercise a race in the page-offline path that a single CE followed by a UE
30 seconds later does not. Replay at original timing is the only reliable way to reproduce
timing-sensitive fault scenarios.

---

## 7. Relationship to Upstream Kernel Work

The engineering methodology in this repository is a direct extension of the diagnostic approach
used in two prior upstream contributions:

**Marvell ARMADA pinctrl drivers:** Writing a pinctrl driver requires reading hardware
register documentation, mapping hardware behavior to the kernel's abstraction layer
(`pinctrl_ops`, `pinmux_ops`), and verifying that the driver correctly reflects hardware
state. The same read-hardware-state → map-to-kernel-interface → verify pattern appears in the
EDAC reference driver here.

**Softirq regression diagnosis (commit `3c53776e`):** A softirq accounting bug caused
interrupt latency to degrade silently under load. Diagnosing it required: reading kernel
accounting code in `kernel/softirq.c`, correlating `/proc/softirqs` counters with observed
scheduling behavior, bisecting kernel versions to find the causative commit, and writing a
reproducible test case. This is structurally identical to the CE-rate correlation workflow
this repository formalizes: read kernel counters, correlate with system behavior, identify
the causative event sequence, write a reproducible injection test.

The softirq fix restored a correctness guarantee (bounded softirq processing latency) that
kernel subsystems depend on for liveness. The EDAC/AER/MCE pipeline this repository documents
restores the analogous guarantee for hardware fault handling: that faults are detected,
counted, and recovered from in bounded time, and that the recovery path is verified before
it is needed.
