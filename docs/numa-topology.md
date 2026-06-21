# NUMA Topology and EDAC Error Attribution

## DIMM → Channel → Memory Controller → NUMA Node

On multi-socket and NUMA ARM SoCs, the memory hierarchy is:

```
NUMA Node 0                    NUMA Node 1
  └─ mc0 (EDAC)                  └─ mc1 (EDAC)
       ├─ csrow0 (channel 0)          ├─ csrow0
       │    ├─ dimm0 (DIMM A1)        │    └─ dimm0
       │    └─ dimm1 (DIMM A2)        └─ csrow1
       └─ csrow1 (channel 1)
```

## sysfs NUMA Mapping

```
/sys/devices/system/edac/mc/mc<N>/device/numa_node   → NUMA node ID
/sys/devices/system/node/node<M>/                     → NUMA node topology
```

## Impact on Metrics

With NUMA topology enabled, EDAC metrics gain a `numa_node` label:

```
edac_correctable_errors_total{mc="0", csrow="0", numa_node="0"} 42
edac_correctable_errors_total{mc="1", csrow="0", numa_node="1"} 0
```

This allows Grafana to correlate memory errors with CPU-local vs. remote DRAM access,
which is critical for performance-sensitive workloads on NUMA systems.
