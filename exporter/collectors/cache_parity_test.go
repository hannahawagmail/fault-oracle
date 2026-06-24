// SPDX-License-Identifier: Apache-2.0
// collectors/cache_parity_test.go — Unit tests for the cache parity collector.

package collectors

import (
	"fmt"
	"path/filepath"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
	"go.uber.org/zap"
)

func buildCacheECCSysfs(t *testing.T, root, cpu, level string, ce, ue uint64) {
	t.Helper()
	devDir := filepath.Join(root, "bus", "platform", "devices", fmt.Sprintf("arm-cache-ecc.cpu%s-%s", cpu, level))
	writeFile(t, filepath.Join(devDir, "corrected_errors"), fmt.Sprintf("%d\n", ce))
	writeFile(t, filepath.Join(devDir, "uncorrected_errors"), fmt.Sprintf("%d\n", ue))
}

func TestCacheParityCollector_Metrics(t *testing.T) {
	root := t.TempDir()
	buildCacheECCSysfs(t, root, "0", "l1", 5, 1)
	buildCacheECCSysfs(t, root, "0", "l2", 3, 0)
	buildCacheECCSysfs(t, root, "1", "l1", 2, 0)

	reg := prometheus.NewRegistry()
	c := NewCacheParityCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	if err := reg.Register(c); err != nil {
		t.Fatalf("Register: %v", err)
	}

	expected := `
# HELP cpu_cache_correctable_total Total correctable CPU cache ECC errors.
# TYPE cpu_cache_correctable_total counter
cpu_cache_correctable_total{cpu="0",level="l1"} 5
cpu_cache_correctable_total{cpu="0",level="l2"} 3
cpu_cache_correctable_total{cpu="1",level="l1"} 2
# HELP cpu_cache_uncorrectable_total Total uncorrectable CPU cache ECC errors.
# TYPE cpu_cache_uncorrectable_total counter
cpu_cache_uncorrectable_total{cpu="0",level="l1"} 1
cpu_cache_uncorrectable_total{cpu="0",level="l2"} 0
cpu_cache_uncorrectable_total{cpu="1",level="l1"} 0
`
	if err := testutil.GatherAndCompare(reg, strings.NewReader(expected),
		"cpu_cache_correctable_total", "cpu_cache_uncorrectable_total"); err != nil {
		t.Fatal(err)
	}
}

func TestCacheParityCollector_Up(t *testing.T) {
	root := t.TempDir()
	buildCacheECCSysfs(t, root, "0", "l1", 1, 0)

	reg := prometheus.NewRegistry()
	c := NewCacheParityCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	if err := reg.Register(c); err != nil {
		t.Fatalf("Register: %v", err)
	}

	expected := `
# HELP fault_resilience_collector_up 1 if the collector is running and producing metrics, 0 if disabled or hardware absent
# TYPE fault_resilience_collector_up gauge
fault_resilience_collector_up{collector="cache_parity"} 1
`
	if err := testutil.GatherAndCompare(reg, strings.NewReader(expected),
		"fault_resilience_collector_up"); err != nil {
		t.Fatal(err)
	}
}

func TestCacheParityCollector_MissingSysfs(t *testing.T) {
	root := t.TempDir() // empty — no arm-cache-ecc devices

	reg := prometheus.NewRegistry()
	c := NewCacheParityCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	if err := reg.Register(c); err != nil {
		t.Fatalf("Register: %v", err)
	}

	expected := `
# HELP fault_resilience_collector_up 1 if the collector is running and producing metrics, 0 if disabled or hardware absent
# TYPE fault_resilience_collector_up gauge
fault_resilience_collector_up{collector="cache_parity"} 0
`
	if err := testutil.GatherAndCompare(reg, strings.NewReader(expected),
		"fault_resilience_collector_up"); err != nil {
		t.Fatal(err)
	}
}
