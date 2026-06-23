// SPDX-License-Identifier: Apache-2.0
// collectors/numa_imbalance.go — Detects cross-NUMA memory access imbalance.
// Scrapes /sys/devices/system/node/node*/numastat for hit/miss/foreign counters.
package collectors

import (
	"bufio"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/prometheus/client_golang/prometheus"
)

// NUMAImbalanceCollector implements prometheus.Collector for NUMA memory stats.
type NUMAImbalanceCollector struct {
	opts        Options
	hitDesc     *prometheus.Desc
	missDesc    *prometheus.Desc
	foreignDesc *prometheus.Desc
	ratioDesc   *prometheus.Desc
}

// NewNUMAImbalanceCollector creates a new NUMA imbalance collector.
func NewNUMAImbalanceCollector(opts Options) *NUMAImbalanceCollector {
	return &NUMAImbalanceCollector{
		opts:        opts,
		hitDesc:     prometheus.NewDesc("numa_hit_total", "Local memory allocations.", []string{"node"}, nil),
		missDesc:    prometheus.NewDesc("numa_miss_total", "Remote memory allocations (cross-NUMA).", []string{"node"}, nil),
		foreignDesc: prometheus.NewDesc("numa_foreign_total", "Allocations intended for this node but placed elsewhere.", []string{"node"}, nil),
		ratioDesc:   prometheus.NewDesc("numa_imbalance_ratio", "Cross-NUMA imbalance ratio: miss/(hit+miss).", []string{"node"}, nil),
	}
}

func (c *NUMAImbalanceCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.hitDesc
	ch <- c.missDesc
	ch <- c.foreignDesc
	ch <- c.ratioDesc
}

func (c *NUMAImbalanceCollector) Collect(ch chan<- prometheus.Metric) {
	nodeRoot := filepath.Join(c.opts.SysfsRoot, "devices", "system", "node")
	entries, err := os.ReadDir(nodeRoot)
	if err != nil {
		ch <- prometheus.MustNewConstMetric(collectorUpDesc, prometheus.GaugeValue, 0, "numa_imbalance")
		return
	}
	found := false
	for _, e := range entries {
		if !e.IsDir() || !strings.HasPrefix(e.Name(), "node") {
			continue
		}
		nodeID := strings.TrimPrefix(e.Name(), "node")
		stats, err := parseNumastat(filepath.Join(nodeRoot, e.Name(), "numastat"))
		if err != nil {
			continue
		}
		found = true
		hit, miss, foreign := stats["numa_hit"], stats["numa_miss"], stats["numa_foreign"]
		ch <- prometheus.MustNewConstMetric(c.hitDesc, prometheus.CounterValue, float64(hit), nodeID)
		ch <- prometheus.MustNewConstMetric(c.missDesc, prometheus.CounterValue, float64(miss), nodeID)
		ch <- prometheus.MustNewConstMetric(c.foreignDesc, prometheus.CounterValue, float64(foreign), nodeID)
		ratio := 0.0
		if hit+miss > 0 {
			ratio = float64(miss) / float64(hit+miss)
		}
		ch <- prometheus.MustNewConstMetric(c.ratioDesc, prometheus.GaugeValue, ratio, nodeID)
	}
	up := 0.0
	if found {
		up = 1.0
	}
	ch <- prometheus.MustNewConstMetric(collectorUpDesc, prometheus.GaugeValue, up, "numa_imbalance")
}

func parseNumastat(path string) (map[string]uint64, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	m := make(map[string]uint64)
	s := bufio.NewScanner(f)
	for s.Scan() {
		parts := strings.Fields(s.Text())
		if len(parts) == 2 {
			if v, err := strconv.ParseUint(parts[1], 10, 64); err == nil {
				m[parts[0]] = v
			}
		}
	}
	return m, s.Err()
}
