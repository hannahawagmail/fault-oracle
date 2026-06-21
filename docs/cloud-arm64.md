# Cloud ARM64 Instance Compatibility

## Summary

The hw-fault-exporter runs on cloud ARM64 instances, but most hardware error
subsystems are **not exposed** in virtualized environments. This is expected:
hypervisors intercept hardware errors before the guest OS sees them.

`fault_resilience_collector_up{collector="..."}=0` is the **correct** output
on cloud VMs — it means the subsystem is absent, not that the exporter is broken.

## Per-Instance-Type Availability Matrix

| Instance Family | CPU | EDAC | MCE | PCIe AER | Thermal | cpufreq | PMU |
|-----------------|-----|------|-----|----------|---------|---------|-----|
| AWS Graviton2 (m6g, c6g, r6g) | Neoverse N1 | ✗ | ✗ | ✗ | ✗ | ✗ (fixed freq) | arm_dsu ✓ |
| AWS Graviton3 (m7g, c7g, r7g) | Neoverse V1 | ✗ | ✗ | ✗ | ✗ | ✗ | arm_cmn ✓ |
| AWS Graviton4 (m8g, c8g)      | Neoverse V2 | ✗ | ✗ | ✗ | ✗ | ✗ | arm_cmn ✓ |
| Azure Dpsv5 (Ampere Altra)    | Neoverse N1 | ✗ | ✗ | ✗ | ✗ | ✗ | partial |
| Azure Dpdsv6 (Cobalt 100)     | Neoverse N2 | ✗ | ✗ | ✗ | ✗ | ✗ | arm_cmn ✓ |
| GCP T2A (Ampere Altra)        | Neoverse N1 | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Bare-metal ARM64 (Ampere, HiSilicon Kunpeng) | varies | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

**Legend:** ✓ = available, ✗ = absent/virtualized

## Expected Metrics on Cloud Instances

```
# AWS Graviton3 — expected output
fault_resilience_collector_up{collector="edac"}    0
fault_resilience_collector_up{collector="mce"}     0
fault_resilience_collector_up{collector="aer"}     0
fault_resilience_collector_up{collector="thermal"} 0
fault_resilience_collector_up{collector="cpufreq"} 0
fault_resilience_collector_up{collector="pmu"}     1  # arm_cmn present
```

## Deploying on Cloud Instances

Use `--no-edac --no-mce --no-aer` to suppress expected warnings:

```bash
hw-fault-exporter \
  --no-edac --no-mce --no-aer \
  --listen-addr :9101 \
  --log-level warn
```

Or use the cloud-mode Helm values overlay:

```yaml
# values-cloud.yaml
args:
  - --no-edac
  - --no-mce
  - --no-aer
  - --log-level=warn
```

## References

- [AWS Graviton Processor](https://aws.amazon.com/ec2/graviton/)
- [Azure Cobalt 100](https://learn.microsoft.com/en-us/azure/virtual-machines/cobalt-overview)
- [Ampere Altra Platform Security](https://amperecomputing.com/products/processors/altra)
