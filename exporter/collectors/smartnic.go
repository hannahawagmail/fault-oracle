// SPDX-License-Identifier: Apache-2.0
//
// collectors/smartnic.go — Prometheus collector for SmartNIC/DPU interface statistics.
//
// Scrapes /sys/class/net/<iface>/statistics/* for each non-loopback interface,
// emitting per-interface counter and gauge metrics.
//
// Metric inventory:
//
//   smartnic_rx_bytes_total{iface}   — Total bytes received (counter)
//   smartnic_tx_bytes_total{iface}   — Total bytes transmitted (counter)
//   smartnic_rx_errors_total{iface}  — Total RX errors (counter)
//   smartnic_tx_errors_total{iface}  — Total TX errors (counter)
//   smartnic_rx_dropped_total{iface} — Total RX drops (counter)
//   smartnic_tx_dropped_total{iface} — Total TX drops (counter)
//   smartnic_link_up{iface}          — 1 if operstate=up, 0 otherwise (gauge)
//   smartnic_link_speed_mbps{iface}  — Link speed in Mbps, 0 if unknown (gauge)
//   fault_resilience_collector_up{collector="smartnic"} — sysfs availability

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

const sysNetRoot = "class/net"

// statFile maps a sysfs statistics file name to a Prometheus metric descriptor.
type statFile struct {
	name      string
	help      string
	valueType prometheus.ValueType
}

var nicStatFiles = []statFile{
	{"rx_bytes", "Total bytes received.", prometheus.CounterValue},
	{"tx_bytes", "Total bytes transmitted.", prometheus.CounterValue},
	{"rx_errors", "Total receive errors.", prometheus.CounterValue},
	{"tx_errors", "Total transmit errors.", prometheus.CounterValue},
	{"rx_dropped", "Total receive packets dropped.", prometheus.CounterValue},
	{"tx_dropped", "Total transmit packets dropped.", prometheus.CounterValue},
	{"rx_packets", "Total packets received.", prometheus.CounterValue},
	{"tx_packets", "Total packets transmitted.", prometheus.CounterValue},
	{"rx_missed_errors", "Total receive missed errors.", prometheus.CounterValue},
	{"collisions", "Total transmit collisions.", prometheus.CounterValue},
}

// SmartNICCollector implements prometheus.Collector for NIC statistics.
type SmartNICCollector struct {
	opts Options

	descs       map[string]*prometheus.Desc
	linkUp      *prometheus.Desc
	linkSpeed   *prometheus.Desc
	collectorUp *prometheus.Desc
	scrapeTime  *prometheus.Desc
	scrapeErrs  *prometheus.Desc
}

// NewSmartNICCollector creates a new SmartNIC collector.
func NewSmartNICCollector(opts Options) *SmartNICCollector {
	descs := make(map[string]*prometheus.Desc, len(nicStatFiles))
	for _, sf := range nicStatFiles {
		descs[sf.name] = prometheus.NewDesc(
			"smartnic_"+sf.name+"_total",
			sf.help,
			[]string{"iface"}, nil,
		)
	}
	return &SmartNICCollector{
		opts:  opts,
		descs: descs,
		linkUp: prometheus.NewDesc(
			"smartnic_link_up",
			"1 if the NIC interface is operstate=up, 0 otherwise.",
			[]string{"iface", "operstate"}, nil,
		),
		linkSpeed: prometheus.NewDesc(
			"smartnic_link_speed_mbps",
			"Interface speed in Mbps (0 if unknown or link down).",
			[]string{"iface"}, nil,
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
func (c *SmartNICCollector) Describe(ch chan<- *prometheus.Desc) {
	for _, d := range c.descs {
		ch <- d
	}
	ch <- c.linkUp
	ch <- c.linkSpeed
	ch <- c.collectorUp
	ch <- c.scrapeTime
	ch <- c.scrapeErrs
}

// Collect implements prometheus.Collector.
func (c *SmartNICCollector) Collect(ch chan<- prometheus.Metric) {
	start := time.Now()
	errCount := 0

	netRoot := filepath.Join(c.opts.SysfsRoot, sysNetRoot)
	ifaces, err := os.ReadDir(netRoot)
	if err != nil {
		c.opts.Logger.Warn("SmartNIC: cannot read net root", zap.String("path", netRoot), zap.Error(err))
		ch <- prometheus.MustNewConstMetric(c.collectorUp, prometheus.GaugeValue, 0, "smartnic")
		emitSelf(ch, c.scrapeTime, c.scrapeErrs, "smartnic", time.Since(start), 1)
		return
	}

	collected := 0
	for _, iface := range ifaces {
		name := iface.Name()

		// Skip loopback
		if name == "lo" {
			continue
		}

		statsDir := filepath.Join(netRoot, name, "statistics")
		if _, err := os.Stat(statsDir); err != nil {
			continue  // virtual or statistics-less interface
		}

		// Per-stat counters
		for _, sf := range nicStatFiles {
			val, err := readUint64(filepath.Join(statsDir, sf.name))
			if err != nil {
				errCount++
				continue
			}
			ch <- prometheus.MustNewConstMetric(
				c.descs[sf.name], sf.valueType, float64(val), name)
		}

		// operstate
		operstate := readString(filepath.Join(netRoot, name, "operstate"))
		linkUpVal := 0.0
		if operstate == "up" {
			linkUpVal = 1.0
		}
		ch <- prometheus.MustNewConstMetric(c.linkUp, prometheus.GaugeValue, linkUpVal, name, operstate)

		// speed
		speedStr := readString(filepath.Join(netRoot, name, "speed"))
		speed, _ := strconv.ParseFloat(strings.TrimSpace(speedStr), 64)
		if speed < 0 {
			speed = 0  // kernel returns -1 when link is down
		}
		ch <- prometheus.MustNewConstMetric(c.linkSpeed, prometheus.GaugeValue, speed, name)

		collected++
	}

	ch <- prometheus.MustNewConstMetric(c.collectorUp, prometheus.GaugeValue, 1, "smartnic")
	emitSelf(ch, c.scrapeTime, c.scrapeErrs, "smartnic", time.Since(start), errCount)
	c.opts.Logger.Debug("SmartNIC: collected", zap.Int("interfaces", collected), zap.Int("errors", errCount))
}

// readString reads the first line of a sysfs file, returning empty string on error.
func readString(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}
