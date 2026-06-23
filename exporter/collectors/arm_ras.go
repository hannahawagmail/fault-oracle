// SPDX-License-Identifier: Apache-2.0
//
// collectors/arm_ras.go — Prometheus collector for ARM RAS Extension error counters.
//
// Reads /sys/devices/platform/arm-ras.*/  sysfs entries exposed by ARM servers
// with RAS Extension support. Each device directory contains:
//   correctable_count   — cumulative correctable errors for the RAS unit
//   uncorrectable_count — cumulative uncorrectable errors
//   deferred_count      — cumulative deferred errors
//
// Metrics emitted:
//   arm_ras_correctable_total{unit}
//   arm_ras_uncorrectable_total{unit}
//   arm_ras_deferred_total{unit}
//   fault_resilience_collector_up{collector="arm_ras"}

package collectors

import (
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"go.uber.org/zap"
)

const armRASPlatformRoot = "devices/platform"

// ARMRASCollector reads ARM RAS error counters from sysfs.
type ARMRASCollector struct {
	opts          Options
	correctable   *prometheus.Desc
	uncorrectable *prometheus.Desc
	deferred      *prometheus.Desc
}

// NewARMRASCollector creates a new ARMRASCollector.
func NewARMRASCollector(opts Options) *ARMRASCollector {
	return &ARMRASCollector{
		opts: opts,
		correctable: prometheus.NewDesc(
			"arm_ras_correctable_total",
			"Total ARM RAS correctable errors per unit.",
			[]string{"unit"}, nil,
		),
		uncorrectable: prometheus.NewDesc(
			"arm_ras_uncorrectable_total",
			"Total ARM RAS uncorrectable errors per unit.",
			[]string{"unit"}, nil,
		),
		deferred: prometheus.NewDesc(
			"arm_ras_deferred_total",
			"Total ARM RAS deferred errors per unit.",
			[]string{"unit"}, nil,
		),
	}
}

// Describe implements prometheus.Collector.
func (c *ARMRASCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.correctable
	ch <- c.uncorrectable
	ch <- c.deferred
	ch <- collectorUpDesc
}

// Collect implements prometheus.Collector.
func (c *ARMRASCollector) Collect(ch chan<- prometheus.Metric) {
	start := time.Now()
	platRoot := filepath.Join(c.opts.SysfsRoot, armRASPlatformRoot)

	entries, err := os.ReadDir(platRoot)
	if err != nil {
		c.opts.Logger.Debug("ARM RAS: platform root not found", zap.String("path", platRoot), zap.Error(err))
		ch <- prometheus.MustNewConstMetric(collectorUpDesc, prometheus.GaugeValue, 0, "arm_ras")
		return
	}

	collected := 0
	for _, e := range entries {
		name := e.Name()
		if !strings.HasPrefix(name, "arm-ras.") {
			continue
		}
		// Unit name: strip "arm-ras." prefix (e.g., "arm-ras.cpu0-l1" → "cpu0-l1")
		unit := strings.TrimPrefix(name, "arm-ras.")
		devDir := filepath.Join(platRoot, name)

		for _, stat := range []struct {
			file string
			desc *prometheus.Desc
		}{
			{"correctable_count", c.correctable},
			{"uncorrectable_count", c.uncorrectable},
			{"deferred_count", c.deferred},
		} {
			val, err := readUint64(filepath.Join(devDir, stat.file))
			if err != nil {
				continue
			}
			ch <- prometheus.MustNewConstMetric(stat.desc, prometheus.CounterValue, float64(val), unit)
		}
		collected++
	}

	up := float64(0)
	if collected > 0 {
		up = 1
	}
	ch <- prometheus.MustNewConstMetric(collectorUpDesc, prometheus.GaugeValue, up, "arm_ras")
	_ = time.Since(start)
	c.opts.Logger.Debug("ARM RAS: collected", zap.Int("units", collected))
}
