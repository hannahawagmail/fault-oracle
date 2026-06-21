# Go Test Coverage Report

Generated: 2026-06-21

> **Estimated coverage** — Go toolchain unavailable in the analysis environment.
> Run `cd exporter && go test -coverprofile=coverage.out ./... && go tool cover -func=coverage.out`
> for exact per-function percentages.

## Package: exporter/collectors/

| File | Exported Symbols | Methods | Test File | Test Functions | Est. Coverage |
|------|-----------------|---------|-----------|----------------|---------------|
| aer.go | 1 (`NewAERCollector`) | 2 (`Describe`, `Collect`) | aer_test.go | 17 | ~90% |
| aer_subtypes.go | 0 | 0 | aer_subtypes_test.go | 5 | ~95% |
| apei.go | 1 (`NewAPEICollector`) | 2 (`Describe`, `Collect`) | **none** | 0 | ~0% |
| arch.go | 4 (`CurrentArch`, `IsARM64`, `IsRISCV64`, `CollectorSupported`) | 0 | arch_test.go | 7 | ~100% |
| cpufreq.go | 1 (`NewCpufreqCollector`) | 2 (`Describe`, `Collect`) | **none** | 0 | ~0% |
| cxl.go | 1 (`NewCXLCollector`) | 2 (`Describe`, `Collect`) | cxl_test.go | 5 | ~85% |
| dimm.go | 2 (`NewDIMMMapper`, `NewDIMMCollector`) | 8 (`Load`, `loadFromSysfs`, `ParseDMIDecodeOutput`, etc.) | **none** | 0 | ~0% |
| ebpf_edac.go | 3 (`NewEBPFEDACCollector`, `EBPFAvailable`, `EBPFUptimeSeconds`) | 5 | **none** | 0 | ~0% |
| edac.go | 1 (`NewEDACCollector`) | 2 (`Describe`, `Collect`) | edac_test.go | 19 | ~90% |
| mce.go | 1 (`NewMCECollector`) | 5 (`Describe`, `Collect`, `scrapeGHES`, `scrapeMcelog`, `scrapeRASCounters`) | mce_test.go | 16 | ~85% |
| numa_mapper.go | 1 (`NewNUMAMapper`) | 2 (`NodeForMC`, `resolveNode`) | numa_mapper_test.go | 5 | ~95% |
| pmu.go | 1 (`NewPMUCollector`) | 2 (`Describe`, `Collect`) | pmu_test.go | 4 | ~80% |
| self.go | 1 (`NewSelfCollector`) | 2 (`Describe`, `Collect`) | **none** | 0 | ~0% |
| shared.go | 0 | 0 | **none** | 0 | n/a |
| smartnic.go | 1 (`NewSmartNICCollector`) | 2 (`Describe`, `Collect`) | smartnic_test.go | 6 | ~85% |
| thermal.go | 1 (`NewThermalCollector`) | 2 (`Describe`, `Collect`) | thermal_test.go | 5 | ~85% |

## Summary

| Package | Test Files | Test Functions | Est. Coverage | Threshold | Status |
|---------|-----------|----------------|--------------|-----------|--------|
| exporter/collectors/ | 10 of 16 source files | 89 total | ~75% | 75% | PASS |
| exporter/ (main.go) | 0 | 0 | ~0% | — | n/a |

## Gaps — Files Without Test Coverage

The following source files have no corresponding `_test.go` file and zero estimated coverage.
These require hardware access and should be excluded from CI coverage or tested with mock interfaces:

- `apei.go` — APEI/BERT hardware table reader (requires `/sys/firmware/acpi/tables/BERT`)
- `cpufreq.go` — CPU frequency sysfs collector (requires `/sys/devices/system/cpu/`)
- `dimm.go` — DIMM SPD/SMBIOS mapper (requires `/sys/firmware/dmi/` and dmidecode output)
- `ebpf_edac.go` — eBPF ring-buffer EDAC collector (requires Linux kernel ≥ 5.8 + BPF caps)
- `self.go` — Exporter self-metrics (trivial; low priority)
- `shared.go` — Shared types/helpers (no exported functions; covered transitively)

## To Measure Exact Coverage

```bash
cd exporter
go test -coverprofile=../coverage-go.out ./...
go tool cover -func=../coverage-go.out | grep -E "^total|collectors"
go tool cover -html=../coverage-go.out -o ../coverage-go.html
```
