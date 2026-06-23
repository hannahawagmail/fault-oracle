// SPDX-License-Identifier: Apache-2.0
package collectors

import (
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

func TestSelfCollector_EmitsExpectedMetrics(t *testing.T) {
	c := NewSelfCollector("1.2.3-test")
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)
	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}

	want := map[string]bool{
		"hw_fault_exporter_build_info":      false,
		"hw_fault_exporter_uptime_seconds":  false,
	}
	for _, mf := range mfs {
		if _, ok := want[mf.GetName()]; ok {
			want[mf.GetName()] = true
		}
	}
	for name, found := range want {
		if !found {
			t.Errorf("expected metric %q not emitted", name)
		}
	}
}

func TestSelfCollector_BuildInfoValue(t *testing.T) {
	c := NewSelfCollector("v0.9.0")
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)
	mfs, _ := reg.Gather()
	for _, mf := range mfs {
		if mf.GetName() != "hw_fault_exporter_build_info" {
			continue
		}
		for _, m := range mf.GetMetric() {
			if m.GetGauge().GetValue() != 1 {
				t.Errorf("build_info value: want 1, got %v", m.GetGauge().GetValue())
			}
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "version" && lp.GetValue() != "v0.9.0" {
					t.Errorf("version label: want v0.9.0, got %s", lp.GetValue())
				}
			}
		}
	}
}

func TestSelfCollector_UptimePositive(t *testing.T) {
	c := NewSelfCollector("test")
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)
	mfs, _ := reg.Gather()
	for _, mf := range mfs {
		if mf.GetName() != "hw_fault_exporter_uptime_seconds" {
			continue
		}
		for _, m := range mf.GetMetric() {
			if m.GetGauge().GetValue() < 0 {
				t.Errorf("uptime should be >= 0, got %v", m.GetGauge().GetValue())
			}
		}
	}
}

func TestSelfCollector_DescribeCount(t *testing.T) {
	c := NewSelfCollector("test")
	ch := make(chan *prometheus.Desc, 10)
	c.Describe(ch)
	close(ch)

	count := 0
	for range ch {
		count++
	}
	if count != 2 {
		t.Errorf("Describe: want 2 descriptors, got %d", count)
	}
}
