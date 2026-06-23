<!-- SPDX-License-Identifier: Apache-2.0 -->
# Runbook — ARM Linux Hardware Fault Resilience

This runbook accompanies the Prometheus alert rules in
`deploy/alerts/fault-resilience-rules.yaml`. Each section describes
the alert, its significance, and the investigation steps.

---

## EDACCorrectableStorm

**Severity:** warning  
**Trigger:** CE rate > 10/min for 2 minutes on any controller/csrow/channel.

Correctable ECC errors (CEs) indicate single-bit memory errors that the hardware
corrected automatically. Isolated CEs are normal. A sustained storm suggests DIMM
degradation or a thermal/voltage issue.

**Investigation steps:**

1. Confirm the alert with the current CE rate:
   ```promql
   rate(edac_correctable_errors_total[5m]) * 60
   ```
2. Identify the affected DIMM slot using the DIMM mapper:
   ```bash
   python3 tools/dimm_slot_mapper.py --controller mc0 --csrow 1 --channel 0
   ```
3. Check the kernel log for EDAC messages:
   ```bash
   journalctl -k --since "1 hour ago" | grep -i "EDAC\|ECC"
   ```
4. Run a memory scrub (requires root):
   ```bash
   echo 1 > /sys/devices/system/edac/mc0/sdram_scrub_rate
   ```
5. If the rate does not decrease after 30 minutes, schedule replacement of the
   identified DIMM in the next maintenance window.
6. If the rate exceeds 100/min or persists > 4 hours, escalate to
   [EDACUncorrectableError](#edacuncorrectableerror) protocol.

---

## EDACUncorrectableError

**Severity:** critical  
**Trigger:** Any UE detected (immediate, no `for` delay).

An uncorrectable ECC error (UE) means the hardware could not correct a multi-bit
memory error. Data served from that memory region may be corrupt. Treat as a
potential data integrity incident.

**Investigation steps:**

1. Check whether the system has already panicked (kernel oops or MCE panic):
   ```bash
   journalctl -k -p err..emerg --since "30 min ago"
   dmesg | grep -i "Machine check\|UE\|uncorrectable"
   ```
2. Identify the DIMM slot:
   ```bash
   python3 tools/dimm_slot_mapper.py --controller mc0 --csrow 0
   ```
3. Check for in-memory data corruption indicators:
   - Application crash logs
   - Filesystem errors (`dmesg | grep "EXT4-fs error\|BTRFS error"`)
   - Failed checksums in critical services
4. If workloads are sensitive (database, key-value store), consider:
   - Evacuating the node from serving traffic
   - Triggering a controlled failover
5. Schedule an emergency DIMM replacement. Do not defer beyond the next
   maintenance window.
6. After replacement, run `make inject-ce BACKEND=none` (dry-run) to verify
   the injection path still works on the repaired system.

---

## CollectorDown

**Severity:** warning (EDAC), info (AER, MCE)  
**Trigger:** `fault_resilience_collector_up{collector="..."} == 0` for 5 minutes.

The exporter cannot access the subsystem's sysfs paths. Hardware error
counters for this subsystem are not being collected.

**Investigation steps by collector:**

### EDAC collector

1. Verify the EDAC module is loaded:
   ```bash
   lsmod | grep edac
   ls /sys/devices/system/edac/mc/
   ```
2. If the directory is missing, the EDAC driver for your memory controller
   may not be loaded. Check `dmesg | grep edac` for driver probe messages.
3. Load the appropriate driver, e.g. for Marvell Octeon TX2:
   ```bash
   modprobe thunderx_edac
   ```
4. Verify the exporter can now reach the path:
   ```bash
   hw-fault-exporter --once | grep collector_up
   ```

### AER collector

This is expected on embedded SoCs without PCIe. If PCIe devices are present:

1. Check PCIe AER is enabled in the kernel:
   ```bash
   grep CONFIG_PCIEAER /boot/config-$(uname -r)
   ```
2. Verify AER sysfs nodes exist:
   ```bash
   ls /sys/bus/pci/devices/*/aer_dev_correctable 2>/dev/null | head -5
   ```

### MCE collector

Expected on platforms without ACPI APEI (e.g. Raspberry Pi, many SBCs).
On Ampere Altra or NXP i.MX8MP with ACPI:

1. Check APEI GHES is enabled:
   ```bash
   ls /sys/firmware/acpi/errors/
   dmesg | grep APEI
   ```
2. Verify mcelog or GHES is running if on an x86 mixed fleet.

---

## ExporterDown

**Severity:** critical  
**Trigger:** `up{job="hw-fault-exporter"} == 0` for 2 minutes.

The Prometheus scrape target for hw-fault-exporter is unreachable. Hardware
fault data is not being collected.

**Investigation steps:**

1. Check the exporter process:
   ```bash
   systemctl status hw-fault-exporter
   # or, if running as a container:
   docker ps | grep hw-fault-exporter
   kubectl -n monitoring get pods | grep hw-fault-exporter
   ```
2. Check the port:
   ```bash
   curl -sf http://localhost:9101/healthz && echo OK
   ```
3. Review logs:
   ```bash
   journalctl -u hw-fault-exporter --since "10 min ago"
   ```
4. Restart if the process has crashed:
   ```bash
   systemctl restart hw-fault-exporter
   ```
5. If port 9101 is in use by another process, update `--listen-addr` and
   the Prometheus scrape config `deploy/prometheus/scrape_configs.yaml`.

---

## PCIeFatalError

**Severity:** critical  
**Trigger:** Any PCIe fatal AER error detected.

A PCIe fatal error means the link or endpoint entered an unrecoverable state.
The device driver may have been reset or detached.

**Investigation steps:**

1. Identify the affected device:
   ```bash
   dmesg | grep -i "AER\|PCIe" | tail -30
   lspci -vv -s <BDF>   # BDF from the alert label
   ```
2. Check whether the device is still functional:
   ```bash
   ls /sys/bus/pci/devices/<BDF>/
   cat /sys/bus/pci/devices/<BDF>/broken_parity_status
   ```
3. Attempt FLR (Function Level Reset) if the driver supports it:
   ```bash
   echo 1 > /sys/bus/pci/devices/<BDF>/reset
   ```
4. If the device is a NIC, check network connectivity:
   ```bash
   ip link show
   ethtool <iface>
   ```
5. If recovery fails, the device may require physical replacement or a
   system reboot to restore the PCIe link.

---

## PCIeNonFatalErrorRate

**Severity:** warning  
**Trigger:** Non-fatal PCIe AER errors > 1/min for 5 minutes.

Non-fatal errors are correctable at the link layer (e.g. bad TLP, receiver
error). A sustained rate suggests a marginal cable, connector, or slot.

**Investigation steps:**

1. Check error type breakdown:
   ```promql
   rate(pcie_aer_uncorrectable_total{severity="nonfatal"}[10m]) * 60
   ```
2. Check the specific error type from the label and cross-reference with the
   PCIe AER error register definitions in `docs/aer-primer.md`.
3. Inspect the physical connector/cable for the affected BDF.
4. Try reseating the card if accessible.
5. Monitor for 24 hours after reseating. If the rate does not drop to zero,
   schedule replacement.

---

## General Diagnostic Commands

```bash
# One-shot metrics dump (no running exporter needed)
hw-fault-exporter --once

# Parse a kernel log for EDAC events
python3 replay/parse_edac_trace.py --input /var/log/kern.log --pretty | jq .

# Replay a trace at 10x speed to generate synthetic Prometheus data
make replay-fast

# Run the full test suite
make test-all

# Check all collectors are up
curl -s http://localhost:9101/metrics | grep collector_up
```

---

## Automated Remediation Failures

**Severity:** high  
**Trigger:** Remediation webhook returns error or node drain times out.

**Investigation steps:**

1. Check remediation controller logs:
   ```bash
   kubectl logs -l app=remediation-controller -n monitoring
   ```
2. Check if the target node is cordoned:
   ```bash
   kubectl get node <node> -o jsonpath='{.spec.unschedulable}'
   ```
3. Verify webhook endpoint is reachable:
   ```bash
   curl -s http://remediation-controller:8080/healthz
   ```
4. Check Kubernetes API permissions:
   ```bash
   kubectl auth can-i cordon nodes --as=system:serviceaccount:monitoring:remediation-controller
   ```
5. Review the cooldown state file:
   ```bash
   kubectl exec <pod> -- cat /tmp/remediation-state.json
   ```

**Resolution:**

- If drain timeout: increase `--drain-timeout` or check for PDBs blocking eviction.
- If API 403: update the ClusterRole with `nodes` patch permission.
- If webhook unreachable: restart the remediation-controller deployment.

---

## CE → UE Progression Decision Framework

**Severity:** critical (preemptive action required)  
**Trigger:** `failure_probability_7d > 0.8` OR CE rate > 100/hour on a single DIMM.

### Decision Matrix

| CE Rate | Probability | Page-offlined | Action |
|---------|------------|---------------|--------|
| < 10/hr | < 0.5 | No | Monitor |
| 10-100/hr | 0.5-0.8 | No | Schedule replacement in next maintenance window |
| > 100/hr | > 0.8 | Yes | Immediate drain + replace |
| Any | Any | UE occurred | Emergency: isolate node immediately |

**Investigation steps:**

1. Identify affected DIMM:
   ```promql
   edac_controller_ce_total{controller="mc0",csrow="X",channel="Y"}
   ```
2. Map to physical slot:
   ```bash
   ipmitool sdr | grep -i dimm
   ```
   Or check SMBIOS data.
3. Check page-offline status:
   ```bash
   cat /proc/pagehardware
   ```
4. If replacement needed: cordon node → drain workloads → notify DC ops.

---

## Escalation Matrix

| Severity | Response Time | Responder | Communication |
|----------|--------------|-----------|---------------|
| Critical (UE, node down) | 15 min | On-call SRE | PagerDuty + Slack #incidents |
| High (CE storm, drain fail) | 1 hour | Platform team | Slack #hw-faults |
| Medium (single CE, collector down) | 4 hours | Platform team | Jira ticket |
| Low (threshold warning) | Next business day | HW ops | Email notification |