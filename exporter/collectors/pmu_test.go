// SPDX-License-Identifier: Apache-2.0
package collectors

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"go.uber.org/zap"
)

func buildMockPMURoot(t *testing.T, devices map[string]map[string]string) string {
	t.Helper()
	root := t.TempDir()
	for dev, events := range devices {
		evtDir := filepath.Join(root, "bus", "event_source", "devices", dev, "events")
		os.MkdirAll(evtDir, 0755)
		for k, v := range events {
			os.WriteFile(filepath.Join(evtDir, k), []byte(v+"\n"), 0644)
		}
	}
	return root
}

func TestPMUCollector_CMNBandwidth(t *testing.T) {
	root := buildMockPMURoot(t, map[string]map[string]string{
		"arm_cmn_0": {"amba_reads": "1000000", "amba_writes": "500000"},
	})
	c := NewPMUCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)
	mfs, _ := reg.Gather()

	names := map[string]bool{}
	for _, mf := range mfs {
		names[mf.GetName()] = true
	}
	if !names["memory_bandwidth_read_bytes_total"] {
		t.Error("memory_bandwidth_read_bytes_total not found")
	}
}

func TestPMUCollector_NoDevices_CollectorDown(t *testing.T) {
	root := t.TempDir()
	os.MkdirAll(filepath.Join(root, "bus", "event_source", "devices"), 0755)

	c := NewPMUCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)
	mfs, _ := reg.Gather()

	for _, mf := range mfs {
		if mf.GetName() != "fault_resilience_collector_up" {
			continue
		}
		for _, m := range mf.GetMetric() {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "collector" && lp.GetValue() == "pmu" {
					if m.GetGauge().GetValue() != 0 {
						t.Error("collector_up should be 0 with no devices")
					}
				}
			}
		}
	}
}

func TestPMUCollector_MissingSysfs(t *testing.T) {
	c := NewPMUCollector(Options{SysfsRoot: "/nonexistent", Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)
	_, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather should not error: %v", err)
	}
}

func TestPMUCollector_SkipsNonArmPMU(t *testing.T) {
	root := buildMockPMURoot(t, map[string]map[string]string{
		"intel_cqm_0": {"llc_occupancy": "999"},
		"arm_cmn_0":   {"amba_reads": "42"},
	})
	c := NewPMUCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)
	mfs, _ := reg.Gather()

	for _, mf := range mfs {
		if mf.GetName() == "memory_bandwidth_read_bytes_total" {
			for _, m := range mf.GetMetric() {
				for _, lp := range m.GetLabel() {
					if lp.GetName() == "pmu_type" && lp.GetValue() == "intel_cqm" {
						t.Error("intel_cqm should not appear in ARM PMU metrics")
					}
				}
			}
		}
	}
}
