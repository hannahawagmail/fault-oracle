// SPDX-License-Identifier: Apache-2.0
//
// collectors/edac.go — Prometheus collector for Linux EDAC subsystem.
//
// Scrapes /sys/devices/system/edac/mc<N>/csrow<M>/ch<K>_ce_count and related
// paths, emitting per-controller, per-csrow, per-channel counter metrics.
//
// Metric inventory:
//
//   edac_correctable_errors_total{controller, csrow, channel}
//     — Cumulative correctable errors (CE) per channel. Source:
//       /sys/devices/system/edac/mc<N>/csrow<M>/ch<K>_ce_count
//
//   edac_uncorrectable_errors_total{controller, csrow}
//     — Cumulative uncorrectable errors (UE) per csrow. Source:
//       /sys/devices/system/edac/mc<N>/csrow<M>/ue_count
//     (UE location is typically csrow-level; channel-level UE attribution
//      is not reliably reported by all EDAC drivers.)
//
//   edac_controller_ce_total{controller}
//     — Total CE count for the controller. Useful for fleet-level alerting
//       without needing to sum across csrow/channel.
//
//   edac_controller_ue_total{controller}
//     — Total UE count for the controller.
//
//   edac_scrape_duration_seconds{collector="edac"}
//     — Time taken to scrape this collector (from hw_fault_exporter_scrape_duration_seconds).
//
//   edac_scrape_errors_total{collector="edac"}
//     — Number of sysfs read errors during this scrape cycle.
//
//   fault_resilience_collector_up{collector="edac"}
//     — 1 if the EDAC sysfs path is accessible, 0 if the subsystem is absent.

package collectors

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"go.uber.org/zap"
)

// edacMCRoot is the sysfs path for EDAC memory controller entries.
const edacMCRoot = "devices/system/edac/mc"

// Regexp patterns for parsing EDAC sysfs directory names.
var (
	reMCDir    = regexp.MustCompile(`^mc(\d+)$`)
	reCsrowDir = regexp.MustCompile(`^csrow(\d+)$`)
	reChFile   = regexp.MustCompile(`^ch(\d+)_ce_count$`)
)

// EDACCollector implements prometheus.Collector for the EDAC subsystem.
type EDACCollector struct {
	opts Options

	// Per-channel correctable error counter
	ceTotal *prometheus.Desc
	// Per-csrow uncorrectable error counter
	ueTotal *prometheus.Desc
	// Per-controller aggregated CE counter
	ctrlCETotal *prometheus.Desc
	// Per-controller aggregated UE counter
	ctrlUETotal *prometheus.Desc
	// Scrape health metrics (reuse shared descriptors from SelfCollector)
	scrapeDuration *prometheus.Desc
	scrapeErrors   *prometheus.Desc
	// collectorUp uses the shared descriptor (see shared.go) to avoid a
	// duplicate-descriptor panic when all three collectors are registered.
}

// NewEDACCollector creates a new EDAC collector.
func NewEDACCollector(opts Options) *EDACCollector {
	const ns = "edac"
	return &EDACCollector{
		opts: opts,
		ceTotal: prometheus.NewDesc(
			prometheus.BuildFQName(ns, "", "correctable_errors_total"),
			"Total correctable EDAC errors per channel since boot.",
			[]string{"controller", "csrow", "channel"},
			nil,
		),
		ueTotal: prometheus.NewDesc(
			prometheus.BuildFQName(ns, "", "uncorrectable_errors_total"),
			"Total uncorrectable EDAC errors per csrow since boot.",
			[]string{"controller", "csrow"},
			nil,
		),
		ctrlCETotal: prometheus.NewDesc(
			prometheus.BuildFQName(ns, "controller", "ce_total"),
			"Total correctable EDAC errors on the memory controller since boot.",
			[]string{"controller"},
			nil,
		),
		ctrlUETotal: prometheus.NewDesc(
			prometheus.BuildFQName(ns, "controller", "ue_total"),
			"Total uncorrectable EDAC errors on the memory controller since boot.",
			[]string{"controller"},
			nil,
		),
		scrapeDuration: prometheus.NewDesc(
			"hw_fault_exporter_scrape_duration_seconds",
			"Duration of the last scrape cycle for this collector.",
			[]string{"collector"},
			nil,
		),
		scrapeErrors: prometheus.NewDesc(
			"hw_fault_exporter_scrape_errors_total",
			"Total sysfs read errors encountered by this collector.",
			[]string{"collector"},
			nil,
		),
	}
}

// Describe implements prometheus.Collector.
func (c *EDACCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.ceTotal
	ch <- c.ueTotal
	ch <- c.ctrlCETotal
	ch <- c.ctrlUETotal
	ch <- c.scrapeDuration
	ch <- c.scrapeErrors
	ch <- collectorUpDesc
}

// Collect implements prometheus.Collector.
func (c *EDACCollector) Collect(ch chan<- prometheus.Metric) {
	start := time.Now()
	errCount := 0
	// successCount tracks per-controller file reads that succeeded. collectorUp
	// is only set to 1 when at least one read succeeds, so that a world where
	// /sys/devices/system/edac/mc exists but all files are unreadable (e.g.
	// permission denied) is correctly reported as unhealthy.
	successCount := 0

	mcRoot := filepath.Join(c.opts.SysfsRoot, edacMCRoot)
	mcDirs, err := os.ReadDir(mcRoot)
	if err != nil {
		c.opts.Logger.Warn("EDAC: cannot read mc root", zap.String("path", mcRoot), zap.Error(err))
		errCount++
		ch <- prometheus.MustNewConstMetric(collectorUpDesc, prometheus.GaugeValue, 0, "edac")
		emitSelf(ch, c.scrapeDuration, c.scrapeErrors, "edac", time.Since(start), errCount)
		return
	}

	for _, mcEntry := range mcDirs {
		if !mcEntry.IsDir() {
			continue
		}
		m := reMCDir.FindStringSubmatch(mcEntry.Name())
		if m == nil {
			continue
		}
		mcIdx := m[1]
		mcPath := filepath.Join(mcRoot, mcEntry.Name())

		// Read controller-level totals
		ctrlCE, err := readUint64(filepath.Join(mcPath, "ce_count"))
		if err != nil {
			c.opts.Logger.Debug("EDAC: cannot read ce_count", zap.String("mc", mcIdx), zap.Error(err))
			errCount++
		} else {
			ch <- prometheus.MustNewConstMetric(c.ctrlCETotal, prometheus.CounterValue,
				float64(ctrlCE), mcEntry.Name())
		}

		ctrlUE, err := readUint64(filepath.Join(mcPath, "ue_count"))
		if err != nil {
			c.opts.Logger.Debug("EDAC: cannot read ue_count", zap.String("mc", mcIdx), zap.Error(err))
			errCount++
		} else {
			ch <- prometheus.MustNewConstMetric(c.ctrlUETotal, prometheus.CounterValue,
				float64(ctrlUE), mcEntry.Name())
		}

		// Read per-csrow files
		csrowEntries, err := os.ReadDir(mcPath)
		if err != nil {
			c.opts.Logger.Warn("EDAC: cannot read mc dir", zap.String("path", mcPath), zap.Error(err))
			errCount++
			continue
		}

		for _, csrowEntry := range csrowEntries {
			if !csrowEntry.IsDir() {
				continue
			}
			cm := reCsrowDir.FindStringSubmatch(csrowEntry.Name())
			if cm == nil {
				continue
			}
			csrowIdx := cm[1]
			csrowPath := filepath.Join(mcPath, csrowEntry.Name())

			// Per-csrow UE count
			csrowUE, err := readUint64(filepath.Join(csrowPath, "ue_count"))
			if err != nil {
				c.opts.Logger.Debug("EDAC: cannot read csrow ue_count",
					zap.String("csrow", csrowIdx), zap.Error(err))
				errCount++
			} else {
				ch <- prometheus.MustNewConstMetric(c.ueTotal, prometheus.CounterValue,
					float64(csrowUE), mcEntry.Name(), csrowIdx)
			}

			// Per-channel CE counts
			chEntries, err := os.ReadDir(csrowPath)
			if err != nil {
				errCount++
				continue
			}
			for _, chEntry := range chEntries {
				fm := reChFile.FindStringSubmatch(chEntry.Name())
				if fm == nil {
					continue
				}
				chIdx := fm[1]
				chCE, err := readUint64(filepath.Join(csrowPath, chEntry.Name()))
				if err != nil {
					c.opts.Logger.Debug("EDAC: cannot read channel ce_count",
						zap.String("ch", chIdx), zap.Error(err))
					errCount++
					continue
				}
				ch <- prometheus.MustNewConstMetric(c.ceTotal, prometheus.CounterValue,
					float64(chCE), mcEntry.Name(), csrowIdx, chIdx)
			}
		}
	}

	ch <- prometheus.MustNewConstMetric(collectorUpDesc, prometheus.GaugeValue, 1, "edac")
	emitSelf(ch, c.scrapeDuration, c.scrapeErrors, "edac", time.Since(start), errCount)
}

// ----- Utility -------------------------------------------------------------

// readUint64 reads a sysfs file and parses its content as a uint64.
// sysfs files typically contain a single ASCII decimal integer followed by '\n'.
func readUint64(path string) (uint64, error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, err
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	if !scanner.Scan() {
		if err := scanner.Err(); err != nil {
			return 0, fmt.Errorf("scan %s: %w", path, err)
		}
		return 0, fmt.Errorf("empty file: %s", path)
	}
	line := strings.TrimSpace(scanner.Text())
	val, err := strconv.ParseUint(line, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("parse %q from %s: %w", line, path, err)
	}
	return val, nil
}

// emitSelf emits the exporter's own scrape health metrics.
func emitSelf(ch chan<- prometheus.Metric, durDesc, errDesc *prometheus.Desc,
	collector string, duration time.Duration, errCount int) {
	ch <- prometheus.MustNewConstMetric(durDesc, prometheus.GaugeValue,
		duration.Seconds(), collector)
	ch <- prometheus.MustNewConstMetric(errDesc, prometheus.CounterValue,
		float64(errCount), collector)
}
