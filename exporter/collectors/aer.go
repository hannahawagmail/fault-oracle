// SPDX-License-Identifier: Apache-2.0
//
// collectors/aer.go — Prometheus collector for PCIe AER error counters.
//
// Scrapes /sys/bus/pci/devices/<BDF>/aer_dev_correctable,
//         /sys/bus/pci/devices/<BDF>/aer_dev_nonfatal, and
//         /sys/bus/pci/devices/<BDF>/aer_dev_fatal
// for each PCIe device that has AER capability, and emits per-device,
// per-error-type counters.
//
// Metric inventory:
//
//   pcie_aer_correctable_total{device, error_type}
//     — Cumulative correctable AER errors. Source: aer_dev_correctable.
//
//   pcie_aer_uncorrectable_total{device, error_type, severity}
//     — Cumulative uncorrectable AER errors. Severity label: nonfatal | fatal.
//     Source: aer_dev_nonfatal and aer_dev_fatal.
//
//   pcie_aer_devices_total
//     — Number of PCIe devices with AER capability currently visible.
//
//   hw_fault_exporter_scrape_duration_seconds{collector="aer"}
//   hw_fault_exporter_scrape_errors_total{collector="aer"}
//
//   fault_resilience_collector_up{collector="aer"}
//     — 1 if the PCI devices sysfs root is accessible, 0 if absent.

package collectors

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"go.uber.org/zap"
)

const pciDevicesRoot = "bus/pci/devices"

// AERCollector implements prometheus.Collector for PCIe AER.
type AERCollector struct {
	opts Options

	corrTotal      *prometheus.Desc
	uncorrTotal    *prometheus.Desc
	devicesTotal   *prometheus.Desc
	scrapeDuration *prometheus.Desc
	scrapeErrors   *prometheus.Desc
	// collectorUp uses the shared collectorUpDesc (see shared.go) to avoid a
	// duplicate-descriptor panic when EDAC, AER and MCE are all registered.
}

// NewAERCollector creates a new AER collector.
func NewAERCollector(opts Options) *AERCollector {
	const ns = "pcie_aer"
	return &AERCollector{
		opts: opts,
		corrTotal: prometheus.NewDesc(
			prometheus.BuildFQName(ns, "", "correctable_total"),
			"Total PCIe AER correctable errors per device and error type since boot.",
			[]string{"device", "error_type"},
			nil,
		),
		uncorrTotal: prometheus.NewDesc(
			prometheus.BuildFQName(ns, "", "uncorrectable_total"),
			"Total PCIe AER uncorrectable errors per device, error type, and severity since boot.",
			[]string{"device", "error_type", "severity"},
			nil,
		),
		devicesTotal: prometheus.NewDesc(
			prometheus.BuildFQName(ns, "", "devices_total"),
			"Number of PCIe devices with AER capability visible in sysfs.",
			nil, nil,
		),
		scrapeDuration: scrapeDurationDesc,
		scrapeErrors:   scrapeErrorsDesc,
	}
}

// Describe implements prometheus.Collector.
func (c *AERCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.corrTotal
	ch <- c.uncorrTotal
	ch <- c.devicesTotal
}

// Collect implements prometheus.Collector.
func (c *AERCollector) Collect(ch chan<- prometheus.Metric) {
	start := time.Now()
	errCount := 0
	devCount := 0

	devRoot := filepath.Join(c.opts.SysfsRoot, pciDevicesRoot)
	entries, err := os.ReadDir(devRoot)
	if err != nil {
		c.opts.Logger.Warn("AER: cannot read PCI devices root",
			zap.String("path", devRoot), zap.Error(err))
		errCount++
		ch <- prometheus.MustNewConstMetric(collectorUpDesc, prometheus.GaugeValue, 0, "aer")
		emitSelf(ch, c.scrapeDuration, c.scrapeErrors, "aer", time.Since(start), errCount)
		return
	}

	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		devPath := filepath.Join(devRoot, entry.Name())
		bdf := entry.Name() // e.g., "0000:01:00.0"

		// Check if this device has AER capability
		corrPath := filepath.Join(devPath, "aer_dev_correctable")
		if _, err := os.Stat(corrPath); os.IsNotExist(err) {
			continue // No AER capability — skip
		}
		devCount++

		// Scrape correctable errors
		corrErrors, err := parseAERFile(corrPath)
		if err != nil {
			c.opts.Logger.Debug("AER: parse correctable",
				zap.String("device", bdf), zap.Error(err))
			errCount++
		} else {
			for errType, count := range corrErrors {
				if errType == "TOTAL_ERR_COR" {
					continue // Skip the total — we emit per-type metrics
				}
				ch <- prometheus.MustNewConstMetric(c.corrTotal,
					prometheus.CounterValue, float64(count), bdf, errType)
			}
		}

		// Scrape non-fatal uncorrectable errors
		nonfatalPath := filepath.Join(devPath, "aer_dev_nonfatal")
		if _, err := os.Stat(nonfatalPath); err == nil {
			nonfatalErrors, err := parseAERFile(nonfatalPath)
			if err != nil {
				c.opts.Logger.Debug("AER: parse nonfatal",
					zap.String("device", bdf), zap.Error(err))
				errCount++
			} else {
				for errType, count := range nonfatalErrors {
					if errType == "TOTAL_ERR_UNCOR" {
						continue
					}
					ch <- prometheus.MustNewConstMetric(c.uncorrTotal,
						prometheus.CounterValue, float64(count), bdf, errType, "nonfatal")
				}
			}
		}

		// Scrape fatal uncorrectable errors
		fatalPath := filepath.Join(devPath, "aer_dev_fatal")
		if _, err := os.Stat(fatalPath); err == nil {
			fatalErrors, err := parseAERFile(fatalPath)
			if err != nil {
				c.opts.Logger.Debug("AER: parse fatal",
					zap.String("device", bdf), zap.Error(err))
				errCount++
			} else {
				for errType, count := range fatalErrors {
					if errType == "TOTAL_ERR_UNCOR" {
						continue
					}
					ch <- prometheus.MustNewConstMetric(c.uncorrTotal,
						prometheus.CounterValue, float64(count), bdf, errType, "fatal")
				}
			}
		}
	}

	ch <- prometheus.MustNewConstMetric(c.devicesTotal, prometheus.GaugeValue, float64(devCount))
	ch <- prometheus.MustNewConstMetric(collectorUpDesc, prometheus.GaugeValue, 1, "aer")
	emitSelf(ch, c.scrapeDuration, c.scrapeErrors, "aer", time.Since(start), errCount)
}

// parseAERFile parses an AER sysfs file into a map of error_name → count.
//
// The file format (from kernel drivers/pci/pcie/aer.c) is:
//
//	RxErr 0
//	BadTLP 3
//	BadDLLP 0
//	...
//	TOTAL_ERR_COR 3
func parseAERFile(path string) (map[string]uint64, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", path, err)
	}
	defer f.Close()

	result := make(map[string]uint64)
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		parts := strings.Fields(line)
		if len(parts) != 2 {
			continue // Unexpected format — skip
		}
		name := parts[0]
		count, err := strconv.ParseUint(parts[1], 10, 64)
		if err != nil {
			continue // Non-numeric count — skip
		}
		result[name] = count
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("scan %s: %w", path, err)
	}
	return result, nil
}
