# Architecture Support

## Supported Architectures

| Architecture | Go GOARCH | Build target | Status |
|-------------|-----------|--------------|--------|
| ARM64 (AArch64) | `arm64` | `make build` | Full support — primary target |
| x86_64 | `amd64` | `make build-x86_64` | Supported — EDAC/MCE/AER/thermal all present |
| RISC-V 64-bit | `riscv64` | `make build-riscv64` | Builds cleanly; most collectors emit `collector_up=0` |

## Collector Availability by Architecture

| Collector | ARM64 | x86_64 | RISC-V |
|-----------|-------|--------|--------|
| EDAC | ✓ (sysfs CE/UE) | ✓ | ✗ (EDAC not upstream) |
| MCE | ✓ (via GHES) | ✓ (native MCE) | ✗ |
| PCIe AER | ✓ | ✓ | ✓ (if PCIe present) |
| Thermal | ✓ | ✓ | Partial (depends on BSP) |
| cpufreq | ✓ | ✓ | ✓ (if cpufreq driver) |
| ARM PMU (CMN/DSU) | ✓ | ✗ | ✗ |
| APEI BERT | ✓ (SBSA) | ✓ | ✗ |
| CXL | ✓ | ✓ | ✓ (if CXL present) |
| SmartNIC | ✓ | ✓ | ✓ |

`✗` = `CollectorSupported()` returns false or `fault_resilience_collector_up=0` at runtime.

## Runtime Architecture Detection

`exporter/collectors/arch.go` provides `CurrentArch()`, `IsARM64()`, `IsRISCV64()`, and `CollectorSupported(name)`:

```go
// In main.go registration loop:
if collectors.CollectorSupported("pmu") {
    registry.MustRegister(collectors.NewPMUCollector(opts))
}
```

Collectors that are architecture-optional should be gated at registration time. Collectors that simply lack sysfs paths should be registered anyway — they emit `collector_up=0`, which is visible in dashboards and drives the `CollectorDown` alert.

## Cross-Compiling

All Go code uses `CGO_ENABLED=0`, so cross-compilation works without a cross-sysroot:

```bash
# ARM64 (default)
make build

# x86_64
make build-x86_64

# RISC-V 64-bit
make build-riscv64

# All three
make build-all-arch
```

CI runs the RISC-V cross-compile in Stage 11 (`riscv64-cross-compile` job) on every push to verify the build remains clean. The resulting binary is verified as ELF RISC-V with `file(1)`.

## RISC-V Notes

RISC-V Linux currently lacks:
- Upstream EDAC drivers (work in progress as of kernel 6.8)
- ACPI APEI / BERT (RISC-V uses ACPI only on some platforms)
- ARM-specific PMU event sources (CMN, DSU)

All of these are handled gracefully: `CollectorSupported("pmu")` returns `false` on RISC-V, preventing registration. Other collectors register but emit `collector_up=0` when their sysfs paths are absent — identical behaviour to a cloud VM.

When RISC-V EDAC upstreaming completes, only `CollectorSupported()` in `arch.go` needs updating.
