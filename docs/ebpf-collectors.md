# eBPF Kernel-Native Collectors

## Overview

The eBPF collectors replace sysfs polling for EDAC, MCE, and PCIe AER events with
zero-latency kernel tracepoint hooks. Instead of reading `/sys/devices/system/edac/mc*/`
every 10 seconds, a BPF program fires on each kernel `ras:mc_event` tracepoint call —
the same instant the kernel records the error.

## Tracepoints

| Tracepoint | Kernel source | Data captured |
|---|---|---|
| `ras:mc_event` | `drivers/edac/edac_mc.c` | mc, csrow, channel, error_type, address |
| `ras:aer_event` | `drivers/pci/pcie/aer.c` | dev_name, severity, error_type |
| `ras:mce_record` | `arch/x86/kernel/cpu/mcheck/mce.c` | status, addr, bank |

All three write structured events to a **BPF ring buffer** (256 KB for EDAC/AER/MCE events in `edac_trace.bpf.c`; the workload attribution program uses a separate 128 KB ring buffer in `workload_attr.bpf.c`). A Go goroutine drains the ring buffer continuously and increments Prometheus counters.

## Kernel Requirements

| Requirement | Min version | Why |
|---|---|---|
| BPF ring buffer | 5.8 | `BPF_MAP_TYPE_RINGBUF` |
| BTF + CO-RE | 5.4 | Portable struct access without kernel headers |
| `ras:` tracepoints | 4.14 | Upstream EDAC tracepoints |
| `CAP_BPF` (or `CAP_SYS_ADMIN`) | 5.8 | Unprivileged BPF load |

AWS Graviton3 (Amazon Linux 2023, kernel 6.1): **all requirements met.**

## Comparison: eBPF vs sysfs polling

| Property | sysfs polling (current) | eBPF (new) |
|---|---|---|
| Latency | Up to 10s (scrape interval) | < 1ms (tracepoint fires synchronously) |
| Missed events | Yes — bursts between scrapes invisible | No — every event captured |
| CPU overhead | ~0.1% (file reads) | ~0.01% (BPF program, sampling-reduced) |
| Kernel module required | No | No (BPF, no DKMS) |
| Minimum kernel | 3.x (EDAC sysfs) | 5.8 (ring buffer) |
| Cloud VM support | Yes | Requires ras: tracepoints (usually present) |
| Graceful fallback | n/a | Yes → sysfs collector_up=1 |

## eBPF metric naming

| Metric | Description |
|---|---|
| `ebpf_edac_ce_total{mc, top_layer, mid_layer}` | CE events via tracepoint |
| `ebpf_edac_ue_total{mc, top_layer, mid_layer}` | UE events via tracepoint |
| `ebpf_aer_total{severity}` | AER events (correctable/uncorrectable/fatal) |
| `ebpf_mce_total{severity="uncorrectable"}` | MCE records (severity is always `"uncorrectable"` — the BPF program unconditionally sets UE severity for all MCE records) |
| `fault_resilience_collector_up{collector="ebpf_edac"}` | 1=loaded, 0=unavailable |

## Build

```bash
# Requires clang 14+, libbpf, bpftool
make -C ebpf vmlinux.h          # generate BTF header from running kernel
make -C ebpf edac_trace.bpf.o   # compile BPF C → .o
make -C ebpf generate           # run bpf2go → generate Go bindings
make build                       # builds Go exporter with eBPF support
```

## Graceful Fallback

When eBPF is unavailable (`kernel < 5.8`, no BTF, `EBPF_DISABLED=1`):
- `EBPFAvailable()` returns `false`
- `ebpf_edac` collector emits `fault_resilience_collector_up{collector="ebpf_edac"}=0`
- The existing sysfs-based EDAC/MCE/AER collectors continue working
- `EBPFCollectorDown` alert fires at severity=info (expected on cloud VMs)

## Workload Attribution

The secondary `workload_attr.bpf.c` hooks `handle_mm_fault` (kprobe) to map:
```
process (comm, pid) → NUMA node → CE rate correlation
```
At 1-in-100 sampling rate, overhead is < 0.01% even at 100k page faults/sec.
Emits `ebpf_ce_by_workload_total{comm, mc, numa_node}` for the Grafana CE attribution heatmap.
