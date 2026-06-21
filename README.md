# arm-linux-fault-resilience

A reference implementation and methodology distillation for kernel-level hardware-fault
detection, recovery, and observability on ARM Linux platforms in datacenter-scale deployments.

---

## Why This Exists

> **Outcome 2 — Zero-Downtime AI Infrastructure Resilience: I architect, resolve, and codify
> the kernel-level mechanisms that detect, recover from, and prevent hardware faults across AI
> server fleets, where unhandled rare errors at fleet scale cascade into outages costing between
> one million and five million dollars per hour.**

Hardware faults — bit flips in DRAM, correctable cache errors in ARM processors, uncorrectable
PCIe link errors — are statistically rare on any individual machine. At fleet scale that
calculus inverts. A fleet of several thousand ARM servers each experiencing a correctable
memory error once per week produces hundreds of error events per hour across the fleet. Left
unobserved, correctable errors are a leading indicator of imminent uncorrectable faults; left
unrecovered, uncorrectable faults silently corrupt workloads or panic the kernel mid-job.
The difference between a fleet that handles this gracefully and one that does not is almost
entirely in the kernel-to-observability pipeline built between hardware fault detection and the
on-call engineer's alerting dashboard.

This repository distills the engineering methodology that closes that gap. It demonstrates,
end-to-end: how the Linux kernel's EDAC (Error Detection And Correction) subsystem surfaces
hardware memory errors through a standardized sysfs interface; how PCIe Advanced Error Reporting
(AER) exposes link-level faults; how an in-kernel fault-injection harness exercises recovery
paths in CI so rare-event code is not untested code; how a Prometheus exporter translates
kernel counters into fleet-queryable metrics; and how trace-based replay lets an engineer
reproduce the kernel state of a past fault for postmortem analysis without waiting for the
fault to recur naturally. Every component here is built on mainline Linux kernel infrastructure,
public specifications, and open-source tooling — no proprietary systems required to run or
understand it.

The methodology documented here is grounded in direct kernel work: the Marvell ARMADA pinctrl
drivers (merged into mainline Linux), and the diagnosis of a 2018 softirq accounting regression
credited by Linus Torvalds in commit `3c53776e`. That softirq diagnosis required precisely the
same skill set this repo formalizes — read kernel accounting paths, correlate counters with
subsystem behavior, bisect to the causative commit, and report with a reproducible test case.
Rare-event hardware faults demand the same discipline applied one layer lower in the stack,
at the hardware–software boundary.

---

## Non-Proprietary Scope Statement

All content in this repository is generic reference material. It is built exclusively on:
mainline Linux kernel infrastructure (EDAC, fault injection, PCIe AER), public ARM and PCIe
specifications, and open-source tooling (Go, Prometheus, Grafana, QEMU). Nothing here reflects
any employer's proprietary systems, internal incident records, production configurations,
internal codenames, or non-public metrics. The EDAC reference driver targets a generic ARM
Cortex-A72 platform fully exercisable under QEMU. This repository is intended as a
methodology reference, not as a description of any production deployment.

---

## What's in This Repo

```
arm-linux-fault-resilience/
├── docs/                        Reference documentation
│   ├── hardware-fault-taxonomy.md   Correctable vs. uncorrectable; L1/L2/DRAM/PCIe/MCE
│   ├── edac-framework-primer.md     Linux EDAC subsystem internals for non-kernel readers
│   ├── aer-primer.md                PCIe Advanced Error Reporting deep-dive
│   └── references.md                Upstream specs, kernel docs, and prior art
│
├── edac-reference/              Generic Cortex-A72 EDAC poll driver
│   ├── edac_cortex_ref.c            Kernel module — QEMU-runnable reference implementation
│   ├── Kconfig / Makefile           Build integration
│   └── test/verify_edac_sysfs.sh    Post-load sysfs verification script
│
├── fault-injection/             CI fault-injection harness
│   ├── inject_edac_ce.sh            Trigger correctable-error recovery path
│   ├── inject_edac_ue.sh            Trigger uncorrectable-error recovery path
│   ├── inject_aer.sh                PCIe AER error injection via aer-inject
│   └── ci_fault_matrix.sh           Iterate fault type × recovery path combinations
│
├── exporter/                    Prometheus metrics exporter (Go)
│   ├── main.go                      HTTP server + collector registration
│   ├── collectors/edac.go           Scrapes /sys/devices/system/edac/
│   ├── collectors/mce.go            Scrapes machine-check error counters
│   ├── collectors/aer.go            Scrapes PCIe AER sysfs entries
│   └── Dockerfile                   Container image for fleet deployment
│
├── dashboards/                  Grafana
│   └── fleet-fault-resilience.json  Dashboard: CE/UE rates, AER events, MCE timeline
│
├── replay/                      Postmortem trace replay toolkit
│   ├── parse_edac_trace.py          Parse kernel log / EDAC trace → structured JSON
│   ├── replay_kernel_state.sh       Re-inject parsed events via fault-injection layer
│   └── example_traces/              Synthetic anonymized example traces
│
├── ci/                          GitHub Actions
│   ├── build-and-test.yml           Full pipeline: build → inject → scrape → assert
│   └── qemu-boot.sh                 Boot QEMU ARM64 virt, load module, run tests
│
└── tests/                       Comprehensive test suite (pytest + shell)
    ├── conftest.py                  Shared fixtures and mock sysfs tree
    ├── test_edac_counters.py        Exporter metrics ↔ sysfs value reconciliation
    ├── test_aer_counters.py         AER collector correctness
    ├── test_mce_counters.py         MCE collector correctness
    ├── test_collectors_unit.py      Unit tests for each collector in isolation
    ├── test_fault_injection.py      Injection → counter increment verification
    └── test_replay_parse.py         Parse → replay roundtrip on sample traces
```

---

## Quickstart

### Prerequisites

```bash
# Debian / Ubuntu
sudo apt-get install -y \
    gcc-aarch64-linux-gnu \
    qemu-system-aarch64 \
    linux-headers-$(uname -r) \
    golang-go \
    python3-pytest \
    python3-requests \
    prometheus

# Fedora / RHEL
sudo dnf install -y \
    gcc-aarch64-linux-gnu \
    qemu-system-aarch64 \
    kernel-devel \
    golang \
    python3-pytest \
    python3-requests
```

### Step 1 — Build the EDAC reference kernel module

```bash
cd edac-reference/

# Native build (if running ARM64)
make KERNELDIR=/lib/modules/$(uname -r)/build

# Cross-compile for ARM64 (from x86 host)
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \
     KERNELDIR=/path/to/arm64-kernel-headers
```

### Step 2 — Boot under QEMU and load the module

```bash
# Download a prebuilt ARM64 kernel + initrd (e.g., Ubuntu cloud image for arm64)
# then:
./ci/qemu-boot.sh --kernel Image --initrd initrd.img --module edac-reference/edac_cortex_ref.ko

# Inside the QEMU guest:
sudo insmod /tmp/edac_cortex_ref.ko
sudo ./edac-reference/test/verify_edac_sysfs.sh
```

### Step 3 — Run the fault-injection harness

```bash
# Inject a correctable error and verify counter increment:
sudo ./fault-injection/inject_edac_ce.sh --controller mc0 --csrow 0 --channel 0

# Run the full CI fault matrix (all fault types × all recovery paths):
sudo ./fault-injection/ci_fault_matrix.sh 2>&1 | tee fault-matrix-results.log
```

### Step 4 — Start the Prometheus exporter

```bash
cd exporter/
go build -o hw-fault-exporter .
sudo ./hw-fault-exporter --sysfs-root /sys --listen-addr :9101

# Confirm metrics are available:
curl -s http://localhost:9101/metrics | grep edac_
curl -s http://localhost:9101/metrics | grep pcie_aer_
curl -s http://localhost:9101/metrics | grep mce_
```

### Step 5 — Import the Grafana dashboard

1. Open Grafana → Dashboards → Import
2. Upload `dashboards/fleet-fault-resilience.json`
3. Select your Prometheus data source
4. The dashboard auto-discovers all `edac_`, `pcie_aer_`, and `mce_` metrics

### Step 6 — Run the replay toolkit on a sample trace

```bash
cd replay/

# Parse an EDAC kernel log trace into structured JSON:
python3 parse_edac_trace.py \
    --input example_traces/sample_ce_storm.log \
    --output /tmp/replay_events.json

# Inspect the parsed events:
cat /tmp/replay_events.json | python3 -m json.tool | head -40

# Replay the parsed events through the fault-injection layer:
sudo ./replay_kernel_state.sh --events /tmp/replay_events.json --dry-run
sudo ./replay_kernel_state.sh --events /tmp/replay_events.json
```

### Step 7 — Run the test suite

```bash
cd tests/

# Full pytest suite (uses mock sysfs — no hardware required):
pytest -v --tb=short

# With coverage report:
pytest -v --cov=../exporter --cov-report=term-missing --cov-report=html

# Shell-based sysfs verification (requires loaded module):
bash ../edac-reference/test/verify_edac_sysfs.sh
```

---

## Background and References

### Linux Kernel EDAC Framework

- [Kernel documentation: `Documentation/driver-api/edac.rst`](https://www.kernel.org/doc/html/latest/driver-api/edac.html)
- [EDAC sysfs interface: `Documentation/ABI/testing/sysfs-devices-edac`](https://www.kernel.org/doc/html/latest/admin-guide/edac.html)
- [`drivers/edac/` in the kernel source tree](https://elixir.bootlin.com/linux/latest/source/drivers/edac)

### ARM RAS Architecture

- [ARM Reliability, Availability, and Serviceability (RAS) Extension — Architecture Reference Manual](https://developer.arm.com/documentation/ddi0597/latest)
- [ARM Cortex-A72 Technical Reference Manual](https://developer.arm.com/documentation/100095/latest)
- [ACPI Platform Error Interface (APEI) — the kernel's generic hardware error reporting path for ARM servers](https://www.kernel.org/doc/html/latest/firmware-guide/acpi/apei/einj.html)

### PCIe Advanced Error Reporting (AER)

- [PCI Express Base Specification, §6.2 — Error Signaling and Logging](https://pcisig.com/specifications)
- [Kernel documentation: `Documentation/PCI/pcieaer-howto.rst`](https://www.kernel.org/doc/html/latest/PCI/pcieaer-howto.html)

### Fault Injection

- [Kernel fault injection framework: `Documentation/fault-injection/fault-injection.rst`](https://www.kernel.org/doc/html/latest/fault-injection/fault-injection.html)
- [`lib/fault-inject.c` in the kernel source](https://elixir.bootlin.com/linux/latest/source/lib/fault-inject.c)

### Prometheus and Grafana

- [Prometheus writing exporters guide](https://prometheus.io/docs/instrumenting/writing_exporters/)
- [Prometheus data model and metric types](https://prometheus.io/docs/concepts/metric_types/)
- [Grafana dashboard JSON model](https://grafana.com/docs/grafana/latest/dashboards/json-model/)

### Upstream Contributions (this author)

- **Marvell ARMADA pinctrl drivers** — `drivers/pinctrl/mvebu/pinctrl-armada-ap806.c`
  and `pinctrl-armada-cp110.c` in mainline Linux.
  Browse at [Elixir cross-reference](https://elixir.bootlin.com/linux/latest/source/drivers/pinctrl/mvebu).

- **Softirq regression diagnosis** — commit `3c53776e` (January 2018).
  `Reported-and-tested-by: Hanna Hawa <hhhawa@gmail.com>`.
  The fix restored correct softirq accounting under load — a direct resilience contribution.
  See [AUTHORS.md](AUTHORS.md) for full context.
