// SPDX-License-Identifier: Apache-2.0
// collectors/membw.go — NUMA node memory utilization from sysfs node meminfo.
package collectors

import (
	"bufio"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"go.uber.org/zap"
)

type MembwCollector struct {
	opts      Options
	utilRatio *prometheus.Desc
	totalDesc *prometheus.Desc
	availDesc *prometheus.Desc
}

func NewMembwCollector(opts Options) *MembwCollector {
	return &MembwCollector{
		opts: opts,
		utilRatio: prometheus.NewDesc("memory_bandwidth_utilization_ratio",
			"Ratio of used to total memory per NUMA node (0-1).", []string{"node"}, nil),
		totalDesc: prometheus.NewDesc("memory_node_total_bytes",
			"Total memory in bytes per NUMA node.", []string{"node"}, nil),
		availDesc: prometheus.NewDesc("memory_node_available_bytes",
			"Available memory in bytes per NUMA node.", []string{"node"}, nil),
	}
}

func (c *MembwCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.utilRatio
	ch <- c.totalDesc
	ch <- c.availDesc
	ch <- collectorUpDesc
	ch <- scrapeDurationDesc
	ch <- scrapeErrorsDesc
}

func (c *MembwCollector) Collect(ch chan<- prometheus.Metric) {
	start := time.Now()
	errCount := 0
	pattern := filepath.Join(c.opts.SysfsRoot, "devices", "system", "node", "node*", "meminfo")
	matches, _ := filepath.Glob(pattern)
	if len(matches) == 0 {
		ch <- prometheus.MustNewConstMetric(collectorUpDesc, prometheus.GaugeValue, 0, "membw")
		emitSelf(ch, scrapeDurationDesc, scrapeErrorsDesc, "membw", time.Since(start), 1)
		return
	}
	for _, path := range matches {
		node := strings.TrimPrefix(filepath.Base(filepath.Dir(path)), "node")
		total, free, err := parseNodeMeminfo(path)
		if err != nil {
			c.opts.Logger.Warn("membw: parse error", zap.String("path", path), zap.Error(err))
			errCount++
			continue
		}
		ratio := 0.0
		if total > 0 {
			ratio = float64(total-free) / float64(total)
		}
		ch <- prometheus.MustNewConstMetric(c.utilRatio, prometheus.GaugeValue, ratio, node)
		ch <- prometheus.MustNewConstMetric(c.totalDesc, prometheus.GaugeValue, float64(total), node)
		ch <- prometheus.MustNewConstMetric(c.availDesc, prometheus.GaugeValue, float64(free), node)
	}
	ch <- prometheus.MustNewConstMetric(collectorUpDesc, prometheus.GaugeValue, 1, "membw")
	emitSelf(ch, scrapeDurationDesc, scrapeErrorsDesc, "membw", time.Since(start), errCount)
}

func parseNodeMeminfo(path string) (total, free uint64, err error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, 0, err
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	for s.Scan() {
		fields := strings.Fields(s.Text())
		if len(fields) < 4 {
			continue
		}
		val, e := strconv.ParseUint(fields[3], 10, 64)
		if e != nil {
			continue
		}
		val *= 1024 // meminfo reports in kB
		switch {
		case strings.Contains(fields[2], "MemTotal:"):
			total = val
		case strings.Contains(fields[2], "MemFree:"):
			free = val
		}
	}
	return total, free, s.Err()
}
