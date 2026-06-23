// SPDX-License-Identifier: Apache-2.0
package collectors

import (
	"fmt"
	"os"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"go.uber.org/zap"
)

func TestCheckKernelVersion(t *testing.T) {
	cases := []struct {
		ver    string
		tooOld bool
	}{
		{"Linux version 5.8.0-generic (build@host)", false},
		{"Linux version 5.15.2-arm64", false},
		{"Linux version 6.1.0", false},
		{"Linux version 5.7.19-generic", true},
		{"Linux version 4.19.0", true},
	}
	for _, tc := range cases {
		var kmaj, kmin, kpatch int
		if _, err := fmt.Sscanf(tc.ver, "Linux version %d.%d.%d", &kmaj, &kmin, &kpatch); err != nil {
			t.Fatalf("parse %q: %v", tc.ver, err)
		}
		got := kmaj < 5 || (kmaj == 5 && kmin < 8)
		if got != tc.tooOld {
			t.Errorf("version %q: got tooOld=%v, want %v", tc.ver, got, tc.tooOld)
		}
	}
}

func TestEBPFAvailable(t *testing.T) {
	t.Setenv("EBPF_DISABLED", "0")
	if EBPFAvailable() {
		if _, err := os.Stat("/sys/kernel/btf/vmlinux"); err != nil {
			t.Fatal("EBPFAvailable()=true but BTF absent")
		}
	}
	t.Setenv("EBPF_DISABLED", "1")
	if EBPFAvailable() {
		t.Fatal("EBPFAvailable()=true with EBPF_DISABLED=1")
	}
}

func TestNewEBPFEDACCollector_Unavailable(t *testing.T) {
	t.Setenv("EBPF_DISABLED", "1")
	c := NewEBPFEDACCollector(Options{SysfsRoot: "/nonexistent", Logger: zap.NewNop()})

	ch := make(chan prometheus.Metric, 10)
	c.Collect(ch)
	close(ch)
	found := false
	for m := range ch {
		if m.Desc() == collectorUpDesc {
			found = true
		}
	}
	if !found {
		t.Fatal("expected collector_up metric when eBPF unavailable")
	}
}

func TestDescribe(t *testing.T) {
	t.Setenv("EBPF_DISABLED", "1")
	c := NewEBPFEDACCollector(Options{SysfsRoot: "/nonexistent", Logger: zap.NewNop()})

	ch := make(chan *prometheus.Desc, 10)
	c.Describe(ch)
	close(ch)
	if len(ch) != 5 {
		t.Fatalf("Describe: got %d descriptors, want 5", len(ch))
	}
}
