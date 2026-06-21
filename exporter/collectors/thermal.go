// SPDX-License-Identifier: Apache-2.0
//
// collectors/thermal.go — CPU thermal zone + cooling device collector.
//
// Reads /sys/class/thermal/thermal_zone<N>/ and /sys/class/thermal/cooling_device<N>/
// emitting per-zone temperature and cooling device state as Prometheus metrics.
//
// Metrics:
//   cpu_thermal_zone_celsius{zone, zone_type}   — current temperature in °C
//   cpu_cooling_device_state{device, device_type} — current cooling level (0=off)
//   fault_resilience_collector_up{collector="thermal"}

package collectors

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"go.uber.org/zap"
)

const thermalRoot = "class/thermal"

// ThermalCollector implements prometheus.Collector for thermal zones and cooling devices.
type ThermalCollector struct {
	opts         Options
	zoneTemp     *prometheus.Desc
	coolingState *prometheus.Desc
	collectorUp  *prometheus.Desc
	scrapeTime   *prometheus.Desc
	scrapeErrs   *prometheus.Desc
}

// NewThermalCollector creates a ThermalCollector that reads thermal zones and
// cooling devices from /sys/class/thermal/ under opts.SysfsRoot.
func NewThermalCollector(opts Options) *ThermalCollector {
	return &ThermalCollector{
		opts: opts,
		zoneTemp: prometheus.NewDesc(
			"cpu_thermal_zone_celsius",
			"Current temperature of a thermal zone in degrees Celsius.",
			[]string{"zone", "zone_type"}, nil,
		),
		coolingState: prometheus.NewDesc(
			"cpu_cooling_device_state",
			"Current state of a cooling device (0 = off / minimum cooling).",
			[]string{"device", "device_type"}, nil,
		),
		collectorUp: prometheus.NewDesc(
			"fault_resilience_collector_up",
			"1 if the collector subsystem is accessible in sysfs, 0 if absent.",
			[]string{"collector"}, nil,
		),
		scrapeTime: prometheus.NewDesc(
			"hw_fault_exporter_scrape_duration_seconds",
			"Duration of the last scrape cycle.", []string{"collector"}, nil,
		),
		scrapeErrs: prometheus.NewDesc(
			"hw_fault_exporter_scrape_errors_total",
			"Total sysfs read errors.", []string{"collector"}, nil,
		),
	}
}

// Describe implements prometheus.Collector.
func (c *ThermalCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.zoneTemp
	ch <- c.coolingState
	ch <- c.collectorUp
	ch <- c.scrapeTime
	ch <- c.scrapeErrs
}

// Collect implements prometheus.Collector. Reads all thermal_zone<N> and
// cooling_device<N> entries under /sys/class/thermal/ and emits temperature
// (millidegrees converted to Celsius) and cooling-device state gauges.
func (c *ThermalCollector) Collect(ch chan<- prometheus.Metric) {
	start := time.Now()
	errCount := 0

	thermalDir := filepath.Join(c.opts.SysfsRoot, thermalRoot)
	entries, err := os.ReadDir(thermalDir)
	if err != nil {
		c.opts.Logger.Warn("Thermal: cannot read thermal root",
			zap.String("path", thermalDir), zap.Error(err))
		ch <- prometheus.MustNewConstMetric(c.collectorUp, prometheus.GaugeValue, 0, "thermal")
		emitSelf(ch, c.scrapeTime, c.scrapeErrs, "thermal", time.Since(start), 1)
		return
	}

	for _, entry := range entries {
		name := entry.Name()
		base := filepath.Join(thermalDir, name)

		if strings.HasPrefix(name, "thermal_zone") {
			// Read temperature (in millidegrees Celsius → convert to °C)
			tempRaw, err := readUint64(filepath.Join(base, "temp"))
			if err != nil {
				errCount++
				continue
			}
			zoneType := readString(filepath.Join(base, "type"))
			if zoneType == "" {
				zoneType = "unknown"
			}
			celsius := float64(tempRaw) / 1000.0
			ch <- prometheus.MustNewConstMetric(
				c.zoneTemp, prometheus.GaugeValue, celsius, name, zoneType)

		} else if strings.HasPrefix(name, "cooling_device") {
			curState := readString(filepath.Join(base, "cur_state"))
			devType  := readString(filepath.Join(base, "type"))
			if devType == "" {
				devType = "unknown"
			}
			val, err := strconv.ParseFloat(strings.TrimSpace(curState), 64)
			if err != nil {
				errCount++
				continue
			}
			ch <- prometheus.MustNewConstMetric(
				c.coolingState, prometheus.GaugeValue, val, name, devType)
		}
	}

	ch <- prometheus.MustNewConstMetric(c.collectorUp, prometheus.GaugeValue, 1, "thermal")
	emitSelf(ch, c.scrapeTime, c.scrapeErrs, "thermal", time.Since(start), errCount)
}
