// SPDX-License-Identifier: Apache-2.0
//
// collectors/cpufreq.go — CPU frequency and governor collector.
//
// Reads /sys/devices/system/cpu/cpu<N>/cpufreq/ and emits:
//   cpu_frequency_hz{cpu}       — current frequency in Hz
//   cpu_frequency_min_hz{cpu}   — minimum allowed frequency
//   cpu_frequency_max_hz{cpu}   — maximum allowed frequency
//   cpu_governor{cpu, governor} — 1 for the active governor
//   fault_resilience_collector_up{collector="cpufreq"}

package collectors

import (
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"go.uber.org/zap"
)

const cpuRoot = "devices/system/cpu"

// CpufreqCollector implements prometheus.Collector for CPU frequency scaling.
type CpufreqCollector struct {
	opts       Options
	curFreq    *prometheus.Desc
	minFreq    *prometheus.Desc
	maxFreq    *prometheus.Desc
	governor   *prometheus.Desc
	scrapeTime *prometheus.Desc
	scrapeErrs *prometheus.Desc
	// collectorUp uses the shared collectorUpDesc (see shared.go).
}

func NewCpufreqCollector(opts Options) *CpufreqCollector {
	return &CpufreqCollector{
		opts: opts,
		curFreq: prometheus.NewDesc(
			"cpu_frequency_hz",
			"Current CPU frequency in Hz.",
			[]string{"cpu"}, nil,
		),
		minFreq: prometheus.NewDesc(
			"cpu_frequency_min_hz",
			"Minimum CPU frequency in Hz (policy floor).",
			[]string{"cpu"}, nil,
		),
		maxFreq: prometheus.NewDesc(
			"cpu_frequency_max_hz",
			"Maximum CPU frequency in Hz (policy ceiling).",
			[]string{"cpu"}, nil,
		),
		governor: prometheus.NewDesc(
			"cpu_governor",
			"1 if the specified governor is active on this CPU, 0 otherwise.",
			[]string{"cpu", "governor"}, nil,
		),
		scrapeTime: prometheus.NewDesc("hw_fault_exporter_scrape_duration_seconds",
			"Scrape duration.", []string{"collector"}, nil),
		scrapeErrs: prometheus.NewDesc("hw_fault_exporter_scrape_errors_total",
			"Scrape errors.", []string{"collector"}, nil),
	}
}

func (c *CpufreqCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.curFreq
	ch <- c.minFreq
	ch <- c.maxFreq
	ch <- c.governor
	ch <- collectorUpDesc
	ch <- c.scrapeTime
	ch <- c.scrapeErrs
}

func (c *CpufreqCollector) Collect(ch chan<- prometheus.Metric) {
	start := time.Now()
	errCount := 0

	cpuDir := filepath.Join(c.opts.SysfsRoot, cpuRoot)
	entries, err := os.ReadDir(cpuDir)
	if err != nil {
		c.opts.Logger.Warn("cpufreq: cannot read cpu root",
			zap.String("path", cpuDir), zap.Error(err))
		ch <- prometheus.MustNewConstMetric(collectorUpDesc, prometheus.GaugeValue, 0, "cpufreq")
		emitSelf(ch, c.scrapeTime, c.scrapeErrs, "cpufreq", time.Since(start), 1)
		return
	}

	for _, entry := range entries {
		name := entry.Name()
		// Only process cpu<N> directories (not cpufreq, cpuidle, etc.)
		if !strings.HasPrefix(name, "cpu") {
			continue
		}
		// Skip non-numeric suffixes
		suffix := strings.TrimPrefix(name, "cpu")
		if len(suffix) == 0 || suffix[0] < '0' || suffix[0] > '9' {
			continue
		}

		freqDir := filepath.Join(cpuDir, name, "cpufreq")
		if _, err := os.Stat(freqDir); err != nil {
			continue // CPU has no cpufreq (offline or no scaling support)
		}

		// scaling_cur_freq is in kHz → convert to Hz
		if cur, err := readUint64(filepath.Join(freqDir, "scaling_cur_freq")); err == nil {
			ch <- prometheus.MustNewConstMetric(c.curFreq, prometheus.GaugeValue,
				float64(cur)*1000, name)
		} else {
			errCount++
		}
		if min, err := readUint64(filepath.Join(freqDir, "scaling_min_freq")); err == nil {
			ch <- prometheus.MustNewConstMetric(c.minFreq, prometheus.GaugeValue,
				float64(min)*1000, name)
		}
		if max, err := readUint64(filepath.Join(freqDir, "scaling_max_freq")); err == nil {
			ch <- prometheus.MustNewConstMetric(c.maxFreq, prometheus.GaugeValue,
				float64(max)*1000, name)
		}

		gov := strings.TrimSpace(readString(filepath.Join(freqDir, "scaling_governor")))
		if gov != "" {
			ch <- prometheus.MustNewConstMetric(c.governor, prometheus.GaugeValue,
				1, name, gov)
		}
	}

	ch <- prometheus.MustNewConstMetric(collectorUpDesc, prometheus.GaugeValue, 1, "cpufreq")
	emitSelf(ch, c.scrapeTime, c.scrapeErrs, "cpufreq", time.Since(start), errCount)
}
