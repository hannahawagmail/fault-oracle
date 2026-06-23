// SPDX-License-Identifier: Apache-2.0
package collectors

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"go.uber.org/zap"
)

func buildMockNodeMeminfo(t *testing.T, nodes map[string][2]uint64) string {
	t.Helper()
	root := t.TempDir()
	for node, vals := range nodes {
		dir := filepath.Join(root, "devices", "system", "node", "node"+node)
		os.MkdirAll(dir, 0755)
		// Format matches /sys/devices/system/node/node0/meminfo
		content := "Node " + node + " MemTotal:       " + uitoa(vals[0]) + " kB\n" +
			"Node " + node + " MemFree:        " + uitoa(vals[1]) + " kB\n"
		os.WriteFile(filepath.Join(dir, "meminfo"), []byte(content), 0644)
	}
	return root
}

func uitoa(v uint64) string {
	buf := make([]byte, 0, 20)
	return string(append(buf, []byte(func() string {
		s := ""
		if v == 0 {
			return "0"
		}
		for v > 0 {
			s = string(rune('0'+v%10)) + s
			v /= 10
		}
		return s
	}())...))
}

func TestMembwCollector_Metrics(t *testing.T) {
	// 1GB total, 256MB free per node
	root := buildMockNodeMeminfo(t, map[string][2]uint64{
		"0": {1048576, 262144}, // kB values
	})
	c := NewMembwCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)
	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}

	found := map[string]float64{}
	for _, mf := range mfs {
		for _, m := range mf.GetMetric() {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "node" && lp.GetValue() == "0" {
					found[mf.GetName()] = m.GetGauge().GetValue()
				}
			}
		}
	}

	expectedTotal := float64(1048576 * 1024)
	expectedAvail := float64(262144 * 1024)
	expectedRatio := (expectedTotal - expectedAvail) / expectedTotal

	if v, ok := found["memory_node_total_bytes"]; !ok || v != expectedTotal {
		t.Errorf("total: got %v want %v", v, expectedTotal)
	}
	if v, ok := found["memory_node_available_bytes"]; !ok || v != expectedAvail {
		t.Errorf("avail: got %v want %v", v, expectedAvail)
	}
	if v, ok := found["memory_bandwidth_utilization_ratio"]; !ok || v != expectedRatio {
		t.Errorf("ratio: got %v want %v", v, expectedRatio)
	}
}

func TestMembwCollector_NoNodes_Down(t *testing.T) {
	root := t.TempDir()
	os.MkdirAll(filepath.Join(root, "devices", "system", "node"), 0755)
	c := NewMembwCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)
	mfs, _ := reg.Gather()

	for _, mf := range mfs {
		if mf.GetName() != "fault_resilience_collector_up" {
			continue
		}
		for _, m := range mf.GetMetric() {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "collector" && lp.GetValue() == "membw" {
					if m.GetGauge().GetValue() != 0 {
						t.Error("collector_up should be 0 with no nodes")
					}
				}
			}
		}
	}
}
