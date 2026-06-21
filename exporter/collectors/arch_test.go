// SPDX-License-Identifier: Apache-2.0
package collectors

import (
	"runtime"
	"testing"
)

func TestCurrentArch(t *testing.T) {
	arch := CurrentArch()
	// Must be one of the known values
	valid := map[string]bool{
		ArchARM64:   true,
		ArchRISCV64: true,
		ArchX86_64:  true,
		ArchUnknown: true,
	}
	if !valid[arch] {
		t.Errorf("CurrentArch() returned unknown value %q", arch)
	}
}

func TestCurrentArchMatchesGOARCH(t *testing.T) {
	arch := CurrentArch()
	switch runtime.GOARCH {
	case "arm64":
		if arch != ArchARM64 {
			t.Errorf("GOARCH=arm64 but CurrentArch()=%q", arch)
		}
	case "amd64":
		if arch != ArchX86_64 {
			t.Errorf("GOARCH=amd64 but CurrentArch()=%q", arch)
		}
	case "riscv64":
		if arch != ArchRISCV64 {
			t.Errorf("GOARCH=riscv64 but CurrentArch()=%q", arch)
		}
	}
}

func TestIsARM64(t *testing.T) {
	got := IsARM64()
	want := runtime.GOARCH == "arm64"
	if got != want {
		t.Errorf("IsARM64()=%v want %v", got, want)
	}
}

func TestIsRISCV64(t *testing.T) {
	got := IsRISCV64()
	want := runtime.GOARCH == "riscv64"
	if got != want {
		t.Errorf("IsRISCV64()=%v want %v", got, want)
	}
}

func TestCollectorSupportedPMU(t *testing.T) {
	// PMU is only supported on arm64
	supported := CollectorSupported("pmu")
	if runtime.GOARCH == "arm64" && !supported {
		t.Error("pmu should be supported on arm64")
	}
	if runtime.GOARCH == "riscv64" && supported {
		t.Error("pmu should NOT be supported on riscv64")
	}
}

func TestCollectorSupportedAPEI(t *testing.T) {
	supported := CollectorSupported("apei")
	if runtime.GOARCH == "riscv64" && supported {
		t.Error("apei should NOT be supported on riscv64")
	}
	if (runtime.GOARCH == "arm64" || runtime.GOARCH == "amd64") && !supported {
		t.Errorf("apei should be supported on %s", runtime.GOARCH)
	}
}

func TestCollectorSupportedEDACAlwaysTrue(t *testing.T) {
	// EDAC falls through as supported everywhere (may emit collector_up=0 at runtime)
	if !CollectorSupported("edac") {
		t.Error("edac should be supported (returns collector_up=0 when absent)")
	}
	if !CollectorSupported("mce") {
		t.Error("mce should be supported")
	}
	if !CollectorSupported("aer") {
		t.Error("aer should be supported")
	}
	if !CollectorSupported("thermal") {
		t.Error("thermal should be supported")
	}
}
