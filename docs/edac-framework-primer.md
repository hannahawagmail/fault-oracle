# Linux EDAC Framework Primer

This document explains the Linux kernel's EDAC (Error Detection And Correction) subsystem
from first principles — aimed at engineers familiar with Linux but new to hardware error
reporting, and at hardware engineers unfamiliar with how the kernel handles their error signals.

---

## 1. What EDAC Is

EDAC is the Linux kernel's standardized interface for memory error reporting. It was originally
written for x86 server memory controllers and has been extended to cover ARM memory controllers,
cache controllers, and (via ACPI APEI) ARM RAS errors.

The core design principle of EDAC is **separation of concerns**:

- The hardware-specific driver knows how to read error status registers for a particular
  memory controller. It does not know about sysfs, userspace, or Prometheus.
- The EDAC core (`drivers/edac/edac_mc.c`) knows how to expose error counts through sysfs,
  trigger notifications, and apply policies (page-offline thresholds). It does not know
  anything about specific hardware.

A driver author writes code that calls `edac_mc_handle_error()`. Everything else is handled
by the EDAC core.

---

## 2. Key Abstractions

### 2.1 `mem_ctl_info` — the memory controller

`struct mem_ctl_info` (`include/linux/edac.h`) is the central data structure. It represents
one memory controller and contains:

- `mc_name`: human-readable name (e.g., `"Cortex-A72 L2 ECC"`)
- `nr_csrows`: number of chip-select rows (DIMM slots × ranks)
- `edac_cap`: capabilities mask (EDAC_FLAG_SECDED, EDAC_FLAG_S4ECD4ED, etc.)
- `edac_check`: pointer to the poll function (if polling rather than interrupt-driven)
- `layers[]`: logical dimensions of the error location space (csrow, channel, etc.)
- `pvt_info`: pointer to driver private data

### 2.2 `csrow_info` — a chip-select row

A csrow (chip-select row) corresponds to a rank on a DIMM. In a typical dual-rank DDR4 DIMM,
there are two csrows. Each csrow contains:

- `nr_channels`: number of memory channels serving this csrow
- `grain`: minimum reporting granularity in bytes (typically 8 bytes = one ECC symbol)
- `mtype`: memory type (MEM_DDR4, MEM_LPDDR4, etc.)
- `edac_mode`: EDAC operating mode (EDAC_SECDED, EDAC_S4ECD4ED, etc.)

### 2.3 `dimm_info` — a physical DIMM

`struct dimm_info` represents the DIMM installed in a particular csrow / channel combination.
It carries the DIMM label (slot identifier), memory size, and location within the csrow/channel
space.

---

## 3. The sysfs Interface

After a driver registers with `edac_mc_add_mc()`, the EDAC core creates the following sysfs
tree (for a system with one memory controller, `mc0`, with two csrows and two channels):

```
/sys/devices/system/edac/
└── mc/
    └── mc0/
        ├── ce_count              # total correctable errors on this controller
        ├── ce_noinfo_count       # CEs with no location info
        ├── ue_count              # total uncorrectable errors
        ├── ue_noinfo_count       # UEs with no location info
        ├── mc_name               # "Cortex-A72 L2 ECC"
        ├── size_mb               # total memory managed (MB)
        ├── reset_counters        # write "1" to reset all counters
        ├── sdram_scrub_rate      # scrubber rate in bytes/second (if supported)
        ├── csrow0/
        │   ├── ce_count          # CEs on this csrow
        │   ├── ue_count          # UEs on this csrow
        │   ├── ch0_ce_count      # CEs on channel 0 of this csrow
        │   ├── ch0_dimm_label    # slot identifier (e.g., "DIMM_A0")
        │   ├── ch1_ce_count      # CEs on channel 1 of this csrow
        │   └── ch1_dimm_label
        └── csrow1/
            ├── ce_count
            ├── ue_count
            ├── ch0_ce_count
            └── ch1_ce_count
```

All counters are cumulative since boot (or last reset). They are `unsigned long` in the kernel
and exposed as decimal ASCII strings in sysfs. The Prometheus exporter reads these files and
emits them as monotonically increasing counters, which is the correct Prometheus type for
cumulative hardware event counts.

---

## 4. The Driver Registration Flow

A minimal EDAC driver follows this sequence:

```c
/* 1. Allocate the mem_ctl_info structure */
mci = edac_mc_alloc(mc_num, ARRAY_SIZE(layers), layers, sizeof(*priv));

/* 2. Fill in controller metadata */
mci->mc_idx      = mc_num;
mci->mtype_cap   = MEM_FLAG_DDR4;
mci->edac_ctl_cap = EDAC_FLAG_SECDED;
mci->edac_cap    = EDAC_FLAG_SECDED;
mci->mod_name    = KBUILD_MODNAME;
mci->ctl_name    = "cortex-a72-l2-ecc";
mci->dev_name    = dev_name(&pdev->dev);
mci->edac_check  = cortex_ref_poll;   /* called by EDAC core every poll_msec ms */
mci->pvt_info    = priv;

/* 3. Register with the EDAC core */
ret = edac_mc_add_mc(mci);

/* 4. Set polling interval (milliseconds) */
edac_mc_reset_delay_period(1000);     /* poll every 1 second */
```

The poll function `cortex_ref_poll()` is called by the EDAC core's internal workqueue at the
configured interval. It reads hardware registers (or, in the reference driver, a simulated
counter) and calls `edac_mc_handle_error()` for each detected fault:

```c
edac_mc_handle_error(
    HW_EVENT_ERR_CORRECTED,  /* or HW_EVENT_ERR_UNCORRECTED */
    mci,
    1,                       /* error count */
    page,                    /* physical page number */
    offset_in_page,          /* byte offset within page */
    syndrome,                /* ECC syndrome word */
    csrow,                   /* csrow index */
    channel,                 /* channel index */
    -1,                      /* other_detail index (-1 = none) */
    "correctable L2 cache error"
);
```

The EDAC core increments the appropriate sysfs counters, logs to dmesg, and (for UEs) invokes
`memory_failure()` if the kernel was compiled with `CONFIG_MEMORY_FAILURE`.

---

## 5. Interrupt-Driven vs. Poll-Driven Reporting

EDAC drivers operate in one of two modes:

**Poll-driven:** The driver registers an `edac_check` function pointer. The EDAC core calls
it on a configurable timer (controlled by `/sys/module/edac_core/parameters/poll_msec`,
default 1000ms). This is appropriate for controllers that report errors by setting a bit in
a status register and waiting for software to read it.

**Interrupt-driven:** The driver registers an interrupt handler that calls
`edac_mc_handle_error()` directly when the hardware interrupts on an error. This provides
lower-latency reporting (milliseconds instead of up to 1 second) and is preferred for
production implementations where error latency matters. The reference driver in this repo
uses polling to remain QEMU-compatible; a real production driver would use interrupts.

To use interrupt-driven reporting: do not set `mci->edac_check`; instead, call
`edac_mc_handle_error()` from the interrupt handler. The EDAC core is interrupt-safe.

---

## 6. EDAC and ACPI APEI (ARM Servers)

On ARM servers that implement ACPI APEI (Advanced Platform Error Interface), the EDAC
subsystem does not use a custom driver for DRAM errors. Instead:

1. Firmware detects the error and writes a CPER (Common Platform Error Record) to a shared
   memory region described by the HEST (Hardware Error Source Table).
2. Firmware signals the OS via an NMI-equivalent mechanism (IRQ or SError on ARM64).
3. The kernel's `drivers/acpi/apei/ghes.c` reads the CPER record.
4. GHES calls `ghes_edac_report_mem_error()`, which routes the error into the EDAC core
   as if a normal EDAC driver had reported it.

The sysfs interface is identical regardless of whether errors arrive via a platform-specific
driver or via GHES. The Prometheus exporter does not need to know which path was used.

---

## 7. EDAC Error Thresholding and Page Offline

The EDAC core implements a simple CE threshold mechanism:

- `edac_mc_get_error_panic_threshold()` returns the configured UE threshold.
- If `CONFIG_MEMORY_FAILURE` is enabled and a UE is reported with a valid physical address,
  `memory_failure(page, MF_ACTION_REQUIRED)` is called automatically by the EDAC core.

For CEs, the EDAC core itself does not apply a threshold — it counts and logs. The policy
decision (at what CE rate to offline a DIMM) is made by userspace tooling (such as the
`edac-utils` package, or a custom daemon that reads the Prometheus metrics and calls
`echo offline > /sys/devices/system/memory/memory<N>/state` when thresholds are exceeded).

This design — kernel provides mechanisms, userspace applies policy — is intentional. CE
thresholds are fleet-specific: a threshold appropriate for spinning-disk storage servers may
be inappropriate for latency-sensitive inference servers.

---

## 8. Kernel Configuration Requirements

To use the EDAC subsystem, the kernel must be built with:

```
CONFIG_EDAC=y                    # EDAC core
CONFIG_EDAC_LEGACY_SYSFS=y       # /sys/devices/system/edac/ interface
CONFIG_EDAC_DEBUG=y              # optional: verbose debug output
CONFIG_MEMORY_FAILURE=y          # automatic page-offline on UE (strongly recommended)
CONFIG_ACPI_APEI=y               # APEI support (ARM servers with GHES)
CONFIG_ACPI_APEI_GHES=y          # Generic Hardware Error Source
CONFIG_FAULT_INJECTION=y         # kernel fault injection framework (for CI)
CONFIG_FAIL_PAGE_ALLOC=y         # fault injection into page allocator
CONFIG_FAULT_INJECTION_DEBUG_FS=y # debugfs interface for fault injection
```

For the reference driver in this repository, the minimal required configuration is
`CONFIG_EDAC=y` and `CONFIG_EDAC_LEGACY_SYSFS=y`. The QEMU boot script (`ci/qemu-boot.sh`)
starts a kernel built with all of the above enabled.
