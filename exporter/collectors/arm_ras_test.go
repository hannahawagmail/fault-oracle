// SPDX-License-Identifier: Apache-2.0
package collectors

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"go.uber.org/zap"
)

func buildMockARMRASRoot(t *testing.T, units map[string]map[string]string) string {
	t.Helper()
	root := t.TempDir()
	platDir := filepath.Join(root, "devices", "platform")
	if err := os.MkdirAll(platDir, 0755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	for unitName, files := range units {
		devDir := filepath.Join(platDir, "arm-ras."+unitName)
		if err := os.MkdirAll(devDir, 0755); err != nil {
			t.Fatalf("mkdir: %v", err)
		}
		for k, v := range files {
			if err := os.WriteFile(filepath.Join(devDir, k), []byte(v+"\n"), 0644); err != nil {
				t.Fatalf("write %s: %v", k, err)
			}
		}
	}
	return root
}

func TestARMRASCollector_MetricValues(t *testing.T) {
	root := buildMockARMRASRoot(t, map[string]map[string]string{
		"cpu0-l1": {
			"correctable_count":   "17",
			"uncorrectable_count": "2",
			"deferred_count":      "1",
		},
		"cpu0-l2": {
			"correctable_count":   "5",
			"uncorrectable_count": "0",
			"deferred_count":      "3",
		},
	})

	c := NewARMRASCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)

	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}

	// Verify expected metrics exist and values match
	type check struct {
		metric string
		unit   string
		value  float64
	}
	expected := []check{
		{"arm_ras_correctable_total", "cpu0-l1", 17},
		{"arm_ras_uncorrectable_total", "cpu0-l1", 2},
		{"arm_ras_deferred_total", "cpu0-l1", 1},
		{"arm_ras_correctable_total", "cpu0-l2", 5},
		{"arm_ras_uncorrectable_total", "cpu0-l2", 0},
		{"arm_ras_deferred_total", "cpu0-l2", 3},
	}

	for _, exp := range expected {
		found := false
		for _, mf := range mfs {
			if mf.GetName() != exp.metric {
				continue
			}
			for _, m := range mf.GetMetric() {
				for _, lp := range m.GetLabel() {
					if lp.GetName() == "unit" && lp.GetValue() == exp.unit {
						if m.GetCounter().GetValue() != exp.value {
							t.Errorf("%s{unit=%q}: got %v, want %v",
								exp.metric, exp.unit, m.GetCounter().GetValue(), exp.value)
						}
						found = true
					}
				}
			}
		}
		if !found {
			t.Errorf("metric %s{unit=%q} not found", exp.metric, exp.unit)
		}
	}
}

func TestARMRASCollector_CollectorUp(t *testing.T) {
	root := buildMockARMRASRoot(t, map[string]map[string]string{
		"cpu0-l1": {"correctable_count": "1", "uncorrectable_count": "0", "deferred_count": "0"},
	})

	c := NewARMRASCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)

	mfs, _ := reg.Gather()
	for _, mf := range mfs {
		if mf.GetName() != "fault_resilience_collector_up" {
			continue
		}
		for _, m := range mf.GetMetric() {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "collector" && lp.GetValue() == "arm_ras" {
					if m.GetGauge().GetValue() != 1 {
						t.Errorf("collector_up should be 1 when devices present, got %v", m.GetGauge().GetValue())
					}
					return
				}
			}
		}
	}
	t.Error("fault_resilience_collector_up{collector=arm_ras} not found")
}

func TestARMRASCollector_MissingSysfs_CollectorDown(t *testing.T) {
	c := NewARMRASCollector(Options{SysfsRoot: "/nonexistent/arm-ras", Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)

	mfs, _ := reg.Gather()
	for _, mf := range mfs {
		if mf.GetName() != "fault_resilience_collector_up" {
			continue
		}
		for _, m := range mf.GetMetric() {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "collector" && lp.GetValue() == "arm_ras" {
					if m.GetGauge().GetValue() != 0 {
						t.Errorf("collector_up should be 0 when sysfs missing, got %v", m.GetGauge().GetValue())
					}
					return
				}
			}
		}
	}
	t.Error("fault_resilience_collector_up{collector=arm_ras} not found")
}
