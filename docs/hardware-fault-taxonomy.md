# Hardware Fault Taxonomy for ARM Linux Datacenter Systems

This document classifies the hardware fault types relevant to ARM Linux servers, describes
their kernel-visible manifestations, and explains which subsystem handles each type.

---

## 1. Fault Classification Axes

Every hardware fault can be characterized along three independent axes:

### Axis 1: Correctability

| Class | Definition | Kernel response |
|-------|-----------|-----------------|
| **Correctable (CE)** | Hardware detected and corrected the error. No data loss. | Increment counter, log, optionally page-offline |
| **Uncorrectable, non-fatal (UCE-NF)** | Error detected; affected data marked invalid. System continues. | Kill affected process or offline page; log |
| **Uncorrectable, fatal (UCE-F)** | Error in critical kernel data. No safe continuation path. | Controlled panic or machine check abort |

### Axis 2: Location

| Location | Detection mechanism | Kernel subsystem |
|----------|--------------------|--------------------|
| L1 instruction cache | Parity or ECC (implementation-defined) | EDAC or platform-specific |
| L1 data cache | Parity (typically no ECC at L1) | EDAC |
| L2 unified cache | ECC (most ARM Cortex-A implementations) | EDAC |
| L3 / LLC (if present) | ECC | EDAC |
| DRAM | ECC via memory controller | EDAC mc driver |
| PCIe link | CRC, AER correctable/uncorrectable | PCI AER driver |
| PCIe endpoint device | Device-specific, reported via AER | PCI AER driver |
| CPU internal (RAS) | ARM RAS Extension error records | GHES / APEI |

### Axis 3: Detection Timing

| Timing | Mechanism | Latency to software visibility |
|--------|-----------|-------------------------------|
| **Synchronous** | Error occurs during a specific memory access instruction | Immediate — synchronous abort or SError |
| **Asynchronous** | Error detected by background scrubber or ECC poll | Up to poll interval (typically seconds) |
| **Firmware-first** | Firmware intercepts error, writes to CPER record, notifies OS | Platform-dependent (ms to seconds) |

---

## 2. Memory Error Taxonomy (EDAC)

### 2.1 DRAM Bit Errors

DRAM bit errors arise from several physical mechanisms:

**Single Event Upsets (SEU):** Ionizing radiation (cosmic rays, alpha particles from packaging
materials) causes a charge change in a DRAM cell. At sea level, a 1-Gbit DRAM module
experiences roughly 1 SEU per 1000 hours under normal conditions. A 256-DIMM server rack
sees several SEUs per hour — all correctable by ECC, but only if the error is detected.

**Row Hammer:** Repeated accesses to a DRAM row cause charge leakage in adjacent rows. Modern
server DIMMs include Target Row Refresh (TRR) mitigations; the kernel additionally supports
software-side mitigation through access pattern randomization. Row hammer faults that bypass
TRR produce CE events initially, progressing to UE as the weakened cell degrades.

**Retention failures:** DRAM cells that cannot hold charge for the standard refresh interval.
Typically manifest as temperature-dependent CEs (more frequent at elevated temperatures) on
a specific bit position.

**Multi-bit errors (MBE):** ECC DRAM corrects single-bit errors per ECC symbol but cannot
correct double-bit errors (SECDED — Single Error Correct, Double Error Detect). An MBE
produces an uncorrectable error. Chipkill-capable memory controllers extend this to
full-DRAM-chip failure correction.

### 2.2 Cache Errors

ARM Cortex-A72 implements ECC on L2 cache RAM. The ECC covers both data and tag arrays.

**Tag ECC errors:** An error in the cache tag array is particularly dangerous because the
tag determines which physical address a cache line corresponds to. A corrupt tag causes the
wrong data to be returned for a cache hit. ARM L2C-310 and similar implementations detect
tag parity/ECC errors and signal them to the CPU via an implementation-defined error signal.

**Data ECC errors:** An error in the data array is corrected (CE) or detected as uncorrectable
(UE) by the standard ECC logic. Correctable data errors are fixed on the fly; the corrected
data is written back.

**SRAM wear:** Unlike DRAM, SRAM does not have the refresh-based failure mode, but physical
aging (hot carrier injection, time-dependent dielectric breakdown) can produce increasing
error rates in aging hardware.

### 2.3 Memory Controller Errors

The memory controller itself can be a source of errors:

- **Command/address parity errors:** The address sent to the DRAM is corrupted on the bus.
  Detected by DRAM-side parity checking; results in a CE or UE depending on severity.
- **Write data bus errors:** Data corrupted between memory controller and DRAM on a write.
  Detected by write CRC or ECC verification on readback.
- **Internal FIFO / buffer errors:** Errors in the memory controller's internal SRAM buffers.
  Reported through the controller's error status registers, surfaced via EDAC.

---

## 3. PCIe Error Taxonomy (AER)

### 3.1 Correctable AER Errors

These errors are detected and recovered from autonomously by the PCIe link hardware. They do
not cause data loss but indicate link quality issues:

| Error | Description | Typical cause |
|-------|-------------|---------------|
| `ReceiverError` | Physical layer receiver detected an error | Bad cable, SI issues |
| `BadTLP` | Transaction Layer Packet failed LCRC check | EMI, bad connector |
| `BadDLLP` | Data Link Layer Packet failed CRC | Link noise |
| `ReplayNumRollover` | Too many replay attempts before ACK | Severe link degradation |
| `ReplayTimerTimeout` | ACK not received within timeout | Link partner not responding |
| `AdvisoryNonFatalError` | Receiver overflow (buffer overflow corrected) | High traffic burst |
| `HeaderLogOverflow` | Error log header FIFO overflowed | Error storm |

A rising `ReceiverError` or `BadTLP` rate on a specific PCIe slot is actionable:
it indicates a failing connector, cable, or riser card before the link goes down entirely.

### 3.2 Non-Fatal Uncorrectable AER Errors

Data loss has occurred on this transaction, but the device and link can continue:

| Error | Description |
|-------|-------------|
| `PoisonedTLP` | A TLP was marked as poisoned (data known bad) |
| `FlowControlProtocol` | PCIe flow control protocol violated |
| `CompletionTimeout` | Outstanding request timed out with no completion |
| `CompleterAbort` | Request aborted by the completer |
| `UnexpectedCompletion` | Completion received for a request not outstanding |
| `ReceiverOverflow` | Receiver buffer overflowed |
| `MalformedTLP` | TLP format violation |
| `ECRCError` | End-to-end CRC check failed |
| `UnsupportedRequest` | Request type not supported by this device |

`CompletionTimeout` is the most operationally significant: it occurs when a DMA read from
a device times out, which on an AI accelerator typically means the accelerator has locked up
internally. The AER handler sequence (freeze → reset → resume) is the path to recovery.

### 3.3 Fatal Uncorrectable AER Errors

Link or device state is unrecoverable without a hardware reset:

| Error | Description |
|-------|-------------|
| `DataLinkProtocol` | Data link layer state machine violation |
| `SurpriseDown` | Device disappeared from the bus unexpectedly |
| `FlowControlProtocol` (when fatal) | Unrecoverable flow control violation |

`SurpriseDown` on an AI accelerator card is the AER equivalent of a server losing a
PCIe device mid-job — the job fails, the slot requires physical intervention or a deep reset.

---

## 4. CPU / RAS Errors (ARM RAS Extension)

ARM servers implementing the RAS Extension (ARMv8.2-RAS) expose error records through:

- **ERRIDR_EL1 / ERR<n>SR_EL1:** Error record registers accessible in EL1/EL2.
- **ACPI HEST (Hardware Error Source Table):** Firmware advertises error sources; the kernel's
  GHES driver registers handlers.
- **CPER (Common Platform Error Record):** The standard structure for error records exchanged
  between firmware and OS.

### ARM RAS Error Record Structure

Each error record contains:

- `ERR<n>STATUS`: Error status (valid, overflow, UE/CE, error type)
- `ERR<n>ADDR`: Physical address of the error (if applicable)
- `ERR<n>MISC0`: Implementation-defined additional information (e.g., syndrome)
- `ERR<n>CTLR`: Control register (enable, interrupt vs. poll)

The kernel's `drivers/acpi/apei/ghes.c` reads CPER records from a firmware-maintained buffer
(BERT — Boot Error Record Table for persistent errors, or GHES notification for runtime errors)
and calls registered error handlers, including the EDAC GHES shim.

---

## 5. Error Rate Interpretation

Raw error counts are less useful than rates and trends:

| Observation | Likely interpretation |
|-------------|----------------------|
| Isolated CE, no recurrence | Transient (cosmic ray); monitor |
| CE rate > 1/hour on same DIMM rank | DIMM degrading; schedule replacement |
| CE rate rising monotonically on same bit position | Retention failure; replace immediately |
| CE storm followed by UE on same address | DIMM failure; offline the node |
| AER correctable rate > 10/minute on one slot | Connector / cable issue; inspect |
| AER CompletionTimeout on AI accelerator | Accelerator lockup; device reset |
| MCE on CPU core (RAS UE) | CPU microarchitectural error; offline core |

These thresholds are illustrative. Production threshold tuning requires baseline measurement
across a fleet to establish the normal CE rate distribution for the specific DRAM vendor and
DIMM geometry in use.

---

## 6. Error Propagation and Cascade Risk

The most dangerous failure mode is not the fault itself but silent propagation:

1. An uncorrectable DRAM error in a memory-mapped I/O region causes a device to DMA corrupted
   data into a peer device's buffer.
2. The peer device processes the corrupted data, producing a corrupted result.
3. The corrupted result is written to persistent storage.
4. The error is now in the storage layer and survives a reboot.

This cascade is prevented by:

- **Poison propagation:** ARM and PCIe both support a "poison" bit on data that is known
  corrupt. Poison propagates with the data but does not silently corrupt downstream consumers —
  instead, the consumer that attempts to use poisoned data receives a fault.
- **Kernel memory failure handling (`mm/memory-failure.c`):** The kernel's memory failure
  handler can offline a poisoned page before it is consumed.
- **EDAC early warning:** A rising CE rate on a DIMM that is also serving as a device DMA
  target is caught before it becomes a UE.

The observability pipeline in this repository makes all of these events visible as Prometheus
metrics so that the cascade can be interrupted at the first link in the chain.
