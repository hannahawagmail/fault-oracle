# How This Project Protects ML Platforms

## The Problem: Silent Hardware Faults Corrupt Training Runs

ML training workloads are uniquely vulnerable to hardware memory errors because:

1. **Scale amplifies probability.** A single A100/H100 training node holds 256–512 GB of HBM and DDR5. At a DRAM CE rate of 1 error/GB/day (typical for 3-year-old DIMMs), a 256-node training cluster sees **thousands** of correctable errors per day. Most are silently corrected. But each UE is a potential NaN or gradient corruption.

2. **Long jobs have no checkpoints.** A 7-day LLaMA pretraining run cannot restart cleanly from scratch. A silent bit flip in a weight tensor on day 6 produces subtly wrong gradients — the loss curve looks normal, the model trains to completion, and only evaluation reveals the issue. Weeks of compute wasted.

3. **Gradient aggregation spreads corruption.** In AllReduce (NCCL/MPI), a corrupted gradient tensor from one node gets averaged across the entire fleet before the error is detected. The corruption becomes a feature of the distributed gradient.

4. **Memory bandwidth matters.** At 400 GB/s+ per node, a thermally throttled node (stuck at 60% cpufreq) becomes the AllReduce bottleneck. The entire training step waits for the slow node, wasting thousands of GPU-hours per day.

---

## Concrete Failure Scenarios and How This Project Addresses Them

### Scenario 1: Silent DIMM Degradation Before a Long Training Run

**Without this project:**
- Cluster passes health checks (DRAM test completes, GPUs pass DCGM check).
- A DIMM on node `worker-47` is 4 years old, CE rate quietly climbing at 2x/month.
- Training starts. On day 8, DIMM hits UE during backward pass. Process crashes.
- Job restarts from day-5 checkpoint. 3 days of compute (≈ $45,000 at H100 pricing) lost.
- Root cause identified 48h after crash via manual log review.

**With this project:**
```
failure_probability_7d{instance="worker-47", mc="0", csrow="2"} = 0.83
dimm_risk_tier{instance="worker-47", mc="0", csrow="2", tier="critical"} = 1
```
- `DIMMFailureImminent` alert fires 4 days before training starts.
- AI runbook generated: "DIMM in slot B2, Bank 0 showing 83% failure probability.
  Replace before scheduling next training job. ETA to failure: 3–5 days."
- Node drained, DIMM replaced (30 min).
- Training runs clean. $45,000 saved.

**Downtime saved: 72 hours of training time.**

---

### Scenario 2: Thermal Throttle Slowing AllReduce

**Without this project:**
- `worker-31` CPU hits 92°C due to a clogged heatsink. cpufreq scales down to 1.2 GHz.
- NCCL AllReduce step time increases from 800ms to 2,400ms (3× slower).
- Training throughput drops 40% fleet-wide because every node waits for the slow one.
- Ops team notices "training slower than expected" 6 hours later. Manual investigation.
- Node identified, heatsink cleaned. Total throughput loss: 6h × 40% = 2.4 GPU-hours × (fleet size).

**With this project:**
```
cpu_thermal_zone_celsius{instance="worker-31", zone="0"} = 92.4
cpu_scaling_cur_freq{instance="worker-31", cpu="0"} = 1200000  # Hz (throttled from 3.2GHz)
```
- `CPUThermalCritical` fires in 30 seconds (not 6 hours).
- Alert includes: "cpufreq throttled to 37.5% of nominal. AllReduce stragglers expected."
- Correlator links thermal alert + "slow node" dmesg: `thermal thermal_zone0: critical temperature reached`.
- Automated recovery: node pulled from training group, heatsink ticket created.
- Fleet training throughput restored in 12 minutes.

**Throughput saved: 5h 48min × fleet-wide 40% degradation.**

---

### Scenario 3: PCIe AER Errors Corrupting GPU-CPU Tensor Transfers

**Without this project:**
- `worker-19` has a marginal PCIe lane. Occasional non-fatal AER errors during DMA from GPU.
- `torch.cuda.memory_stats()` shows intermittent ECC errors. Engineers assume it's GPU VRAM.
- 3 weeks of debugging. GPU replaced. Problem persists (it's actually the PCIe link).
- Total wasted GPU time: 3 weeks × 8 GPUs = 1,680 GPU-hours.

**With this project:**
```
pcie_aer_nonfatal_total{instance="worker-19", dev_id="0000:03:00.0", error_type="ECRC"} = 847
ebpf_aer_total{severity="uncorrectable", instance="worker-19"} = 12   # in last 10 min
```
- `PCIeAERUncorrectable` fires immediately on the first burst.
- eBPF collector captures 12 non-fatal UEs in 10 minutes that sysfs would have missed (polled every 10s).
- AER taxonomy shows `ECRC` (End-to-End CRC) errors → PCIe link integrity, not GPU VRAM.
- Root cause identified in 15 minutes instead of 3 weeks.

**Debug time saved: 3 weeks. GPU-hours saved: 1,680.**

---

### Scenario 4: Multi-Node CE Storm During Checkpoint Save

**Without this project:**
- 32 nodes writing a 500 GB checkpoint simultaneously hit a memory pressure spike.
- 7 nodes see simultaneous CE bursts (normal under memory pressure).
- rasdaemon logs thousands of events. Engineers unsure if this is normal or pre-failure.
- 8-hour investigation. No action taken. Two weeks later, one node UEs mid-training.

**With this project:**
- Anomaly detector fires: `anomaly_zscore{instance="worker-*"} > 3` across 7 nodes simultaneously.
- Event correlator identifies "CE storm" pattern (≥3 events of same type within 60s) across correlated nodes.
- Survival analysis: those 7 nodes all belong to cohort `ddr4_rank2_3to5yr` with 42% median survival at 90 days.
- Automated report: "7 nodes in checkpoint-storm pattern match high-risk DIMM cohort. Recommend scheduled replacement within 30 days."
- Replacements scheduled during next maintenance window. Zero UE incidents in next quarter.

**Prevented: 1 UE incident with $45K+ compute loss and 72h debugging.**

---

### Scenario 5: RISC-V/ARM Heterogeneous Cluster Monitoring Gap

**Without this project:**
- A heterogeneous cluster runs both x86_64 (inference) and ARM64 (training) nodes.
- ARM64 nodes have EDAC/CXL sysfs paths that differ from x86. Monitoring dashboards show gaps.
- On-call gets `no data` for ARM nodes in Grafana. No alerting.

**With this project:**
- `compat/cloud-probe.sh --json` reports per-node capability matrix on cluster join.
- ARM64 nodes: EDAC=1, MCE=1, AER=1, PMU=1.
- x86 nodes: EDAC=1, MCE=1, AER=1, PMU=0 (ARM PMU not available).
- `fault_resilience_collector_up{collector="pmu"}=0` on x86 is expected, not an alert.
- Single Grafana fleet dashboard covers all architectures with correct conditional panels.

---

## Quantified Impact Summary

| Failure Mode | Detection Time (Before) | Detection Time (After) | Compute Loss Prevented |
|---|---|---|---|
| DIMM pre-failure | Post-crash (0 warning) | 4–7 days before failure | 72h training run |
| Thermal throttle | 6 hours (manual) | 30 seconds (alert) | 6h × fleet-wide 40% |
| PCIe AER corruption | 3 weeks (wrong root cause) | 15 minutes | 1,680 GPU-hours |
| CE storm / UE precursor | 8 hours + 1 UE incident | Automated, maintenance window | $45K+ per incident |
| Architecture monitoring gap | Never detected | Automated on cluster join | N/A (coverage gap closed) |

---

## Integration Points for ML Platforms

### PyTorch / JAX Training Loop

Add a pre-training health gate that blocks job submission if any node in the job's allocation has `failure_probability_7d > 0.5`:

```python
# launcher/health_gate.py
import urllib.request, json

def check_node_health(prometheus_url: str, nodes: list[str]) -> list[str]:
    """Return list of unhealthy nodes. Raises if > 10% of allocation is unhealthy."""
    query = 'failure_probability_7d > 0.5'
    resp  = urllib.request.urlopen(f"{prometheus_url}/api/v1/query?query={query}")
    data  = json.loads(resp.read())
    at_risk = {r["metric"]["instance"] for r in data["data"]["result"]}
    unhealthy = [n for n in nodes if n in at_risk]
    if len(unhealthy) / len(nodes) > 0.1:
        raise RuntimeError(f"Job blocked: {len(unhealthy)}/{len(nodes)} nodes at risk: {unhealthy}")
    return unhealthy
```

### Slurm / Kubernetes Job Scheduler Integration

```yaml
# Kubernetes admission webhook: block pod scheduling on at-risk nodes
apiVersion: v1
kind: ConfigMap
metadata:
  name: hw-fault-scheduler-extender
data:
  config: |
    filterVerb: filter
    urlPrefix: http://hw-fault-scheduler.monitoring.svc:9200
    # Rejects nodes where failure_probability_7d > 0.5
    # or fault_resilience_collector_up{collector="edac"} == 0
```

### NVIDIA DCGM + This Project (Complementary)

| Signal | DCGM | This Project |
|--------|------|-------------|
| GPU ECC errors | ✓ | — |
| CPU/DRAM EDAC errors | — | ✓ |
| PCIe AER errors | Partial | ✓ Full taxonomy |
| Thermal throttle (CPU) | — | ✓ |
| Memory bandwidth (PMU) | — | ✓ |
| Failure prediction (ML) | — | ✓ |
| Recovery automation | — | ✓ |

DCGM covers the GPU; this project covers everything between the GPU and the CPU. Together they provide complete node health visibility.

---

## eBPF vs sysfs: Why It Matters for ML Workloads

Standard sysfs-polling (10s scrape interval) misses CE bursts during:
- **Checkpoint writes** (sudden DRAM pressure spike, hundreds of CEs in 2s)
- **AllReduce operations** (memory bandwidth saturation)
- **Model loading** (large tensor copies saturate DRAM bandwidth)

The eBPF collector captures every event at the moment it occurs. During a checkpoint write burst:

```
sysfs counter delta:  +47 CE  (appears as one jump at next scrape)
eBPF event stream:    CE at t+0.2s, t+0.4s, t+0.5s, t+0.6s ... (47 individual events)
```

This lets the correlator identify **which operation** triggered the CEs — and whether the pattern is a normal burst (ignore) or an accelerating failure (alert).
