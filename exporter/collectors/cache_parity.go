// SPDX-License-Identifier: Apache-2.0
// collectors/cache_parity.go — Prometheus collector for ARM CPU cache parity/ECC errors.
// Scans /sys/bus/platform/devices/arm-cache-ecc.cpu<N>-<level>/ for corrected/uncorrected errors.

package collectors

import (
	"path/filepath"
	"regexp"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"go.uber.org/zap"
)

const cacheECCGlob = "bus/platform/devices/arm-cache-ecc.*"

var reCacheECC = regexp.MustCompile(`arm-cache-ecc\.cpu(\d+)-([a-z0-9]+)`)

type CacheParityCollector struct {
	opts    Options
	ceTotal *prometheus.Desc
	ueTotal *prometheus.Desc
}

func NewCacheParityCollector(opts Options) *CacheParityCollector {
	return &CacheParityCollector{
		opts: opts,
		ceTotal: prometheus.NewDesc("cpu_cache_correctable_total",
			"Total correctable CPU cache ECC errors.", []string{"cpu", "level"}, nil),
		ueTotal: prometheus.NewDesc("cpu_cache_uncorrectable_total",
			"Total uncorrectable CPU cache ECC errors.", []string{"cpu", "level"}, nil),
	}
}

func (c *CacheParityCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.ceTotal
	ch <- c.ueTotal
}

func (c *CacheParityCollector) Collect(ch chan<- prometheus.Metric) {
	start := time.Now()
	errCount, found := 0, false

	matches, err := filepath.Glob(filepath.Join(c.opts.SysfsRoot, cacheECCGlob))
	if err != nil || len(matches) == 0 {
		if err != nil {
			c.opts.Logger.Warn("cache_parity: glob failed", zap.Error(err))
			errCount++
		}
		ch <- prometheus.MustNewConstMetric(collectorUpDesc, prometheus.GaugeValue, 0, "cache_parity")
		emitSelf(ch, scrapeDurationDesc, scrapeErrorsDesc, "cache_parity", time.Since(start), errCount)
		return
	}

	for _, devPath := range matches {
		m := reCacheECC.FindStringSubmatch(filepath.Base(devPath))
		if m == nil {
			continue
		}
		cpu, level := m[1], m[2]
		if ce, err := readUint64(filepath.Join(devPath, "corrected_errors")); err == nil {
			found = true
			ch <- prometheus.MustNewConstMetric(c.ceTotal, prometheus.CounterValue, float64(ce), cpu, level)
		} else {
			errCount++
		}
		if ue, err := readUint64(filepath.Join(devPath, "uncorrected_errors")); err == nil {
			found = true
			ch <- prometheus.MustNewConstMetric(c.ueTotal, prometheus.CounterValue, float64(ue), cpu, level)
		} else {
			errCount++
		}
	}

	up := 0.0
	if found {
		up = 1.0
	}
	ch <- prometheus.MustNewConstMetric(collectorUpDesc, prometheus.GaugeValue, up, "cache_parity")
	emitSelf(ch, scrapeDurationDesc, scrapeErrorsDesc, "cache_parity", time.Since(start), errCount)
}
