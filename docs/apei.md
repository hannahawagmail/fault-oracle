# ACPI APEI Integration

## Overview

ACPI APEI (Advanced Platform Error Interface) is the firmware-OS error reporting bridge defined in ACPI 6.4 §18. On SBSA/SBBR-compliant ARM servers, the platform firmware populates APEI structures to communicate hardware errors both during boot (BERT) and at runtime (EINJ, ERST, HEST).

This project integrates two APEI components:

| Component | Acronym | Purpose |
|-----------|---------|---------|
| Boot Error Record Table | BERT | Persists fatal errors from the *previous* boot for OS inspection |
| Error INJection | EINJ | Debugfs interface for injecting synthetic hardware errors for testing |

## BERT — Boot Error Record Table

### How it works

At boot, firmware scans hardware error logs and writes any persistent errors into the BERT region (a physical memory range declared in the BERT ACPI table). The kernel maps this and exposes it at `/sys/firmware/acpi/tables/BERT`. Records use the CPER (Common Platform Error Record) format from UEFI 2.9 Appendix N.

```
Previous boot crash
         │  (firmware survives reset, scans error logs)
         ▼
  BERT region (persistent memory)
         │  (kernel maps at boot)
         ▼
  /sys/firmware/acpi/tables/BERT
         │
         ├── apei/bert-reader.py  →  Prometheus textfile
         └── exporter/collectors/apei.go  →  live Prometheus gauge
```

### Sysfs interface

```bash
# Check if BERT is available
ls /sys/firmware/acpi/tables/BERT

# Hexdump first 48 bytes (ACPI header + BERT body)
xxd /sys/firmware/acpi/tables/BERT | head -3

# Read all BERT records
sudo python3 apei/bert-reader.py --output text
sudo python3 apei/bert-reader.py --output json | jq .
```

### CPER Record Structure

```
Offset  Size   Field
0       4      Signature ("CPER")
4       2      Revision
6       4      SignatureEnd (0xFFFFFFFF)
10      2      SectionCount
18      2      SectionCount (offset 18 in our simplified reader)
20      4      RecordLength
24      4      Severity  0=recoverable 1=fatal 2=corrected 3=informational
...
128+    N×72   Section Descriptors
```

### Metrics

```
apei_bert_record_count{severity="corrected"}    0
apei_bert_record_count{severity="fatal"}        1
apei_bert_last_read_timestamp                   1750000000.000
```

Grafana alert: any `severity="fatal"` count > 0 should page immediately — it means the system crashed due to hardware on the previous boot.

## EINJ — Error INJection

EINJ exposes a debugfs interface at `/sys/kernel/debug/apei/einj/` for injecting synthetic hardware errors into the platform. Used for:

- Validating alert pipeline end-to-end
- Testing recovery scripts without needing real hardware failures
- CI chaos scenarios on bare-metal nodes

### Requirements

- `CONFIG_ACPI_APEI_EINJ=y` in kernel config
- `debugfs` mounted at `/sys/kernel/debug`
- Platform firmware support (check `available_error_type`)
- Root privileges

### Usage

```bash
# Check available error types
cat /sys/kernel/debug/apei/einj/available_error_type

# Inject a memory correctable error (dry run first)
sudo ./apei/einj-inject.sh --type mem-correctable --dry-run

# Actually inject
sudo ./apei/einj-inject.sh --type mem-correctable

# PCIe non-fatal at a specific physical address
sudo ./apei/einj-inject.sh --type pcie-non-fatal --addr 0x100000000

# Inject memory UE (will trigger MCE — ensure kdump is configured)
sudo ./apei/einj-inject.sh --type mem-uncorrectable
```

### Error type map

| Flag name | EINJ type mask | Result |
|-----------|---------------|--------|
| `mem-correctable` | 0x01 | EDAC CE counter increment, rasdaemon logs event |
| `mem-uncorrectable` | 0x10 | MCE, possible kernel panic (test with kdump) |
| `mem-fatal` | 0x20 | Guaranteed machine check abort |
| `pcie-correctable` | 0x40 | PCIe AER correctable counter |
| `pcie-non-fatal` | 0x80 | PCIe AER uncorrectable non-fatal |
| `pcie-fatal` | 0x100 | PCIe link down, possible panic |
| `processor` | 0x08 | CPU correctable error |

### Integration with chaos runner

EINJ scripts are referenced from `chaos/scenarios/`:

```yaml
# chaos/scenarios/ce-storm.yaml (excerpt)
inject:
  script: apei/einj-inject.sh
  args: ["--type", "mem-correctable"]
```

## Platform Availability

| Platform | BERT | EINJ |
|----------|------|------|
| AWS Graviton (EC2) | absent | absent |
| Azure Cobalt 100 | absent | absent |
| GCP Axion (C4A) | absent | absent |
| Ampere Altra (bare-metal) | present | present |
| Neoverse N2 (bare-metal) | present | present |
| x86_64 SBSA server | present | present |

APEI absence on cloud VMs is expected — `fault_resilience_collector_up{collector="apei"}=0` is the correct reading there.
