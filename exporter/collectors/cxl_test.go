// SPDX-License-Identifier: Apache-2.0
package collectors

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"go.uber.org/zap"
)

func buildMockCXLRoot(t *testing.T, devices map[string]map[string]string) string {
	t.Helper()
	root := t.TempDir()
	for devName, files := range devices {
		errDir := filepath.Join(root, "bus", "cxl", "devices", devName, "error_counts")
		if err := os.MkdirAll(errDir, 0755); err != nil {
			t.Fatalf("mkdir: %v", err)
		}
		for k, v := range files {
			if err := os.WriteFile(filepath.Join(errDir, k), []byte(v+"\n"), 0644); err != nil {
				t.Fatalf("write %s: %v", k, err)
			}
		}
	}
	return root
}

func TestCXLCollector_SingleDevice(t *testing.T) {
	root := buildMockCXLRoot(t, map[string]map[string]string{
		"mem0": {
			"volatile_correctable_data_error":     "42",
			"volatile_uncorrectable_data_error":   "0",
			"persistent_correctable_data_error":   "3",
			"persistent_uncorrectable_data_error": "0",
		},
	})

	c := NewCXLCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)

	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}

	names := map[string]bool{}
	for _, mf := range mfs {
		names[mf.GetName()] = true
	}
	for _, want := range []string{
		"cxl_correctable_errors_total",
		"fault_resilience_collector_up",
	} {
		if !names[want] {
			t.Errorf("missing metric: %s", want)
		}
	}
}

func TestCXLCollector_CorrectableValue(t *testing.T) {
	root := buildMockCXLRoot(t, map[string]map[string]string{
		"mem0": {"volatile_correctable_data_error": "99"},
	})
	c := NewCXLCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)

	mfs, _ := reg.Gather()
	for _, mf := range mfs {
		if mf.GetName() != "cxl_correctable_errors_total" {
			continue
		}
		for _, m := range mf.GetMetric() {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "device" && lp.GetValue() == "mem0" {
					if m.GetCounter().GetValue() != 99 {
						t.Errorf("expected 99 correctable errors, got %v",
							m.GetCounter().GetValue())
					}
				}
			}
		}
	}
}

func TestCXLCollector_MissingSysfs_CollectorDown(t *testing.T) {
	c := NewCXLCollector(Options{SysfsRoot: "/nonexistent/cxl", Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)

	mfs, _ := reg.Gather()
	for _, mf := range mfs {
		if mf.GetName() != "fault_resilience_collector_up" {
			continue
		}
		for _, m := range mf.GetMetric() {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "collector" && lp.GetValue() == "cxl" {
					if m.GetGauge().GetValue() != 0 {
						t.Errorf("collector_up should be 0 when sysfs missing")
					}
				}
			}
		}
	}
}

func TestCXLCollector_SkipsNonMemDevices(t *testing.T) {
	root := buildMockCXLRoot(t, map[string]map[string]string{
		"mem0": {"volatile_correctable_data_error": "5"},
	})
	// Add a non-mem device directory
	os.MkdirAll(filepath.Join(root, "bus", "cxl", "devices", "port0"), 0755)

	c := NewCXLCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)

	mfs, _ := reg.Gather()
	for _, mf := range mfs {
		if mf.GetName() != "cxl_correctable_errors_total" {
			continue
		}
		for _, m := range mf.GetMetric() {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "device" && lp.GetValue() == "port0" {
					t.Error("port0 should not appear in CXL metrics")
				}
			}
		}
	}
}

func TestCXLCollector_Describe(t *testing.T) {
	c := NewCXLCollector(Options{SysfsRoot: "/nonexistent", Logger: zap.NewNop()})
	ch := make(chan *prometheus.Desc, 16)
	c.Describe(ch)
	close(ch)

	count := 0
	for range ch {
		count++
	}
	if count == 0 {
		t.Error("Describe produced no descriptors")
	}
}
