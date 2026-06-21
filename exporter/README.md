# exporter — Hardware Fault Prometheus Exporter

A Go Prometheus exporter that scrapes kernel hardware error counters from sysfs
and exposes them as fleet-queryable Prometheus metrics.

## Architecture

```
/sys/devices/system/edac/      ──► EDACCollector  ──┐
/sys/firmware/acpi/errors/     ──► MCECollector   ──┼──► HTTP /metrics (Prometheus text)
/sys/bus/pci/devices/*/aer_*   ──► AERCollector   ──┘
```

Three independent `prometheus.Collector` implementations scrape their respective
sysfs paths on each Prometheus scrape. Each collector:
- Emits `Counter`-type metrics (cumulative since boot — correct for hardware event counts)
- Reports its own scrape duration and error count for exporter health monitoring
- Handles missing sysfs paths gracefully (returns 0 with a warning, not a crash)

## Metrics Reference

### EDAC Metrics

| Metric | Labels | Description |
|--------|--------|-------------|
| `edac_correctable_errors_total` | `controller`, `csrow`, `channel` | CE errors per channel |
| `edac_uncorrectable_errors_total` | `controller`, `csrow` | UE errors per csrow |
| `edac_controller_ce_total` | `controller` | Controller-level CE total |
| `edac_controller_ue_total` | `controller` | Controller-level UE total |

### MCE Metrics

| Metric | Labels | Description |
|--------|--------|-------------|
| `mce_events_total` | `severity`, `bank`, `source` | Machine check events |
| `mce_available` | — | 1 if MCE data source found |

### PCIe AER Metrics

| Metric | Labels | Description |
|--------|--------|-------------|
| `pcie_aer_correctable_total` | `device`, `error_type` | Correctable AER errors |
| `pcie_aer_uncorrectable_total` | `device`, `error_type`, `severity` | Uncorrectable AER errors |
| `pcie_aer_devices_total` | — | AER-capable device count |

### Exporter Health Metrics

| Metric | Labels | Description |
|--------|--------|-------------|
| `hw_fault_exporter_build_info` | `version` | Always 1; use for version queries |
| `hw_fault_exporter_uptime_seconds` | — | Exporter uptime |
| `hw_fault_exporter_scrape_duration_seconds` | `collector` | Per-collector scrape time |
| `hw_fault_exporter_scrape_errors_total` | `collector` | Per-collector sysfs errors |

## Build and Run

```bash
# Build
cd exporter/
go build -o hw-fault-exporter .

# Run (requires /sys access — typically root or privileged container)
sudo ./hw-fault-exporter --listen-addr :9101 --sysfs-root /sys

# Test against a mock sysfs tree (no hardware required):
./hw-fault-exporter --listen-addr :9101 --sysfs-root ../tests/fixtures/mock-sysfs

# Docker
docker build -t hw-fault-exporter .
docker run --privileged -v /sys:/sys:ro -p 9101:9101 hw-fault-exporter
```

## Prometheus Configuration

```yaml
# prometheus.yml
scrape_configs:
  - job_name: hw_fault
    static_configs:
      - targets: ['localhost:9101']
    scrape_interval: 15s
    scrape_timeout: 10s
```

## Example Queries

```promql
# CE rate per controller over 5m
rate(edac_controller_ce_total[5m])

# Hosts with any UE in the last hour
increase(edac_controller_ue_total[1h]) > 0

# PCIe BadTLP rate — rising rate indicates link degradation
rate(pcie_aer_correctable_total{error_type="BadTLP"}[5m])

# Alert: any UE in the last 15 minutes
increase(edac_uncorrectable_errors_total[15m]) > 0
```
