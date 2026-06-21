// SPDX-License-Identifier: Apache-2.0
// arch.go — Runtime CPU architecture detection for conditional collector registration.
//
// Some collectors (EDAC, MCE, AER, PMU) read ARM64-specific sysfs paths and
// are meaningless (or return collector_up=0) on RISC-V or x86. This file
// provides a central place to gate collector registration.
package collectors

import (
	"runtime"
	"strings"
)

// Arch constants returned by CurrentArch.
const (
	ArchARM64   = "arm64"
	ArchRISCV64 = "riscv64"
	ArchX86_64  = "x86_64"
	ArchUnknown = "unknown"
)

// CurrentArch returns the canonical architecture string for the running process.
// It normalises Go's GOARCH values to the Linux uname convention.
func CurrentArch() string {
	switch runtime.GOARCH {
	case "arm64":
		return ArchARM64
	case "riscv64":
		return ArchRISCV64
	case "amd64":
		return ArchX86_64
	default:
		return ArchUnknown
	}
}

// IsARM64 returns true when running on an ARM64 host.
func IsARM64() bool { return CurrentArch() == ArchARM64 }

// IsRISCV64 returns true when running on a RISC-V 64-bit host.
func IsRISCV64() bool { return CurrentArch() == ArchRISCV64 }

// CollectorSupported returns whether the named collector is meaningful
// on the current architecture.
//
// Collectors that read ARM64-specific sysfs paths (PMU, APEI BERT) are
// marked as unsupported on RISC-V; all others fall through as supported
// (they will simply emit collector_up=0 when sysfs paths are absent).
func CollectorSupported(name string) bool {
	arch := CurrentArch()
	switch strings.ToLower(name) {
	case "pmu":
		// ARM CMN/DSU PMU — not present on RISC-V or generic x86
		return arch == ArchARM64
	case "apei":
		// ACPI APEI is x86 and ARM64 only; RISC-V uses a different mechanism
		return arch == ArchARM64 || arch == ArchX86_64
	default:
		// EDAC, MCE, AER, thermal, cpufreq: present on both ARM64 and x86,
		// simply absent on RISC-V — let the collector report collector_up=0.
		return true
	}
}
