# fault-injection — CI Fault Harness

This directory contains scripts for injecting hardware fault events into the
kernel's error-handling paths and verifying that counters, logs, and recovery
mechanisms respond correctly.

## Why Fault Injection in CI?

Recovery code paths for hardware faults share a property with fire-exit doors:
they are only needed in emergencies, and an emergency is the worst time to discover
that the exit is blocked. A correctable-error threshold handler, a page-offline
path, or a PCIe AER recovery sequence that has never actually been exercised is
untested code, regardless of how carefully it was written.

The kernel's fault injection framework (`CONFIG_FAULT_INJECTION`, `CONFIG_PCIEAER_INJECT`)
was designed precisely to address this. It provides stable, debugfs-based interfaces
to force specific error paths to execute, so that CI can verify recovery on every
commit rather than waiting for the event to occur naturally in production.

## Files

| Script | Purpose |
|--------|---------|
| `inject_edac_ce.sh` | Inject correctable EDAC errors; verify counter increment + dmesg |
| `inject_edac_ue.sh` | Inject uncorrectable EDAC errors; verify UE counter + memory_failure path |
| `inject_aer.sh` | Inject PCIe AER errors (correctable or non-fatal) via aer-inject or ACPI EINJ |
| `ci_fault_matrix.sh` | Full CI matrix: all fault types × all controllers × all csrow/channel pairs |

## Quick Run

```bash
# Inject one correctable error on the default csrow/channel:
sudo bash inject_edac_ce.sh

# Inject a UE and check memory_failure path:
sudo bash inject_edac_ue.sh --check-memory-failure

# Run the full matrix (CE and UE only):
sudo bash ci_fault_matrix.sh --types CE,UE --output-dir /tmp/results

# Include AER tests and emit JUnit XML for CI:
sudo bash ci_fault_matrix.sh --types CE,UE,AER_CE,AER_UE \
    --junit /tmp/results/fault-matrix.xml

# Dry run — see what would be tested without injecting anything:
bash ci_fault_matrix.sh --dry-run --types CE,UE,AER_CE,AER_UE
```

## Prerequisites

- `edac_cortex_ref.ko` loaded (see `edac-reference/`)
- debugfs mounted: `mount -t debugfs debugfs /sys/kernel/debug`
- Root access (writing to debugfs requires CAP_SYS_ADMIN)
- For AER injection: `aer-inject` module (`CONFIG_PCIEAER_INJECT=m`) or ACPI EINJ table

## Fault Types Supported

| Type | What is injected | What is verified |
|------|-----------------|-----------------|
| `CE` | Correctable EDAC error | `ce_count`, `csrow<N>/ch<M>_ce_count`, dmesg CE entry |
| `UE` | Uncorrectable EDAC error | `ue_count`, `csrow<N>/ue_count`, dmesg UE entry |
| `AER_CE` | PCIe correctable error (BadTLP) | `aer_dev_correctable` counter, dmesg AER entry |
| `AER_UE` | PCIe non-fatal uncorrectable (CompletionTimeout) | `aer_dev_nonfatal` counter, dmesg AER entry |
