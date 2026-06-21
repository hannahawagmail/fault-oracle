# PCIe Advanced Error Reporting (AER) — Deep Dive

This document covers PCIe AER from specification to Linux kernel handling to operational
monitoring, aimed at engineers who work with ARM servers and need to understand and act on
AER events in production.

---

## 1. What AER Is and Why It Exists

Every PCIe transaction (memory read, memory write, completion) traverses multiple logical
layers: Physical (PHY), Data Link, and Transaction. Each layer has its own error detection
mechanisms. Without AER, error detection is vendor-specific and inconsistent — a PCIe device
might signal an error via an interrupt with a vendor-specific status register, or not at all.

PCIe AER (defined in the PCIe Base Specification §6.2, available at pcisig.com) standardizes:

1. **Which errors are detectable** — a fixed enumeration of error conditions per layer.
2. **How errors are recorded** — a standard set of capability registers at a fixed PCIe
   Extended Capability offset.
3. **How errors are signaled** — via Message Signaled Interrupts (MSI/MSI-X) to the root port,
   which then signals the OS.
4. **The recovery sequence** — a defined protocol for the OS, root port, and device driver
   to coordinate recovery.

---

## 2. AER Register Architecture

Each PCIe function that implements AER has an **AER Extended Capability** block in its
configuration space (offset varies; located by the Extended Capability linked list, capability
ID 0x0001).

### 2.1 Correctable Error Status / Mask Registers (CES/CEM)

```
Offset 0x04: Correctable Error Status Register (CES)
Offset 0x08: Correctable Error Mask Register (CEM)

Bit  0: Receiver Error Status
Bit  6: Bad TLP Status
Bit  7: Bad DLLP Status
Bit  8: Replay Number Rollover Status
Bit 12: Replay Timer Timeout Status
Bit 13: Advisory Non-Fatal Error Status
Bit 14: Corrected Internal Error Status
Bit 15: Header Log Overflow Status
```

Each bit is set when the corresponding error is detected. The mask register controls which
errors generate an interrupt (0 = enable, 1 = mask — the PCIe convention is inverted from
what most engineers expect).

### 2.2 Uncorrectable Error Status / Mask / Severity Registers (UES/UEM/UEV)

```
Offset 0x08: Uncorrectable Error Status Register (UES)
Offset 0x0C: Uncorrectable Error Mask Register (UEM)
Offset 0x10: Uncorrectable Error Severity Register (UEV)

Bit  4: Data Link Protocol Error
Bit  5: Surprise Down Error (hotplug-capable slots only)
Bit 12: Poisoned TLP Received
Bit 13: Flow Control Protocol Error
Bit 14: Completion Timeout
Bit 15: Completer Abort
Bit 16: Unexpected Completion
Bit 17: Receiver Overflow
Bit 18: Malformed TLP
Bit 19: ECRC Error
Bit 20: Unsupported Request Error
Bit 21: ACS Violation
Bit 22: Uncorrectable Internal Error
Bit 23: MC Blocked TLP
Bit 24: AtomicOp Egress Blocked
Bit 25: TLP Prefix Blocked Error
Bit 26: Poisoned TLP Egress Blocked
```

The Severity register controls whether each uncorrectable error is treated as non-fatal (0)
or fatal (1). By default, `DataLinkProtocolError` and `SurpriseDown` are fatal; the rest
are non-fatal.

### 2.3 Root Error Command / Status Registers (REC/RES)

These exist only on root ports and root complex event collectors:

```
Root Error Command Register:
  Bit 0: Correctable Error Reporting Enable
  Bit 1: Non-Fatal Error Reporting Enable
  Bit 2: Fatal Error Reporting Enable

Root Error Status Register:
  Bit 0: ERR_COR Received
  Bit 1: Multiple ERR_COR Received
  Bit 2: ERR_FATAL/NONFATAL Received
  Bit 3: Multiple ERR_FATAL/NONFATAL Received
  Bit 4: First Uncorrectable Fatal
  Bit 5: Non-Fatal Error Messages Received
  Bit 6: Fatal Error Messages Received
  Bits 27-31: Advanced Error Interrupt Message Number (MSI vector)
```

### 2.4 Header Log Register

When an AER uncorrectable error occurs, the first 16 bytes of the offending TLP are captured
in the Header Log Register. This is invaluable for debugging because it shows exactly which
transaction triggered the error.

---

## 3. AER Error Signaling Flow

```
PCIe Device detects error
        │
        │  Sets AER Uncorrectable/Correctable Error Status bit
        │
        ▼
Device sends ERR_COR / ERR_NONFATAL / ERR_FATAL message upstream
        │
        ▼
Root Port receives error message
        │
        │  Sets Root Error Status bits
        │  Fires MSI interrupt (vector from Interrupt Message Number)
        │
        ▼
OS interrupt handler (drivers/pci/pcie/aer.c: aer_irq())
        │
        │  Reads Root Error Status
        │  Reads device AER status registers
        │  Clears error status bits
        │
        ▼
AER event dispatched to device driver via pci_bus_sem callbacks
        │
        ├──► pci_error_handlers.error_detected()  ← driver callback
        ├──► pci_error_handlers.slot_reset()       ← if device reset needed
        └──► pci_error_handlers.resume()           ← after successful recovery
```

---

## 4. Linux Kernel AER Implementation

### 4.1 Core Driver: `drivers/pci/pcie/aer.c`

The AER kernel driver (`CONFIG_PCIEAER=y`) implements:

- **`aer_irq()`**: Top-half interrupt handler. Reads the Root Error Status register and
  schedules the bottom half.
- **`aer_isr()`**: Workqueue bottom half. Reads the full error status, logs to dmesg, and
  invokes the PCI error recovery sequence.
- **`pci_aer_clear_status()`**: Clears hardware error status bits after handling.

### 4.2 Error Recovery Sequence

For non-fatal uncorrectable errors:

```c
/* Step 1: Notify driver, get its assessment */
status = report_error_detected(dev, pci_channel_io_normal, &result);

/* Step 2: If driver requests reset, issue function-level reset */
if (result == PCI_ERS_RESULT_NEED_RESET) {
    pci_reset_function(dev);
    /* Step 3: Notify driver of slot reset */
    report_slot_reset(dev);
}

/* Step 4: Resume normal operation */
report_resume(dev);
```

For fatal errors:

```c
/* Step 1: Freeze the channel */
report_error_detected(dev, pci_channel_io_frozen, &result);

/* Step 2: Reset the slot (link reset, not just function reset) */
pci_reset_bus(dev);

/* Step 3: Notify of slot reset and resume */
report_slot_reset(dev);
report_resume(dev);
```

If a device driver does not implement `pci_error_handlers`, the device is taken offline
permanently after a fatal error.

### 4.3 sysfs AER Interface

The kernel exposes per-device AER error counters via sysfs:

```
/sys/bus/pci/devices/<BDF>/
├── aer_dev_correctable          # one line per correctable error type: "name count"
├── aer_dev_nonfatal             # one line per non-fatal uncorrectable error type
└── aer_dev_fatal                # one line per fatal error type
```

Example `aer_dev_correctable` content:

```
RxErr 0
BadTLP 3
BadDLLP 0
Rollover 0
Timeout 0
NonFatalErr 0
CorrIntErr 0
HeaderOF 0
TOTAL_ERR_COR 3
```

The Prometheus exporter in this repo scrapes these files and emits per-device,
per-error-type counters.

---

## 5. AER Injection for CI Testing

The `aer-inject` kernel module (`drivers/pci/pcie/aer_inject.c`, loaded via
`CONFIG_PCIEAER_INJECT=m`) provides a debugfs interface to inject synthetic AER events:

```bash
# Load the injection module
modprobe aer-inject

# Inject a BadTLP correctable error on device 0000:01:00.0
cat > /dev/aer-inject << EOF
{
  "id": "0000:01:00.0",
  "error_type": "correctable",
  "correctable_status": 0x40   # BadTLP bit
}
EOF
```

Alternatively, the `aer-inject` tool (from the kernel tools directory) provides a
command-line interface:

```bash
aer-inject --id 0000:01:00.0 --error correctable --ce_status 0x40
```

After injection, the kernel's AER handler runs as if the hardware had signaled the error,
and the sysfs counters increment accordingly. The fault-injection script
`fault-injection/inject_aer.sh` wraps this interface for use in CI.

---

## 6. ACPI EINJ — Platform-Level Error Injection

For ARM servers with ACPI support, EINJ (Error INJection) provides a firmware-mediated path
to inject errors at the platform level, including memory errors and PCIe errors:

```bash
# Check available error types
cat /sys/kernel/debug/apei/einj/available_error_type

# Inject a PCIe uncorrectable non-fatal error
echo 0x00000010 > /sys/kernel/debug/apei/einj/error_type   # PCIe UNF
echo 0x00000000 > /sys/kernel/debug/apei/einj/flags        # no flags
echo 1          > /sys/kernel/debug/apei/einj/error_inject
```

EINJ is more invasive than `aer-inject` (it involves firmware) and requires the platform
to implement the EINJ ACPI table. It is not available in QEMU virt; the
`fault-injection/inject_aer.sh` script detects availability and falls back to `aer-inject`.

---

## 7. Operational Interpretation

### Reading AER counters to identify a degrading link

```bash
# Watch correctable error rate on all PCIe devices
watch -n 5 'for dev in /sys/bus/pci/devices/*/aer_dev_correctable; do
    bdf=$(echo $dev | grep -oP "[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]");
    total=$(grep TOTAL_ERR_COR $dev | awk "{print \$2}");
    [ "$total" -gt "0" ] && echo "$bdf: $total correctable errors";
done'
```

### Correlating AER events with device driver resets

AER recovery events appear in dmesg with a consistent format:

```
pcieport 0000:00:01.0: AER: Corrected error received: 0000:01:00.0
pcieport 0000:00:01.0: AER: PCIe Bus Error: severity=Corrected, type=Physical Layer, (Receiver ID)
     device [1234:5678] error status/mask=00000040/00002000
              [ 6] Bad TLP
```

For uncorrectable errors followed by recovery:

```
pcieport 0000:00:01.0: AER: Uncorrected (Non-Fatal) error received: 0000:01:00.0
pcieport 0000:00:01.0: AER: broadcast error_detected message
0000:01:00.0: error_detected: state=pci_channel_io_normal
0000:01:00.0: [driver]: link reset successful
pcieport 0000:00:01.0: AER: broadcast resume message
```

The `replay/parse_edac_trace.py` script parses these dmesg patterns into structured JSON
for replay and postmortem analysis.
