// SPDX-License-Identifier: Apache-2.0
//
// collectors/cxl.go — Prometheus collector for CXL 2.0 memory error counters.
//
// CXL (Compute Express Link) memory devices expose error counts under:
//   /sys/bus/cxl/devices/mem<N>/
//     error_counts/           (kernel ≥ 6.2)
//       volatile_correctable_data_error
//       volatile_uncorrectable_data_error
//       volatile_uncorrectable_no_addr_error
//       persistent_correctable_data_error
//       persistent_uncorrectable_data_error
//
// Metrics emitted:
//   cxl_correctable_errors_total{device, error_type}
//   cxl_uncorrectable_errors_total{device, error_type}
//   fault_resilience_collector_up{collector="cxl"}
//
// References: CXL 2.0 spec §8.2 (Error Handling), Linux kernel drivers/cxl/mem.c

package collectors

import (
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"go.uber.org/zap"
)

const cxlBusRoot = "bus/cxl/devices"

// cxlStatFile maps a sysfs filename to its metric category (correctable/uncorrectable).
type cxlStatFile struct {
	name       string
	errorType  string // "correctable" or "uncorrectable"
	label      string // value of error_type label
}

var cxlStatFiles = []cxlStatFile{
	{"volatile_correctable_data_error",        "correctable", "volatile_data"},
	{"volatile_uncorrectable_data_error",       "uncorrectable", "volatile_data"},
	{"volatile_uncorrectable_no_addr_error",    "uncorrectable", "volatile_no_addr"},
	{"persistent_correctable_data_error",       "correctable", "persistent_data"},
	{"persistent_uncorrectable_data_error",     "uncorrectable", "persistent_data"},
	{"persistent_uncorrectable_no_addr_error",  "uncorrectable", "persistent_no_addr"},
}

// CXLCollector implements prometheus.Collector for CXL memory devices.
type CXLCollector struct {
	opts        Options
	correctable *prometheus.Desc
	uncorrectable *prometheus.Desc
	collectorUp *prometheus.Desc
	scrapeTime  *prometheus.Desc
	scrapeErrs  *prometheus.Desc
}

// NewCXLCollector creates a new CXLCollector.
func NewCXLCollector(opts Options) *CXLCollector {
	return &CXLCollector{
		opts: opts,
		correctable: prometheus.NewDesc(
			"cxl_correctable_errors_total",
			"Total CXL correctable memory errors per device and error type.",
			[]string{"device", "error_type"}, nil,
		),
		uncorrectable: prometheus.NewDesc(
			"cxl_uncorrectable_errors_total",
			"Total CXL uncorrectable memory errors per device and error type.",
			[]string{"device", "error_type"}, nil,
		),
		collectorUp: prometheus.NewDesc(
			"fault_resilience_collector_up",
			"1 if the collector subsystem is accessible in sysfs, 0 if absent.",
			[]string{"collector"}, nil,
		),
		scrapeTime: prometheus.NewDesc(
			"hw_fault_exporter_scrape_duration_seconds",
			"Duration of the last scrape cycle for this collector.",
			[]string{"collector"}, nil,
		),
		scrapeErrs: prometheus.NewDesc(
			"hw_fault_exporter_scrape_errors_total",
			"Total sysfs read errors encountered by this collector.",
			[]string{"collector"}, nil,
		),
	}
}

// Describe implements prometheus.Collector.
func (c *CXLCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.correctable
	ch <- c.uncorrectable
	ch <- c.collectorUp
	ch <- c.scrapeTime
	ch <- c.scrapeErrs
}

// Collect implements prometheus.Collector.
func (c *CXLCollector) Collect(ch chan<- prometheus.Metric) {
	start := time.Now()
	errCount := 0

	cxlRoot := filepath.Join(c.opts.SysfsRoot, cxlBusRoot)
	devices, err := os.ReadDir(cxlRoot)
	if err != nil {
		c.opts.Logger.Warn("CXL: cannot read bus root", zap.String("path", cxlRoot), zap.Error(err))
		ch <- prometheus.MustNewConstMetric(c.collectorUp, prometheus.GaugeValue, 0, "cxl")
		emitSelf(ch, c.scrapeTime, c.scrapeErrs, "cxl", time.Since(start), 1)
		return
	}

	collected := 0
	for _, dev := range devices {
		name := dev.Name()
		// CXL memory devices are named mem<N>
		if !strings.HasPrefix(name, "mem") {
			continue
		}

		errDir := filepath.Join(cxlRoot, name, "error_counts")
		if _, err := os.Stat(errDir); err != nil {
			// error_counts/ absent on older kernels — silently skip
			continue
		}

		for _, sf := range cxlStatFiles {
			val, err := readUint64(filepath.Join(errDir, sf.name))
			if err != nil {
				errCount++
				continue
			}

			var desc *prometheus.Desc
			if sf.errorType == "correctable" {
				desc = c.correctable
			} else {
				desc = c.uncorrectable
			}
			ch <- prometheus.MustNewConstMetric(desc, prometheus.CounterValue,
				float64(val), name, sf.label)
		}
		collected++
	}

	ch <- prometheus.MustNewConstMetric(c.collectorUp, prometheus.GaugeValue, 1, "cxl")
	emitSelf(ch, c.scrapeTime, c.scrapeErrs, "cxl", time.Since(start), errCount)
	c.opts.Logger.Debug("CXL: collected", zap.Int("devices", collected), zap.Int("errors", errCount))
}
