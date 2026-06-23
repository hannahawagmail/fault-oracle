// SPDX-License-Identifier: Apache-2.0
package collectors

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
	"go.uber.org/zap"
)

func TestNUMAImbalanceCollector_Metrics(t *testing.T) {
	root := t.TempDir()
	nodeDir := filepath.Join(root, "devices", "system", "node", "node0")
	if err := os.MkdirAll(nodeDir, 0o755); err != nil {
		t.Fatal(err)
	}
	content := "numa_hit 1000\nnuma_miss 200\nnuma_foreign 50\nlocal_node 900\nother_node 300\n"
	if err := os.WriteFile(filepath.Join(nodeDir, "numastat"), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	c := NewNUMAImbalanceCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	if err := reg.Register(c); err != nil {
		t.Fatal(err)
	}

	expected := `
# HELP numa_hit_total Local memory allocations.
# TYPE numa_hit_total counter
numa_hit_total{node="0"} 1000
# HELP numa_miss_total Remote memory allocations (cross-NUMA).
# TYPE numa_miss_total counter
numa_miss_total{node="0"} 200
# HELP numa_foreign_total Allocations intended for this node but placed elsewhere.
# TYPE numa_foreign_total counter
numa_foreign_total{node="0"} 50
# HELP numa_imbalance_ratio Cross-NUMA imbalance ratio: miss/(hit+miss).
# TYPE numa_imbalance_ratio gauge
numa_imbalance_ratio{node="0"} 0.16666666666666666
# HELP fault_resilience_collector_up 1 if the collector is running and producing metrics, 0 if disabled or hardware absent
# TYPE fault_resilience_collector_up gauge
fault_resilience_collector_up{collector="numa_imbalance"} 1
`
	if err := testutil.GatherAndCompare(reg, strings.NewReader(expected),
		"numa_hit_total", "numa_miss_total", "numa_foreign_total",
		"numa_imbalance_ratio", "fault_resilience_collector_up"); err != nil {
		t.Errorf("unexpected metrics:\n%s", err)
	}
}

func TestNUMAImbalanceCollector_MissingSysfs(t *testing.T) {
	root := t.TempDir() // empty — no node directory

	c := NewNUMAImbalanceCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	if err := reg.Register(c); err != nil {
		t.Fatal(err)
	}

	expected := `
# HELP fault_resilience_collector_up 1 if the collector is running and producing metrics, 0 if disabled or hardware absent
# TYPE fault_resilience_collector_up gauge
fault_resilience_collector_up{collector="numa_imbalance"} 0
`
	if err := testutil.GatherAndCompare(reg, strings.NewReader(expected), "fault_resilience_collector_up"); err != nil {
		t.Errorf("unexpected metrics:\n%s", err)
	}
}
