# ARM PMU Memory Bandwidth Monitoring

## Overview

ARM SoCs expose memory bandwidth counters through the Performance Monitoring Unit (PMU).
On server-class ARM SoCs (Neoverse N1/N2/V1), two PMU interfaces are relevant:

- **ARM CMN (Cache Mesh Network) PMU** — tracks AMBA bus transactions between the
  mesh interconnect and DRAM controllers. Available on Neoverse N2, V1, and later.
- **ARM DSU (DynamIQ Shared Unit) PMU** — cluster-level PMU tracking L3 cache
  transactions including external memory requests. Available on Neoverse N1, A55, A77.

## sysfs Interface

```
/sys/bus/event_source/devices/
  arm_cmn_0/
    events/
      amba_reads    — AMBA read beat count (each beat = 64 bytes on CHI)
      amba_writes   — AMBA write beat count
    cpumask         — CPU affinity for reading this PMU
    type            — PMU type ID for perf_event_open
  arm_dsu_0/
    events/
      l3d_cache_refill   — L3 cache miss → external memory fetch
      l3d_cache_wb       — L3 writeback → external memory write
```

## perf_event_open Approach (production)

The exporter's sysfs-based approach reads aggregate counters already exposed by
the kernel. For higher-fidelity per-CPU sampling, use `perf_event_open(2)`:

```c
struct perf_event_attr attr = {
    .type = /* read from /sys/bus/event_source/devices/arm_cmn_0/type */,
    .config = /* event ID from /events/amba_reads */,
    .disabled = 1,
    .inherit = 1,
};
int fd = perf_event_open(&attr, -1, cpu, -1, 0);
```

## Availability by Platform

| Platform | PMU Type | Kernel Version |
|----------|----------|---------------|
| Neoverse N1 (AWS Graviton2) | arm_dsu | ≥ 5.10 |
| Neoverse N2 (Ampere Altra Max) | arm_cmn | ≥ 5.15 |
| Neoverse V1 (AWS Graviton3) | arm_cmn | ≥ 5.17 |
| Cloud VMs (most) | None exposed | — |

## References

- Linux kernel `drivers/perf/arm-cmn.c`
- Linux kernel `drivers/perf/arm_dsu_pmu.c`
- Arm Neoverse N1 PMU Guide (ARM-EPM-128912)
