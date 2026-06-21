// SPDX-License-Identifier: Apache-2.0
//
// collectors/mce.go — Prometheus collector for Machine Check Events.
//
// On ARM64 servers with ACPI APEI and RAS Extension support, machine check
// events arrive via the GHES (Generic Hardware Error Source) driver and are
// surfaced through /sys/firmware/acpi/errors/ and kernel tracepoints.
//
// On x86 systems, MCE events are read from /dev/mcelog.
//
// This collector tries multiple sources in order and uses the first that
// is available. On a system with neither GHES nor mcelog, all MCE metrics
// will report 0 with a warning logged.
//
// Metric inventory:
//
//   mce_events_total{severity, bank, source}
//     — Total machine check events by severity and bank (register bank ID).
//     Severity: corrected | deferred | uncorrected | panic.
//     Source: ghes | mcelog | none.
//
//   mce_available
//     — 1 if an MCE data source was found, 0 otherwise.
//     Useful for alerting on exporter misconfiguration.
//
//   hw_fault_exporter_scrape_duration_seconds{collector="mce"}
//   hw_fault_exporter_scrape_errors_total{collector="mce"}
//
//   fault_resilience_collector_up{collector="mce"}
//     — 1 if at least one MCE data source (GHES, mcelog, or RAS counters) is accessible, 0 otherwise.

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

// MCECollector implements prometheus.Collector for machine check events.
type MCECollector struct {
	opts Options

	mceTotal       *prometheus.Desc
	mceAvailable   *prometheus.Desc
	scrapeDuration *prometheus.Desc
	scrapeErrors   *prometheus.Desc
	collectorUp    *prometheus.Desc
}

// mceEvent represents a parsed machine check event entry.
type mceEvent struct {
	severity string // corrected, deferred, uncorrected, panic
	bank     string // integer bank ID as string, or "unknown"
	source   string // ghes, mcelog
	count    uint64
}

// NewMCECollector creates a new MCE collector.
func NewMCECollector(opts Options) *MCECollector {
	const ns = "mce"
	return &MCECollector{
		opts: opts,
		mceTotal: prometheus.NewDesc(
			prometheus.BuildFQName(ns, "", "events_total"),
			"Total machine check events by severity, bank, and source since boot.",
			[]string{"severity", "bank", "source"},
			nil,
		),
		mceAvailable: prometheus.NewDesc(
			prometheus.BuildFQName(ns, "", "available"),
			"1 if an MCE data source is available (GHES or mcelog), 0 otherwise.",
			nil, nil,
		),
		scrapeDuration: prometheus.NewDesc(
			"hw_fault_exporter_scrape_duration_seconds",
			"Duration of the last scrape cycle for this collector.",
			[]string{"collector"}, nil,
		),
		scrapeErrors: prometheus.NewDesc(
			"hw_fault_exporter_scrape_errors_total",
			"Total sysfs read errors encountered by this collector.",
			[]string{"collector"}, nil,
		),
		collectorUp: prometheus.NewDesc(
			"fault_resilience_collector_up",
			"1 if the collector subsystem is accessible in sysfs, 0 if absent.",
			[]string{"collector"},
			nil,
		),
	}
}

// Describe implements prometheus.Collector.
func (c *MCECollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.mceTotal
	ch <- c.mceAvailable
	ch <- c.scrapeDuration
	ch <- c.scrapeErrors
	ch <- c.collectorUp
}

// Collect implements prometheus.Collector.
func (c *MCECollector) Collect(ch chan<- prometheus.Metric) {
	start := time.Now()
	errCount := 0

	var events []mceEvent
	var available float64

	// Try GHES first (ARM64 APEI path)
	ghesEvents, err := c.scrapeGHES()
	if err == nil && len(ghesEvents) > 0 {
		events = ghesEvents
		available = 1
		c.opts.Logger.Debug("MCE: using GHES source", zap.Int("events", len(events)))
	} else {
		// Fall back to mcelog (x86 / legacy path)
		mcelogEvents, err := c.scrapeMcelog()
		if err == nil && len(mcelogEvents) > 0 {
			events = mcelogEvents
			available = 1
			c.opts.Logger.Debug("MCE: using mcelog source", zap.Int("events", len(events)))
		} else {
			// Try the rasdaemon-style sysfs counter path
			rasEvents, err := c.scrapeRASCounters()
			if err == nil && len(rasEvents) > 0 {
				events = rasEvents
				available = 1
				c.opts.Logger.Debug("MCE: using RAS counters source", zap.Int("events", len(events)))
			} else {
				c.opts.Logger.Debug("MCE: no data source available (GHES, mcelog, RAS all unavailable)")
				available = 0
				errCount++
			}
		}
	}

	// Emit events
	for _, ev := range events {
		ch <- prometheus.MustNewConstMetric(c.mceTotal, prometheus.CounterValue,
			float64(ev.count), ev.severity, ev.bank, ev.source)
	}

	ch <- prometheus.MustNewConstMetric(c.mceAvailable, prometheus.GaugeValue, available)
	ch <- prometheus.MustNewConstMetric(c.collectorUp, prometheus.GaugeValue, available, "mce")
	emitSelf(ch, c.scrapeDuration, c.scrapeErrors, "mce", time.Since(start), errCount)
}

// scrapeGHES reads ACPI APEI GHES error records from sysfs.
//
// On ARM64 servers, the GHES driver creates files under:
//   /sys/firmware/acpi/errors/
//
// The rasdaemon project also exports a summary via:
//   /sys/bus/platform/devices/GHES:0/*/error_count  (implementation-specific)
//
// For a more portable approach, we read the kernel's ras:mc_event tracepoint
// counts from /sys/kernel/debug/tracing/events/ras/mc_event/enable — but this
// requires debugfs and the tracing subsystem. The simplest reliable path is to
// check for the GHES sysfs directory and return empty if not present.
func (c *MCECollector) scrapeGHES() ([]mceEvent, error) {
	ghesPath := filepath.Join(c.opts.SysfsRoot, "firmware", "acpi", "errors")
	entries, err := os.ReadDir(ghesPath)
	if err != nil {
		return nil, fmt.Errorf("GHES path not found: %w", err)
	}

	// The GHES sysfs interface exposes files named by error severity:
	//   corrected_errors, deferred_errors, uncorrected_errors
	// Each file contains a count.
	var events []mceEvent
	severityFiles := map[string]string{
		"corrected":   "corrected_errors",
		"deferred":    "deferred_errors",
		"uncorrected": "uncorrected_errors",
	}
	_ = entries // entries used for directory existence check above

	for severity, fname := range severityFiles {
		fpath := filepath.Join(ghesPath, fname)
		count, err := readUint64(fpath)
		if err != nil {
			continue // File may not exist on all platforms
		}
		events = append(events, mceEvent{
			severity: severity,
			bank:     "unknown", // GHES doesn't expose bank-level granularity
			source:   "ghes",
			count:    count,
		})
	}

	return events, nil
}

// scrapeMcelog reads machine check events from /dev/mcelog (x86).
//
// mcelog output format (simplified):
//
//	Hardware event. This is not a software error.
//	MCE 0
//	CPU 0 BANK 0 TSC <value>
//	STATUS 0x<hex> MCGSTATUS 0x<hex>
//	...
//
// We parse the BANK field to extract bank IDs and count events by severity
// (decoded from STATUS bits where bit 63 = VAL, bit 61 = UC, bit 57 = PCC).
var reBank = regexp.MustCompile(`BANK (\d+)`)
var reStatus = regexp.MustCompile(`STATUS 0x([0-9a-fA-F]+)`)

func (c *MCECollector) scrapeMcelog() ([]mceEvent, error) {
	mcelogPath := "/dev/mcelog"
	f, err := os.Open(mcelogPath)
	if err != nil {
		return nil, fmt.Errorf("mcelog not available: %w", err)
	}
	defer f.Close()

	// bank → severity → count
	type key struct{ bank, severity string }
	counts := make(map[key]uint64)

	var currentBank string
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()

		if m := reBank.FindStringSubmatch(line); m != nil {
			currentBank = m[1]
		}

		if m := reStatus.FindStringSubmatch(line); m != nil && currentBank != "" {
			status, err := strconv.ParseUint(m[1], 16, 64)
			if err != nil {
				continue
			}
			severity := decodeMCESeverity(status)
			counts[key{currentBank, severity}]++
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("mcelog scan: %w", err)
	}

	var events []mceEvent
	for k, count := range counts {
		events = append(events, mceEvent{
			severity: k.severity,
			bank:     k.bank,
			source:   "mcelog",
			count:    count,
		})
	}
	return events, nil
}

// decodeMCESeverity decodes MCE STATUS register severity.
// STATUS bit layout (Intel/AMD MCE; approximated for ARM64 RAS error records):
//   Bit 63: VAL — entry is valid
//   Bit 61: UC  — uncorrected error
//   Bit 57: PCC — processor context corrupt (panic-level)
func decodeMCESeverity(status uint64) string {
	const (
		bitUC  = 1 << 61
		bitPCC = 1 << 57
	)
	if status&bitPCC != 0 {
		return "panic"
	}
	if status&bitUC != 0 {
		return "uncorrected"
	}
	return "corrected"
}

// scrapeRASCounters tries the rasdaemon sysfs counter path.
// rasdaemon (when running) writes aggregate counts to:
//   /sys/kernel/debug/rasdaemon/<severity>_count   (implementation-specific)
// Alternatively, some platforms expose:
//   /sys/devices/system/edac/mc<N>/csrow<N>/ue_count  (already covered by EDAC collector)
//
// This function looks for a standardized counter file written by rasdaemon.
func (c *MCECollector) scrapeRASCounters() ([]mceEvent, error) {
	// Look for rasdaemon counter files (if rasdaemon is installed and running)
	rasCounterDir := "/var/lib/rasdaemon"
	entries, err := os.ReadDir(rasCounterDir)
	if err != nil {
		return nil, fmt.Errorf("rasdaemon counters not found: %w", err)
	}

	reCounter := regexp.MustCompile(`^(\w+)_count$`)
	var events []mceEvent
	for _, entry := range entries {
		m := reCounter.FindStringSubmatch(entry.Name())
		if m == nil {
			continue
		}
		severity := m[1]
		count, err := readUint64(filepath.Join(rasCounterDir, entry.Name()))
		if err != nil {
			continue
		}
		// Parse severity → bank from filename convention: <severity>_bank<N>_count
		bankRe := regexp.MustCompile(`^(\w+)_bank(\d+)_count$`)
		if bm := bankRe.FindStringSubmatch(entry.Name()); bm != nil {
			severity = bm[1]
			events = append(events, mceEvent{
				severity: severity,
				bank:     bm[2],
				source:   "rasdaemon",
				count:    count,
			})
		} else {
			events = append(events, mceEvent{
				severity: severity,
				bank:     "all",
				source:   "rasdaemon",
				count:    count,
			})
		}
	}
	return events, nil
}

// Options holds configuration shared across collectors.
type Options struct {
	SysfsRoot     string
	ScrapeTimeout time.Duration
	Logger        *zap.Logger
}

// Ensure Options is defined once (declared here, used by edac.go and aer.go via same package).
// Go does not allow duplicate type declarations in the same package — the Options struct is the
// single canonical definition for all collectors in this package.
var _ = fmt.Sprintf // keep fmt imported
var _ = strings.TrimSpace // keep strings imported
