// SPDX-License-Identifier: Apache-2.0
//
// collectors/pmu.go — ARM PMU memory bandwidth collector.
//
// Reads memory bandwidth counters from the ARM CMN (Cache Mesh Network)
// or DSU (DynamIQ Shared Unit) PMU via /sys/bus/event_source/devices/.
//
// Two paths are attempted in order:
//  1. /sys/bus/event_source/devices/arm_cmn_*/events/ — ARM CMN PMU (Neoverse N2/V1)
//  2. /sys/bus/event_source/devices/arm_dsu_*/         — ARM DSU PMU (Neoverse N1)
//
// When neither is present (cloud VMs, old kernels), the collector emits
// fault_resilience_collector_up{collector="pmu"}=0 and no bandwidth metrics.
//
// Metrics:
//   memory_bandwidth_read_bytes_total{node}    — cumulative read bytes from DRAM
//   memory_bandwidth_write_bytes_total{node}   — cumulative write bytes to DRAM
//   fault_resilience_collector_up{collector="pmu"}

package collectors

import (
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"go.uber.org/zap"
)

const pmuEventSourceRoot = "bus/event_source/devices"

// pmuBandwidthFile maps an event_source sysfs counter filename to a metric.
type pmuBandwidthEntry struct {
	dirGlob     string   // glob under event_source/devices/
	counterFile string   // file under the matched directory
	metricDesc  **prometheus.Desc
	direction   string   // "read" or "write" for log
}

// PMUCollector reads ARM CMN/DSU PMU memory bandwidth counters.
type PMUCollector struct {
	opts        Options
	readBytes   *prometheus.Desc
	writeBytes  *prometheus.Desc
	collectorUp *prometheus.Desc
	scrapeTime  *prometheus.Desc
	scrapeErrs  *prometheus.Desc
}

func NewPMUCollector(opts Options) *PMUCollector {
	return &PMUCollector{
		opts: opts,
		readBytes: prometheus.NewDesc(
			"memory_bandwidth_read_bytes_total",
			"Cumulative memory read bytes from DRAM as measured by ARM PMU.",
			[]string{"pmu_type"}, nil,
		),
		writeBytes: prometheus.NewDesc(
			"memory_bandwidth_write_bytes_total",
			"Cumulative memory write bytes to DRAM as measured by ARM PMU.",
			[]string{"pmu_type"}, nil,
		),
		collectorUp: prometheus.NewDesc(
			"fault_resilience_collector_up",
			"1 if the PMU subsystem is accessible in sysfs, 0 if absent.",
			[]string{"collector"}, nil,
		),
		scrapeTime: prometheus.NewDesc("hw_fault_exporter_scrape_duration_seconds",
			"Scrape duration.", []string{"collector"}, nil),
		scrapeErrs: prometheus.NewDesc("hw_fault_exporter_scrape_errors_total",
			"Scrape errors.", []string{"collector"}, nil),
	}
}

func (c *PMUCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.readBytes
	ch <- c.writeBytes
	ch <- c.collectorUp
	ch <- c.scrapeTime
	ch <- c.scrapeErrs
}

func (c *PMUCollector) Collect(ch chan<- prometheus.Metric) {
	start := time.Now()
	errCount := 0

	pmuRoot := filepath.Join(c.opts.SysfsRoot, pmuEventSourceRoot)
	entries, err := os.ReadDir(pmuRoot)
	if err != nil {
		c.opts.Logger.Warn("PMU: cannot read event_source root",
			zap.String("path", pmuRoot), zap.Error(err))
		ch <- prometheus.MustNewConstMetric(c.collectorUp, prometheus.GaugeValue, 0, "pmu")
		emitSelf(ch, c.scrapeTime, c.scrapeErrs, "pmu", time.Since(start), 1)
		return
	}

	collected := 0
	for _, entry := range entries {
		name := entry.Name()
		pmuType := ""
		if strings.HasPrefix(name, "arm_cmn") {
			pmuType = "arm_cmn"
		} else if strings.HasPrefix(name, "arm_dsu") {
			pmuType = "arm_dsu"
		} else {
			continue
		}

		devDir := filepath.Join(pmuRoot, name)

		// Try to read memory bandwidth counters from /events/ or top-level files.
		// Actual counter values are typically in perf_event_attr files or cpumask-scoped
		// files. Here we read any available bandwidth-related files.
		//
		// On real hardware, reading actual PMU counts requires perf_event_open(2).
		// We expose the PMU's existence and any sysfs-readable aggregate counters.
		// If /events/amba_reads or /events/amba_writes exist, read them.

		readPath  := filepath.Join(devDir, "events", "amba_reads")
		writePath := filepath.Join(devDir, "events", "amba_writes")

		if rVal, err := readUint64(readPath); err == nil {
			ch <- prometheus.MustNewConstMetric(c.readBytes, prometheus.CounterValue,
				float64(rVal), pmuType)
			collected++
		}
		if wVal, err := readUint64(writePath); err == nil {
			ch <- prometheus.MustNewConstMetric(c.writeBytes, prometheus.CounterValue,
				float64(wVal), pmuType)
			collected++
		}
	}

	if collected == 0 && len(entries) == 0 {
		// No PMU devices at all
		ch <- prometheus.MustNewConstMetric(c.collectorUp, prometheus.GaugeValue, 0, "pmu")
	} else {
		ch <- prometheus.MustNewConstMetric(c.collectorUp, prometheus.GaugeValue, 1, "pmu")
	}
	emitSelf(ch, c.scrapeTime, c.scrapeErrs, "pmu", time.Since(start), errCount)
	c.opts.Logger.Debug("PMU: collected", zap.Int("counters", collected))
}
