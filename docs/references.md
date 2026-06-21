# References

Canonical upstream references for every specification, kernel subsystem, and tool used
in this repository.

---

## Linux Kernel Documentation

| Topic | URL |
|-------|-----|
| EDAC subsystem (driver API) | https://www.kernel.org/doc/html/latest/driver-api/edac.html |
| EDAC sysfs ABI | https://www.kernel.org/doc/html/latest/admin-guide/ras.html |
| Memory failure handling | https://www.kernel.org/doc/html/latest/vm/memory-failure.html |
| Fault injection framework | https://www.kernel.org/doc/html/latest/fault-injection/fault-injection.html |
| PCIe AER howto | https://www.kernel.org/doc/html/latest/PCI/pcieaer-howto.html |
| ACPI APEI (firmware error interface) | https://www.kernel.org/doc/html/latest/firmware-guide/acpi/apei/overview.html |
| ACPI EINJ (error injection) | https://www.kernel.org/doc/html/latest/firmware-guide/acpi/apei/einj.html |
| RAS tracepoints | https://www.kernel.org/doc/html/latest/admin-guide/ras.html |
| Kernel source — EDAC drivers | https://elixir.bootlin.com/linux/latest/source/drivers/edac |
| Kernel source — PCIe AER | https://elixir.bootlin.com/linux/latest/source/drivers/pci/pcie/aer.c |
| Kernel source — memory failure | https://elixir.bootlin.com/linux/latest/source/mm/memory-failure.c |
| Kernel source — GHES | https://elixir.bootlin.com/linux/latest/source/drivers/acpi/apei/ghes.c |
| Kernel source — fault inject | https://elixir.bootlin.com/linux/latest/source/lib/fault-inject.c |

---

## ARM Architecture References

| Topic | URL |
|-------|-----|
| ARM RAS Extension Architecture | https://developer.arm.com/documentation/ddi0597/latest |
| ARM Cortex-A72 Technical Reference Manual | https://developer.arm.com/documentation/100095/latest |
| ARM Cortex-A72 MPCore Technical Reference Manual | https://developer.arm.com/documentation/100094/latest |
| ARM Architecture Reference Manual (ARMv8-A) | https://developer.arm.com/documentation/ddi0487/latest |
| ARM AMBA AXI Protocol Specification | https://developer.arm.com/documentation/ihi0022/latest |
| ARM Server Base System Architecture (SBSA) | https://developer.arm.com/documentation/den0029/latest |
| ARM Server Base Boot Requirements (SBBR) | https://developer.arm.com/documentation/den0044/latest |
| ARM L2C-310 Level-2 Cache Controller TRM | https://developer.arm.com/documentation/ddi0246/latest |

---

## PCIe Specifications

| Topic | URL |
|-------|-----|
| PCI Express Base Specification | https://pcisig.com/specifications (membership required for full spec) |
| PCIe AER §6.2 summary (Wikipedia) | https://en.wikipedia.org/wiki/PCI_Express#Advanced_Error_Reporting |
| PCI SIG Error Reporting ECN | https://pcisig.com/specifications/pciexpress/technical_library |
| Linux PCI Howto | https://www.kernel.org/doc/html/latest/PCI/pci.html |

---

## ACPI / UEFI / RAS Standards

| Topic | URL |
|-------|-----|
| ACPI Specification (current) | https://uefi.org/specifications |
| UEFI Specification (CPER defined in Appendix N) | https://uefi.org/specifications |
| ACPI HEST / BERT / GHES overview | https://uefi.org/sites/default/files/resources/ACPI_5_1.pdf §18 |
| IPMI Platform Management FRU Information | https://www.intel.com/content/www/us/en/servers/ipmi/ipmi-platform-mgt-fru-infostorage-def.html |

---

## Prometheus and Grafana

| Topic | URL |
|-------|-----|
| Prometheus data model | https://prometheus.io/docs/concepts/data_model/ |
| Prometheus metric types | https://prometheus.io/docs/concepts/metric_types/ |
| Prometheus writing exporters | https://prometheus.io/docs/instrumenting/writing_exporters/ |
| Prometheus best practices: naming | https://prometheus.io/docs/practices/naming/ |
| Prometheus best practices: instrumentation | https://prometheus.io/docs/practices/instrumentation/ |
| Prometheus client Go library | https://github.com/prometheus/client_golang |
| Grafana dashboard JSON model | https://grafana.com/docs/grafana/latest/dashboards/json-model/ |
| Grafana alerting | https://grafana.com/docs/grafana/latest/alerting/ |

---

## Upstream Contributions (this author)

| Contribution | Link |
|-------------|-------|
| `pinctrl-armada-ap806.c` (mainline) | https://elixir.bootlin.com/linux/latest/source/drivers/pinctrl/mvebu/pinctrl-armada-ap806.c |
| `pinctrl-armada-cp110.c` (mainline) | https://elixir.bootlin.com/linux/latest/source/drivers/pinctrl/mvebu/pinctrl-armada-cp110.c |
| Linus Torvalds credit — commit `3c53776e` | https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=3c53776e |

---

## Related Tools and Projects

| Tool | Purpose | URL |
|------|---------|-----|
| `edac-utils` | Userspace EDAC utilities (edac-util, edac-ctl) | https://github.com/grondo/edac-utils |
| `mcelog` | Machine check error logger (x86) | https://mcelog.org/ |
| `rasdaemon` | RAS event logger (ARM64, x86) using kernel tracepoints | https://github.com/mchehab/rasdaemon |
| `aer-inject` | PCIe AER injection tool (in kernel tools/) | https://elixir.bootlin.com/linux/latest/source/tools/pci/aer-inject.c |
| QEMU | x86/ARM emulator used for reference testing | https://www.qemu.org/ |
| `qemu-system-aarch64` virt machine | ARM64 virtual machine target | https://www.qemu.org/docs/master/system/arm/virt.html |
| `prometheus/node_exporter` | Reference Prometheus exporter (Go) | https://github.com/prometheus/node_exporter |

---

## Background Reading

| Title | URL |
|-------|-----|
| "DRAM Errors in the Wild: A Large-Scale Field Study" (Schroeder et al., SIGMETRICS 2009) | https://research.google/pubs/pub35162/ |
| "Memory Errors in Modern Systems" (Sridharan et al., ASPLOS 2015) | https://dl.acm.org/doi/10.1145/2694344.2694348 |
| "Understanding and Addressing ARM Hardware Errors in Linux" (LPC 2019) | https://linuxplumbersconf.org/event/4/ |
| Linux kernel RAS wiki | https://www.kernel.org/doc/html/latest/admin-guide/ras.html |
| "Reliability, Availability and Serviceability" — ARM white paper | https://developer.arm.com/tools-and-software/open-source-software/linux-kernel/ras |
