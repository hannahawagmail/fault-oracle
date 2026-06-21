// SPDX-License-Identifier: Apache-2.0
//
// collectors/self.go — Exporter self-health metrics.
//
// Emits build info and uptime for the exporter itself, enabling
// "is the exporter running and what version?" queries in Prometheus.

package collectors

import (
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

// SelfCollector emits exporter build info and uptime.
type SelfCollector struct {
	startTime time.Time
	version   string

	buildInfo *prometheus.Desc
	uptime    *prometheus.Desc
}

// NewSelfCollector creates a SelfCollector with the given version string.
func NewSelfCollector(version string) *SelfCollector {
	return &SelfCollector{
		startTime: time.Now(),
		version:   version,
		buildInfo: prometheus.NewDesc(
			"hw_fault_exporter_build_info",
			"Build information for the hardware fault exporter. Value is always 1.",
			[]string{"version"},
			nil,
		),
		uptime: prometheus.NewDesc(
			"hw_fault_exporter_uptime_seconds",
			"Seconds since the hardware fault exporter started.",
			nil, nil,
		),
	}
}

// Describe implements prometheus.Collector.
func (s *SelfCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- s.buildInfo
	ch <- s.uptime
}

// Collect implements prometheus.Collector.
func (s *SelfCollector) Collect(ch chan<- prometheus.Metric) {
	ch <- prometheus.MustNewConstMetric(s.buildInfo, prometheus.GaugeValue, 1, s.version)
	ch <- prometheus.MustNewConstMetric(s.uptime, prometheus.GaugeValue,
		time.Since(s.startTime).Seconds())
}
