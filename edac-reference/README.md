# edac-reference — ARM Cortex-A72 EDAC Reference Driver

This directory contains a generic, poll-based EDAC kernel driver for ARM Cortex-A72
platforms. It is designed to:

- Run without modification under QEMU virt (ARM64) — no real hardware required
- Demonstrate every standard EDAC framework interface (registration, polling, CE/UE
  reporting, sysfs counters, debugfs fault injection)
- Serve as a readable reference for engineers writing EDAC drivers for real platforms

---

## Files

| File | Purpose |
|------|---------|
| `edac_cortex_ref.c` | The kernel module — main driver: poll loop, debugfs, platform driver |
| `cortex_ref_ras.c` | ARM RAS Extension register accessors (separate translation unit) |
| `cortex_ref_ras.h` | Public declarations for the RAS helpers (included by main driver) |
| `Makefile` | Out-of-tree kernel module build system |
| `Kconfig` | Kconfig entry (for in-tree integration) |
| `test/verify_edac_sysfs.sh` | Post-load verification: checks all expected sysfs entries |
| `test/verify_ras_registers.sh` | Phase 2 verification: RAS integration, inject_poison, stats |

---

## Driver Architecture

The driver implements the **poll-based** EDAC model:

```
EDAC core workqueue (every poll_msec ms)
      │
      └──► cortex_ref_poll()
               │
               ├── atomic_xchg(&priv->sim_ce_count, 0)  ← read + clear CE register
               │         │
               │         └── edac_mc_handle_error(HW_EVENT_ERR_CORRECTED, ...)
               │                   │
               │                   └──► /sys/devices/system/edac/mc0/ce_count  ++
               │                        /sys/devices/system/edac/mc0/csrow0/ch0_ce_count  ++
               │                        dmesg: "EDAC MC0: 1 CE ..."
               │
               └── atomic_xchg(&priv->sim_ue_count, 0)  ← read + clear UE register
                         │
                         └── edac_mc_handle_error(HW_EVENT_ERR_UNCORRECTED, ...)
                                   │
                                   └──► /sys/devices/system/edac/mc0/ue_count  ++
                                        memory_failure() (if CONFIG_MEMORY_FAILURE=y)
```

On real hardware, `sim_ce_count` / `sim_ue_count` would be replaced by `readl()` calls
to the memory controller's ECC status registers.

---

## Build

### Native ARM64 (on an ARM64 Linux host)

```bash
make KERNELDIR=/lib/modules/$(uname -r)/build
```

### Cross-compile from x86 host

```bash
make ARCH=arm64 \
     CROSS_COMPILE=aarch64-linux-gnu- \
     KERNELDIR=/path/to/arm64-linux-build
```

### QEMU (using a locally built arm64 kernel)

The CI script `ci/qemu-boot.sh` builds and boots a complete QEMU environment:

```bash
../../ci/qemu-boot.sh --module edac_cortex_ref.ko
```

---

## Loading and Verifying

```bash
# Load the module
sudo insmod edac_cortex_ref.ko

# Verify it registered with the EDAC core
dmesg | grep edac_cortex_ref
# Expected: "edac_cortex_ref: module loaded (version 1.0.0)"
# Expected: "edac_cortex_ref v1.0.0 loaded: 2 csrows × 2 channels, 2048 MiB/csrow, poll=1000 ms"

# Verify sysfs entries
ls /sys/devices/system/edac/mc/mc0/
# Expected: ce_count  ue_count  mc_name  size_mb  csrow0/  csrow1/  ...

cat /sys/devices/system/edac/mc/mc0/mc_name
# Expected: cortex-a72-l2-ecc

# Run the automated sysfs verification script
sudo bash test/verify_edac_sysfs.sh
```

---

## Fault Injection

The driver exposes a debugfs interface for CI fault injection:

```bash
# Check that debugfs is mounted
mount | grep debugfs
# If not: sudo mount -t debugfs debugfs /sys/kernel/debug

# Inject 3 correctable errors on csrow 0, channel 0 (default target):
echo 1 > /sys/kernel/debug/edac_cortex_ref/inject_ce   # inject 1 CE

# Change injection target to csrow 1, channel 1:
echo 1 > /sys/kernel/debug/edac_cortex_ref/inject_csrow
echo 1 > /sys/kernel/debug/edac_cortex_ref/inject_channel

# Inject 5 CEs on the new target:
echo 5 > /sys/kernel/debug/edac_cortex_ref/inject_ce

# Verify counter incremented:
cat /sys/devices/system/edac/mc/mc0/csrow1/ch1_ce_count
# Expected: 5

# Inject an uncorrectable error:
echo 1 > /sys/kernel/debug/edac_cortex_ref/inject_ue

# View cumulative injection statistics:
cat /sys/kernel/debug/edac_cortex_ref/stats
# Expected:
# total_ce_injected: 6
# total_ue_injected: 1
```

After each injection, the kernel poll loop (running every `poll_msec` ms) picks up
the pending errors and calls into the EDAC core. The delay between writing to `inject_ce`
and seeing the counter increment in sysfs is at most `poll_msec` (default: 1 second).

---

## Module Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `poll_msec` | 1000 | Poll interval in milliseconds. `0644` — can be changed at runtime via `/sys/module/edac_cortex_ref/parameters/poll_msec` |
| `nr_csrows` | 2 | Number of chip-select rows to model. `0444` — set at load time only |
| `nr_channels` | 2 | Number of channels per csrow. `0444` — set at load time only |

```bash
# Load with custom parameters:
sudo insmod edac_cortex_ref.ko poll_msec=500 nr_csrows=4 nr_channels=2

# Change poll interval at runtime:
echo 250 > /sys/module/edac_cortex_ref/parameters/poll_msec
```

---

## ARM RAS Extension Integration

The Phase 2 update adds `cortex_ref_ras.c`, a helper translation unit that
provides typed accessors to the ARM Reliability, Availability, and
Serviceability (RAS) Extension system registers. It compiles alongside the
main driver and links into the same `edac_cortex_ref.ko`.

### What RAS Extension Registers Provide

The ARM RAS Extension (defined in ARM DDI 0487, chapter D17) exposes a bank
of per-error-source registers, selected by writing an index to `ERRSELR_EL1`.
Once selected, three registers describe the most recently captured error:

| Register | Description |
|----------|-------------|
| `ERRXSTATUS_EL1` | Status flags: valid (V), corrected (CE), uncorrected (UE), overflow (OF), SERR and IERR error codes |
| `ERRXADDR_EL1` | Physical address of the error (valid only when `ERRXSTATUS.AV=1`) |
| `ERRXMISC_EL1` | Implementation-defined miscellaneous diagnostic data |

These registers operate independently of the memory controller MMIO space and
are directly accessible from EL1 via `MRS` instructions, making them usable
from kernel code without ioremap.

### SERR Field Encoding

`ERRXSTATUS_EL1[7:0]` carries the `SERR` field, an architecturally defined
error type code:

| SERR | Meaning |
|------|---------|
| `0x00` | No error |
| `0x02` | ECC error on cache data RAM |
| `0x06` | ECC error on cache tag/dirty RAM |
| `0x12` | Bus error |
| other  | Implementation defined or reserved |

`cortex_ref_serr_name(serr)` maps these codes to human-readable strings for
use in kernel log messages.

### How `cortex_ref_ras.c` Fits into the Module

```
edac_cortex_ref.ko
├── edac_cortex_ref.c   — poll loop, debugfs, platform driver, module init/exit
│       └── #includes cortex_ref_ras.h  (declarations)
└── cortex_ref_ras.c    — ARM RAS register accessors (separate translation unit)
        ├── cortex_ref_read_ras_record()   — MRS ERRSELR / ERRXSTATUS / ERRXADDR / ERRXMISC
        ├── cortex_ref_clear_ras_record()  — MSR ERRXSTATUS (write-1-to-clear V bit)
        ├── cortex_ref_parse_status()      — bit-field decoder, arch-independent
        └── cortex_ref_serr_name()         — SERR code -> human-readable string
```

Inside `cortex_ref_poll()`, the driver calls `cortex_ref_read_ras_record(0, &rec)`
on each poll cycle (arm64 builds only, guarded by `#ifdef CONFIG_ARM64`). If
the record is valid (`rec.valid == true`), the SERR field is logged at
`pr_info` level and the record is cleared via `cortex_ref_clear_ras_record(0)`
so the hardware can capture the next error.

### The `irq_mode` Module Parameter

| Value | Behaviour |
|-------|-----------|
| `false` (default) | The EDAC core workqueue calls `cortex_ref_poll()` every `poll_msec` ms. `cortex_ref_irq_drain()` is called from poll. |
| `true` | `cortex_ref_poll()` is a no-op. Errors must be drained by an IRQ handler that calls `cortex_ref_irq_drain()` directly. |

Use `irq_mode=true` when the memory controller wires a dedicated error
interrupt and low-latency UE reporting is required. On QEMU, use the default
`irq_mode=false` because the virt machine model does not expose a memory
controller IRQ.

```bash
# Load in interrupt-driven mode (real hardware with wired IRQ):
sudo insmod edac_cortex_ref.ko irq_mode=1

# Change at runtime:
echo 1 > /sys/module/edac_cortex_ref/parameters/irq_mode
```

### The `inject_poison` Debugfs Knob

```
/sys/kernel/debug/edac_cortex_ref/inject_poison   (write-only, 0200)
```

Writing a non-zero value to this file:

1. Allocates an anonymous kernel page via `vmalloc(PAGE_SIZE)`.
2. Calls `memory_failure(pfn, 0)` on the page's physical frame number.
3. Logs the PFN to dmesg.
4. Frees the vmalloc region (the physical page remains on the kernel's
   bad-page list and is never re-allocated by the buddy allocator).

This simulates a UE that the kernel cannot correct, triggering the
**page-offline / DIMM replacement escalation path**:

```
inject_poison write
      │
      └──► memory_failure(pfn, 0)
                │
                ├── Take page offline (remove from buddy allocator)
                ├── Unmap from all user PTEs → SIGBUS to owner process
                ├── Update /sys/devices/system/memory/memoryN/state → "offline"
                └── (with ACPI/APEI) Notify firmware → DIMM replacement advisory
```

Requires `CONFIG_MEMORY_FAILURE=y`. On kernels without this option the write
returns `-ENOSYS` and logs: `edac_cortex_ref: inject_poison: SKIP: CONFIG_MEMORY_FAILURE not set`.

### QEMU Compatibility Note

RAS Extension `MRS` instructions (`mrs %0, erxstatus_el1`, etc.) will
generate an **Undefined Instruction** exception in standard QEMU because the
virt machine model does not implement the RAS extension system registers.

The `#ifdef CONFIG_ARM64` guard in `cortex_ref_poll()` means the MRS path is
only compiled into arm64 kernel builds — it is not executed on x86 hosts.
However, even on arm64 targets running under plain QEMU (no KVM), these
instructions will fault.

**Recommended configurations:**

| Environment | RAS register path | Fault injection path |
|-------------|-------------------|----------------------|
| QEMU (no KVM) | Not available — will fault | Works via `inject_ce` / `inject_ue` |
| QEMU + KVM with `+ras` CPU flag | Available | Works |
| Real Cortex-A72/A76/N2 hardware | Available | Works |

To enable RAS registers under QEMU/KVM, add `-cpu host,+ras` to your QEMU
command line.

---

## Adapting to Real Hardware

To adapt this driver to a real ARM memory controller:

1. Replace the `platform_device_register_simple()` call in `cortex_ref_init()` with a
   proper `platform_driver` that matches your Device Tree `compatible` string or ACPI HID.

2. In `cortex_ref_probe()`, request and ioremap your memory controller's MMIO region:
   ```c
   priv->base = devm_platform_ioremap_resource(pdev, 0);
   ```

3. Replace `atomic_xchg(&priv->sim_ce_count, 0)` in `cortex_ref_poll()` with actual
   register reads. For example, a memory controller that reports CE count in a register
   at offset `0x100`:
   ```c
   ce_count = readl(priv->base + MC_ECC_CE_COUNT_REG);
   writel(0, priv->base + MC_ECC_CE_COUNT_REG);  /* clear on write */
   ```

4. If the controller supports error interrupts, remove the `mci->edac_check` assignment
   and instead call `edac_mc_handle_error()` from your IRQ handler for lower latency.

5. Replace the simulated DIMM labels with SPD/SMBIOS data:
   ```c
   snprintf(dimm->label, sizeof(dimm->label), "%s",
            smbios_get_dimm_label(csrow, channel));
   ```
