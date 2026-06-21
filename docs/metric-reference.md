# Metric Reference
Generated: 2026-06-21

Every metric emitted by the fault-oracle system, grouped by subsystem.
Metrics marked ⚠ appear in alert rules and must be present for alerting to function.

Sources: Go exporter (`exporter/collectors/*.go`), Python textfile collectors (`ml/`, `anomaly/`, `aging/`, `bmc/`, `gpu/`, `network/`, `storage/`, `power_cxl/`), and alert rule `expr:` fields (`deploy/alerts/*.yaml`).

---

## EDAC / Memory (Go exporter)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| ⚠ `edac_correctable_errors_total` | counter | `controller, csrow, channel` | Cumulative correctable ECC errors per channel since boot. Source: `/sys/devices/system/edac/mc<N>/csrow<M>/ch<K>_ce_count` |
| ⚠ `edac_uncorrectable_errors_total` | counter | `controller, csrow` | Cumulative uncorrectable ECC errors per csrow since boot. Source: `/sys/devices/system/edac/mc<N>/csrow<M>/ue_count` |
| `edac_controller_ce_total` | counter | `controller` | Total correctable errors aggregated at the memory controller level. Useful for fleet-level alerting without summing csrow/channel. |
| `edac_controller_ue_total` | counter | `controller` | Total uncorrectable errors aggregated at the memory controller level. |

---

## eBPF Tracepoints (Go exporter)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| ⚠ `ebpf_edac_ce_total` | counter | `mc, top_layer, mid_layer` | EDAC correctable errors captured via eBPF `ras:mc_event` tracepoint. Zero-latency vs. sysfs polling. |
| `ebpf_edac_ue_total` | counter | `mc, top_layer, mid_layer` | EDAC uncorrectable errors captured via eBPF tracepoint. |
| `ebpf_aer_total` | counter | `severity` | PCIe AER events captured via eBPF `ras:aer_event` tracepoint. Severity: `correctable`, `uncorrectable`, `fatal`. |
| `ebpf_mce_total` | counter | `severity` | Machine check exception events captured via eBPF `ras:mce_record` tracepoint. |

---

## APEI / BERT (Go exporter)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `apei_bert_record_count` | gauge | `severity` | Number of CPER records in the ACPI BERT boot error region. Severity: `recoverable`, `fatal`, `corrected`, `informational`. Emits 0 when BERT is absent (normal on cloud VMs). |

---

## PCIe AER (Go exporter)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| ⚠ `pcie_aer_correctable_total` | counter | `device, error_type` | Cumulative correctable PCIe AER errors per device (BDF) and error type. Source: `/sys/bus/pci/devices/<BDF>/aer_dev_correctable`. |
| ⚠ `pcie_aer_uncorrectable_total` | counter | `device, error_type, severity` | Cumulative uncorrectable AER errors. Severity: `nonfatal` or `fatal`. Source: `aer_dev_nonfatal` and `aer_dev_fatal`. |
| `pcie_aer_devices_total` | gauge | _(none)_ | Number of PCIe devices with AER capability visible in sysfs. |

---

## MCE / Machine Check Events (Go exporter)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `mce_events_total` | counter | `severity, bank, source` | Total machine check events by severity (`corrected`, `deferred`, `uncorrected`, `panic`), bank ID, and source (`ghes`, `mcelog`, `rasdaemon`). |
| `mce_available` | gauge | _(none)_ | 1 if an MCE data source was found (GHES, mcelog, or rasdaemon counters), 0 otherwise. |

---

## CXL Memory (Go exporter)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| ⚠ `cxl_correctable_errors_total` | counter | `device, error_type` | Total CXL correctable memory errors. `error_type`: `volatile_data`, `persistent_data`. Source: `/sys/bus/cxl/devices/mem<N>/error_counts/`. Requires kernel ≥ 6.2. |
| ⚠ `cxl_uncorrectable_errors_total` | counter | `device, error_type` | Total CXL uncorrectable memory errors. `error_type`: `volatile_data`, `volatile_no_addr`, `persistent_data`, `persistent_no_addr`. |

---

## Thermal (Go exporter)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| ⚠ `cpu_thermal_zone_celsius` | gauge | `zone, zone_type` | Current temperature of a thermal zone in degrees Celsius. Source: `/sys/class/thermal/thermal_zone<N>/temp` (millidegrees, converted). |
| `cpu_cooling_device_state` | gauge | `device, device_type` | Current state of a cooling device (0 = off / minimum cooling). Source: `/sys/class/thermal/cooling_device<N>/cur_state`. |

---

## SmartNIC / NIC Statistics (Go exporter)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `smartnic_rx_bytes_total` | counter | `iface` | Total bytes received per network interface. |
| `smartnic_tx_bytes_total` | counter | `iface` | Total bytes transmitted per network interface. |
| `smartnic_rx_errors_total` | counter | `iface` | Total receive errors per interface. |
| `smartnic_tx_errors_total` | counter | `iface` | Total transmit errors per interface. |
| `smartnic_rx_dropped_total` | counter | `iface` | Total receive packets dropped per interface. |
| `smartnic_tx_dropped_total` | counter | `iface` | Total transmit packets dropped per interface. |
| `smartnic_rx_packets_total` | counter | `iface` | Total packets received per interface. |
| `smartnic_tx_packets_total` | counter | `iface` | Total packets transmitted per interface. |
| `smartnic_rx_missed_errors_total` | counter | `iface` | Total receive missed errors per interface. |
| `smartnic_collisions_total` | counter | `iface` | Total transmit collisions per interface. |
| `smartnic_link_up` | gauge | `iface, operstate` | 1 if interface operstate is `up`, 0 otherwise. |
| `smartnic_link_speed_mbps` | gauge | `iface` | Interface speed in Mbps (0 if unknown or link down). |

---

## ARM PMU / Memory Bandwidth (Go exporter)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `memory_bandwidth_read_bytes_total` | counter | `pmu_type` | Cumulative memory read bytes from DRAM as measured by ARM CMN or DSU PMU. |
| `memory_bandwidth_write_bytes_total` | counter | `pmu_type` | Cumulative memory write bytes to DRAM as measured by ARM PMU. |

---

## CPU Frequency / Governor (Go exporter)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `cpu_frequency_hz` | gauge | `cpu` | Current CPU frequency in Hz. Source: `/sys/devices/system/cpu/cpu<N>/cpufreq/scaling_cur_freq`. |
| `cpu_frequency_min_hz` | gauge | `cpu` | Minimum allowed CPU frequency (policy floor) in Hz. |
| `cpu_frequency_max_hz` | gauge | `cpu` | Maximum allowed CPU frequency (policy ceiling) in Hz. |
| `cpu_governor` | gauge | `cpu, governor` | 1 if the specified governor is active on this CPU, 0 otherwise. |

---

## Exporter Self-Health (Go exporter)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `hw_fault_exporter_build_info` | gauge | `version` | Build information for the hardware fault exporter. Value is always 1; use `version` label for versioning queries. |
| `hw_fault_exporter_uptime_seconds` | gauge | _(none)_ | Seconds since the hardware fault exporter process started. |
| ⚠ `hw_fault_exporter_scrape_duration_seconds` | gauge | `collector` | Time taken to complete one scrape cycle for the named collector. Alert threshold: > 5s. |
| `hw_fault_exporter_scrape_errors_total` | counter | `collector` | Total sysfs read errors encountered by the named collector during scrape. |

---

## Collector Health (collector_up metrics)

All collectors emit `fault_resilience_collector_up{collector=<name>}` = 1 when the hardware subsystem is accessible, 0 when absent or inaccessible.

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| ⚠ `fault_resilience_collector_up` | gauge | `collector` | 1 if the collector is running and producing metrics, 0 if disabled or hardware absent. `collector` values: `edac`, `aer`, `mce`, `apei`, `cxl`, `thermal`, `smartnic`, `pmu`, `cpufreq`, `ebpf_edac`. |

---

## BMC / IPMI (Python textfile collector)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| ⚠ `ipmi_sel_event_total` | counter | `node, severity` | Total IPMI SEL events observed per node and severity. |
| `ipmi_sel_last_event_timestamp` | gauge | `node` | Unix timestamp of the most recent SEL event. |
| `ipmi_sel_last_record_id` | gauge | `node` | Last SEL record ID seen per node. Used for incremental polling. |
| `ipmi_bmc_poll_error_total` | counter | `node` | Total ipmitool poll errors per node. |
| `ipmi_bmc_collector_up` | gauge | `node` | 1 if the BMC was reachable on the last poll, 0 otherwise. |
| `ipmi_bmc_collector_last_run_timestamp` | gauge | `node` | Unix timestamp of the last IPMI SEL collector run. |
| ⚠ `ipmi_sensor_value` | gauge | `node, sensor, sensor_type` | Numeric sensor reading (temperature, voltage, fan RPM, etc.) from `ipmitool sensor`. |
| ⚠ `ipmi_sensor_threshold_breach` | gauge | `node, sensor` | 1 if the sensor is at or beyond the Upper/Lower Critical threshold, 0 otherwise. |
| `ipmi_sensor_collector_last_run_timestamp` | gauge | `node` | Unix timestamp of last sensor collector run. |

---

## BMC / Redfish (Python textfile collector)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| ⚠ `redfish_system_health` | gauge | `node, component` | Redfish system component health: 1=OK, 0=Warning, −1=Critical. |
| ⚠ `redfish_psu_up` | gauge | `node, psu` | 1 if the PSU State is Enabled, 0 otherwise. |
| ⚠ `redfish_temperature_celsius` | gauge | `node, sensor` | Temperature reading in Celsius from Redfish Thermal subsystem. |
| `redfish_fan_rpm` | gauge | `node, fan` | Fan speed in RPM from Redfish Thermal. |
| `redfish_psu_input_watts` | gauge | `node, psu` | PSU input power in Watts from Redfish Power. |
| `redfish_sel_entry_total` | counter | `node, severity, category` | Total new Redfish SEL entries by severity and category. |
| `redfish_collector_up` | gauge | `node` | 1 if Redfish collector ran successfully, 0 otherwise. |
| `redfish_collector_last_run_timestamp` | gauge | `node` | Unix timestamp of last Redfish collector run. |

---

## Network / InfiniBand (Python textfile collector)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| ⚠ `ib_port_state` | gauge | `device, port` | 1 if InfiniBand port state is Active, 0 otherwise. |
| ⚠ `ib_port_error_total` | counter | `device, port, counter` | InfiniBand port error counter totals (SymbolErrorCounter, LinkErrorRecoveryCounter, etc.). |
| `ib_port_link_rate_gbps` | gauge | `device, port` | InfiniBand port link rate in Gbps. |
| `ib_collector_up` | gauge | _(none)_ | 1 if InfiniBand hardware detected and collector operational, 0 otherwise. |
| `ib_collector_last_run_timestamp` | gauge | _(none)_ | Unix timestamp of the last IB collector run. |

---

## Network / NIC + RoCE (Python textfile collector)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| ⚠ `nic_errors_total` | counter | `iface, direction` | NIC error counters from sysfs statistics. `direction`: `rx`, `tx`. |
| ⚠ `roce_errors_total` | counter | `iface, counter` | RoCE-specific error counters from ethtool (e.g. `out_of_buffer`, `out_of_sequence`). |
| `nic_collector_up` | gauge | _(none)_ | 1 if physical NIC interfaces found and collector operational, 0 otherwise. |
| `nic_collector_last_run_timestamp` | gauge | _(none)_ | Unix timestamp of the last NIC collector run. |

---

## Power / RAPL (Python textfile collector)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `rapl_package_power_watts` | gauge | `package` | Instantaneous CPU package power draw in watts (RAPL energy counter delta). |
| `rapl_power_limit_watts` | gauge | `package` | CPU package TDP power limit in watts. |
| `rapl_throttled` | gauge | `package` | 1 if package power exceeds 95% of the TDP limit (indicates thermal/power throttling). |
| `cpu_freq_throttle_ratio` | gauge | `cpu` | Ratio of current to maximum CPU scaling frequency. Values < 1.0 indicate throttling. |
| `rapl_collector_up` | gauge | _(none)_ | 1 if RAPL or cpufreq data was collected successfully. |
| `rapl_collector_last_run_timestamp` | gauge | _(none)_ | Unix timestamp of last RAPL collector run. |

---

## Power / PMEM (Python textfile collector)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `pmem_lifespan_remaining_percent` | gauge | `device` | PMEM device lifespan remaining as percentage. |
| `pmem_lifespan_used_percent` | gauge | `device` | PMEM device lifespan consumed as percentage. |
| `pmem_unsafe_shutdowns_total` | counter | `device` | PMEM cumulative unsafe shutdown count (from ndctl). |
| `pmem_media_errors_total` | counter | `device` | Number of media-error regions reported by ndctl for this PMEM device. |
| `pmem_temperature_celsius` | gauge | `device` | PMEM device temperature in Celsius. |
| `pmem_health_state` | gauge | `device, state` | 1 if the PMEM device is in the given health state (normal, non_critical, critical, fatal). |
| `pmem_ars_in_progress` | gauge | `device` | 1 if an address-range-scrub scan is currently in progress on this PMEM device. |
| `pmem_collector_up` | gauge | _(none)_ | 1 if ndctl is present and PMEM devices were found. |
| `pmem_collector_last_run_timestamp` | gauge | _(none)_ | Unix timestamp of last PMEM collector run. |

---

## Power / Misc Hardware (Python textfile collector)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `hwrng_active` | gauge | _(none)_ | 1 if a hardware RNG device (`/dev/hwrng`) is active. |
| `watchdog_present` | gauge | _(none)_ | 1 if `/dev/watchdog` device node is present. |
| `pcie_aer_correctable_total` | counter | `device, error_type` | PCIe AER correctable total (also emitted by misc collector for cross-check). |
| `pcie_aer_fatal_total` | counter | `device, error_type` | PCIe AER fatal error total (subset of uncorrectable). |
| `misc_collector_up` | gauge | _(none)_ | 1 when the misc hardware collector ran successfully. |
| `misc_collector_last_run_timestamp` | gauge | _(none)_ | Unix timestamp of last misc collector run. |

---

## GPU (NVIDIA) (Python textfile collector)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| ⚠ `gpu_ecc_sbe_total` | counter | `gpu, uuid` | Total single-bit (corrected) ECC errors per GPU (volatile counter, reset on reboot). |
| ⚠ `gpu_ecc_dbe_total` | counter | `gpu, uuid` | Total double-bit (uncorrected) ECC errors per GPU (volatile). |
| ⚠ `gpu_xid_error_total` | counter | `gpu, xid` | Total NVIDIA GPU XID error events observed (from nvidia-smi xid log). |
| ⚠ `gpu_throttle_active` | gauge | `gpu, reason` | 1 if the given throttle reason is active on the GPU (thermal_slowdown, power_limit, etc.). |
| `gpu_temperature_celsius` | gauge | `gpu` | Current GPU die temperature in degrees Celsius. |
| `gpu_power_watts` | gauge | `gpu` | Current GPU power draw in watts. |
| `gpu_clock_mhz` | gauge | `gpu` | Current GPU clock frequency in MHz. |
| `gpu_failure_probability_7d` | gauge | `gpu, uuid` | Estimated probability of GPU failure within 7 days (ML predictor). |
| `gpu_forecast_next_24h_dbe_rate` | gauge | `gpu, uuid` | Predicted mean DBE rate over the next 24 hours (ML predictor). |
| `gpu_collector_up` | gauge | _(none)_ | 1 if NVIDIA GPU detected and collector operational, 0 otherwise. |
| `gpu_predictor_up` | gauge | _(none)_ | 1 if GPU failure predictor can reach Prometheus and is operational. |
| `gpu_predictor_last_run_timestamp` | gauge | _(none)_ | Unix timestamp of the last GPU predictor run. |
| `gpu_throttle_collector_up` | gauge | _(none)_ | 1 if NVIDIA GPU detected and throttle collector operational. |
| `gpu_throttle_collector_last_run_timestamp` | gauge | _(none)_ | Unix timestamp of the last throttle collector run. |
| `nvidia_gpu_collector_last_run_timestamp` | gauge | _(none)_ | Unix timestamp of the last XID collector run. |

---

## GPU (AMD) (Python textfile collector)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| ⚠ `amd_gpu_ras_error_total` | counter | `gpu, block, type` | Total AMD GPU RAS errors per memory block and error type (correctable/uncorrectable). |
| `amd_gpu_utilization_percent` | gauge | `gpu` | Current GPU utilisation as a percentage (from rocm-smi). |
| `amd_gpu_vram_total_bytes` | gauge | `gpu` | Total VRAM available in bytes. |
| `amd_gpu_vram_used_bytes` | gauge | `gpu` | Current VRAM in use in bytes. |
| `amd_collector_up` | gauge | _(none)_ | 1 if AMD GPU detected and collector operational, 0 otherwise. |
| `amd_gpu_collector_up` | gauge | _(none)_ | 1 if AMD GPU detected (alias used by some dashboard panels). |
| `amd_collector_last_run_timestamp` | gauge | _(none)_ | Unix timestamp of the last AMD collector run. |

---

## Storage / NVMe (Python textfile collector)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `nvme_smart_critical_warning` | gauge | `device` | NVMe SMART critical warning bitmask. Non-zero indicates active warnings. |
| `nvme_smart_temperature_celsius` | gauge | `device` | NVMe drive temperature in Celsius. |
| `nvme_smart_available_spare_percent` | gauge | `device` | NVMe available spare capacity as percentage. |
| `nvme_smart_available_spare_threshold_percent` | gauge | `device` | NVMe available spare threshold below which spare is critically low. |
| `nvme_smart_percentage_used` | gauge | `device` | NVMe drive wear percentage (100 = fully worn, vendor endurance exhausted). |
| `nvme_smart_power_on_hours_total` | counter | `device` | NVMe cumulative power-on hours. |
| `nvme_smart_unsafe_shutdowns_total` | counter | `device` | NVMe cumulative unsafe shutdown count. |
| `nvme_smart_media_errors_total` | counter | `device` | NVMe cumulative media and data integrity errors. |
| `nvme_smart_data_units_written_total` | counter | `device` | NVMe cumulative data units written (in 512-byte units × 1000). |
| `nvme_error_log_entries_total` | counter | `device` | NVMe error log entry count (persistent across power cycles). |
| `nvme_collector_up` | gauge | _(none)_ | 1 if NVMe devices were found and nvme-cli is present. |
| `nvme_collector_last_run_timestamp` | gauge | _(none)_ | Unix timestamp of last NVMe collector run. |

---

## Storage / SATA (Python textfile collector)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `sata_smart_passed` | gauge | `device` | SATA/SAS SMART overall health assessment: 1=PASSED, 0=FAILED. |
| `sata_smart_attribute` | gauge | `device, attribute_id, attribute_name` | SATA/SAS SMART attribute raw value (e.g. Reallocated_Sector_Ct, Pending_Sector). |
| `sata_rotational` | gauge | `device` | 1 if the device is a spinning disk (HDD), 0 for SSD. |
| `sata_collector_up` | gauge | _(none)_ | 1 if SATA devices found and smartctl is present. |
| `sata_collector_last_run_timestamp` | gauge | _(none)_ | Unix timestamp of last SATA collector run. |

---

## Storage Controller (Python textfile collector)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `storage_controller_failed_disks` | gauge | `controller` | Number of failed disks reported by the RAID/HBA controller. |
| `storage_controller_critical_disks` | gauge | `controller` | Number of critical/degraded disks reported by the controller. |
| `storage_controller_ecc_errors_total` | counter | `controller` | Cumulative ECC memory errors on the controller's onboard cache. |
| `storage_controller_temperature_celsius` | gauge | `controller` | RAID/HBA controller temperature in Celsius. |
| `storage_controller_up` | gauge | `controller` | 1 if RAID/HBA driver is loaded and devices are present. |
| `storage_controller_last_run_timestamp` | gauge | _(none)_ | Unix timestamp of last storage controller collector run. |

---

## Storage Wear Predictor (Python textfile collector)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `storage_wear_probability_90d` | gauge | `device` | Estimated probability of storage device failure within 90 days based on SMART trends. |
| `storage_wear_rate_percent_per_day` | gauge | `device` | Current wear rate in percentage points per day. |
| `storage_wear_time_to_failure_days` | gauge | `device` | Projected days until the device reaches 100% wear (based on linear extrapolation). |
| `storage_wear_predictor_insufficient_data` | gauge | `device` | 1 if insufficient SMART history is available to compute a prediction. |
| `storage_wear_predictor_up` | gauge | _(none)_ | 1 if the wear predictor ran successfully. |
| `storage_wear_predictor_last_run_timestamp` | gauge | _(none)_ | Unix timestamp of the last wear predictor run. |

---

## ML / DIMM Failure Prediction (Python textfile collector)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| ⚠ `failure_probability_7d` | gauge | `instance, mc, csrow` | Probability of DIMM failure within 7 days, estimated by Prophet CE rate forecast. Range: [0, 1]. |
| ⚠ `failure_probability_30d` | gauge | `instance, mc, csrow` | Probability of DIMM failure within 30 days. |
| `dimm_risk_tier` | gauge | `instance, mc, csrow, tier` | 1 for the active risk tier, 0 for inactive. Tiers: `critical` (≥0.8), `high` (≥0.5), `medium` (≥0.2), `low` (<0.2). |
| `ce_forecast_next_24h` | gauge | `instance, mc, csrow` | Expected total correctable error count over the next 24 hours (CE rate × 3600s/h × 24h). |
| ⚠ `ml_model_age_hours` | gauge | `instance, mc, csrow` | Hours since this DIMM model was last retrained by ce-forecaster. Alert when > 48h. |
| `ml_models_loaded_total` | gauge | _(none)_ | Number of ML models currently loaded by the probability exporter. |
| `ml_exporter_last_run_timestamp` | gauge | _(none)_ | Unix timestamp of the last probability exporter run. |

---

## ML / DIMM Survival Analysis (Python textfile collector)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| ⚠ `dimm_survival_probability` | gauge | `cohort, days` | Kaplan-Meier survival probability at the given day threshold for the given DIMM cohort. Used in fleet-level proactive replacement decisions. |
| `dimm_cohort_size` | gauge | `cohort` | Number of DIMMs in each survival analysis cohort. |
| `dimm_median_lifetime_days` | gauge | `cohort` | Estimated median DIMM lifetime in days derived from the KM survival curve. |
| `survival_analysis_last_run_timestamp` | gauge | _(none)_ | Unix timestamp of the last survival analysis run. |

---

## ML / DIMM Aging (Python textfile collector)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `dimm_aging_ce_rate_slope` | gauge | `mc, csrow` | Linear regression slope of CE rate in errors/day. Positive slope indicates accelerating degradation. |
| `dimm_aging_projected_days_to_threshold` | gauge | `mc, csrow` | Projected days until CE rate exceeds the warn threshold (default 10 CE/day). Encoded as 1e308 for healthy DIMMs with zero or negative slope. |
| `dimm_aging_health_score` | gauge | `mc, csrow` | DIMM health score: 1.0 = new/healthy, 0.0 = at or above critical threshold (100 CE/day). |
| `dimm_aging_r_squared` | gauge | `mc, csrow` | R² coefficient of the CE rate linear regression. Values near 1.0 indicate a reliable trend; low values suggest noisy or non-linear degradation. |
| `dimm_aging_report_timestamp` | gauge | _(none)_ | Unix timestamp of the last aging report generation. |

---

## Anomaly Detection (Python textfile collector)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| ⚠ `anomaly_zscore` | gauge | `instance, collector` | Z-score of the current error rate vs. the 7-day baseline. Z = (current − mean) / stddev. |
| ⚠ `anomaly_detected` | gauge | `instance, collector` | 1 if |Z| exceeds the threshold (default 3.0, ≈0.3% Gaussian false-positive rate), 0 otherwise. |
| `anomaly_baseline_mean` | gauge | `instance, collector` | Baseline mean of the error rate over the 7-day window. |
| `anomaly_baseline_stddev` | gauge | `instance, collector` | Baseline standard deviation of the error rate over the 7-day window. |
| `anomaly_synthetic_stddev` | gauge | `instance, collector` | 1 if stddev was clamped to 1.0 (constant or empty baseline), 0 if real stddev was used. Useful for distinguishing clamped series on dashboards. |
| `anomaly_detector_last_run_timestamp` | gauge | _(none)_ | Unix timestamp of the last anomaly detector run. |

---

## Remediation Controller

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| _(exposed on port 8080)_ | — | — | The remediation controller exposes its own metrics on `:8080/metrics`. Specific metric names are internal to the controller and not currently documented in Python source files. The Prometheus URL and cordon threshold are configurable via environment variables. |

---

## RAS / rasdaemon (Python textfile collector)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `ras_mc_event_total` | counter | `node, severity` | Total rasdaemon memory controller events (from rasdaemon SQLite database). |
| `ras_aer_event_total` | counter | `node, severity` | Total rasdaemon PCIe AER events. |
| `ras_exporter_db_events_total` | counter | `node` | Total events in the rasdaemon database (all types combined). |
| `ras_exporter_last_run_timestamp` | gauge | _(none)_ | Unix timestamp of the last rasdaemon exporter run. |

---

## APEI BERT (Python textfile collector — bert-reader.py)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `apei_bert_record_total` | gauge | `severity` | ACPI BERT boot error records by severity (Python bert-reader.py; complements the Go APEI collector). |

---

## Recording Rules (derived metrics)

These metrics are produced by Prometheus recording rules in `deploy/alerts/recording-rules.yaml` and `deploy/alerts/slo-rules.yaml`. They are not scraped directly but are available for dashboard queries.

| Metric | Description |
|--------|-------------|
| `edac:correctable_errors:rate5m` | 5-minute CE rate per channel |
| `edac:controller_ce:rate5m` | 5-minute CE rate summed per controller |
| `edac:node_ce:rate5m` | 5-minute CE rate summed per node |
| `edac:controller_ce:increase1h` | 1-hour CE increase per controller |
| `edac:node_ce:increase24h` | 24-hour CE increase per node |
| `edac:uncorrectable_errors:increase5m` | 5-minute UE increase per csrow |
| `edac:node_ue:increase24h` | 24-hour UE increase per node |
| `aer:correctable:rate5m` | 5-minute AER correctable rate per device |
| `aer:nonfatal:rate10m` | 10-minute AER non-fatal rate per device |
| `aer:devices_per_node` | Total AER-capable PCIe devices per node |
| `fault_resilience:collectors_up_ratio` | Average collector availability per node |
| `fault_resilience:scrape_duration_p95` | 95th-percentile scrape duration per node |
| `edac:fleet_nodes_with_ce` | Fleet-wide count of nodes reporting any CE |
| `edac:fleet_nodes_in_storm` | Fleet-wide count of nodes in CE storm |
| `edac:fleet_nodes_with_ue_24h` | Fleet-wide count of nodes with UE in last 24h |
| `slo:collector_up:ratio_rate1h` | Collector availability ratio over 1h |
| `slo:collector_up:ratio_rate6h` | Collector availability ratio over 6h |
| `slo:collector_up:ratio_rate1d` | Collector availability ratio over 1d |
| `slo:collector_up:ratio_rate3d` | Collector availability ratio over 3d |
| `slo:collector_up:ratio_rate30d` | Collector availability ratio over 30d |
| `slo:collector_up:burn_rate1h` | SLO error budget burn rate (1h window) |
| `slo:collector_up:burn_rate6h` | SLO error budget burn rate (6h window) |
| `slo:collector_up:burn_rate3d` | SLO error budget burn rate (3d window) |
| `slo:collector_up:error_budget_remaining` | Fraction of 30-day error budget remaining |
| `anomaly:edac_ce_zscore:current` | Current EDAC CE Z-score recording rule |
