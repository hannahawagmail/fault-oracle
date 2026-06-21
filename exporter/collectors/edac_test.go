// SPDX-License-Identifier: Apache-2.0
//
// collectors/edac_test.go — Unit tests for the EDAC Prometheus collector.
//
// Tests use a temporary directory tree that mimics the real sysfs layout:
//
//	<tmpdir>/devices/system/edac/mc/mc<N>/
//	    ce_count
//	    ue_count
//	    csrow<M>/
//	        ce_count
//	        ue_count
//	        ch<K>_ce_count
//
// No real hardware or root access is required.

package collectors

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
	"go.uber.org/zap"
)

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

// newTestLogger returns a no-op zap logger suitable for unit tests.
func newTestLogger() *zap.Logger {
	return zap.NewNop()
}

// writeFile creates parent directories and writes content to path.
func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("MkdirAll %s: %v", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("WriteFile %s: %v", path, err)
	}
}

// buildMCSysfs builds a single memory-controller sub-tree under root.
//
//	ceCounts[csrow][channel], ueCounts[csrow]
func buildMCSysfs(t *testing.T, root, mcName string, ceCounts [][]uint64, ueCounts []uint64) {
	t.Helper()
	mcPath := filepath.Join(root, "devices", "system", "edac", "mc", mcName)
	if err := os.MkdirAll(mcPath, 0o755); err != nil {
		t.Fatalf("MkdirAll %s: %v", mcPath, err)
	}

	// Controller-level totals
	var totalCE, totalUE uint64
	for _, row := range ceCounts {
		for _, ch := range row {
			totalCE += ch
		}
	}
	for _, ue := range ueCounts {
		totalUE += ue
	}
	writeFile(t, filepath.Join(mcPath, "ce_count"), fmt.Sprintf("%d\n", totalCE))
	writeFile(t, filepath.Join(mcPath, "ue_count"), fmt.Sprintf("%d\n", totalUE))

	// Per-csrow files
	for r, rowCEs := range ceCounts {
		csrowDir := filepath.Join(mcPath, fmt.Sprintf("csrow%d", r))
		if err := os.MkdirAll(csrowDir, 0o755); err != nil {
			t.Fatalf("MkdirAll %s: %v", csrowDir, err)
		}
		var csrowCE uint64
		for _, v := range rowCEs {
			csrowCE += v
		}
		writeFile(t, filepath.Join(csrowDir, "ce_count"), fmt.Sprintf("%d\n", csrowCE))
		writeFile(t, filepath.Join(csrowDir, "ue_count"), fmt.Sprintf("%d\n", ueCounts[r]))
		for c, chCE := range rowCEs {
			writeFile(t, filepath.Join(csrowDir, fmt.Sprintf("ch%d_ce_count", c)), fmt.Sprintf("%d\n", chCE))
		}
	}
}

// ---------------------------------------------------------------------------
// readUint64 tests
// ---------------------------------------------------------------------------

func TestReadUint64_BasicValue(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "ce_count")
	if err := os.WriteFile(path, []byte("42\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := readUint64(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != 42 {
		t.Errorf("want 42, got %d", got)
	}
}

func TestReadUint64_Zero(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "ce_count")
	if err := os.WriteFile(path, []byte("0\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := readUint64(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != 0 {
		t.Errorf("want 0, got %d", got)
	}
}

func TestReadUint64_LargeValue(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "ce_count")
	const large = uint64(1<<32 + 7)
	if err := os.WriteFile(path, []byte(fmt.Sprintf("%d\n", large)), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := readUint64(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != large {
		t.Errorf("want %d, got %d", large, got)
	}
}

func TestReadUint64_MaxUint64(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "ce_count")
	const maxU64 = uint64(^uint64(0)) // 2^64 - 1
	if err := os.WriteFile(path, []byte(fmt.Sprintf("%d\n", maxU64)), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := readUint64(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != maxU64 {
		t.Errorf("want %d, got %d", maxU64, got)
	}
}

func TestReadUint64_WhitespaceStripped(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "ce_count")
	// sysfs files often have trailing newlines and spaces
	if err := os.WriteFile(path, []byte("  17   \n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := readUint64(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != 17 {
		t.Errorf("want 17, got %d", got)
	}
}

func TestReadUint64_EmptyFile_Error(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "ce_count")
	if err := os.WriteFile(path, []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := readUint64(path)
	if err == nil {
		t.Fatal("expected error for empty file, got nil")
	}
	if !strings.Contains(err.Error(), "empty") {
		t.Errorf("expected 'empty' in error message, got: %v", err)
	}
}

func TestReadUint64_NonNumeric_Error(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "ce_count")
	if err := os.WriteFile(path, []byte("not-a-number\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := readUint64(path)
	if err == nil {
		t.Fatal("expected error for non-numeric content, got nil")
	}
}

func TestReadUint64_MissingFile_Error(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "nonexistent")
	_, err := readUint64(path)
	if err == nil {
		t.Fatal("expected error for missing file, got nil")
	}
	if !os.IsNotExist(err) {
		// Accept wrapped not-exist errors too
		if !strings.Contains(err.Error(), "no such file") &&
			!strings.Contains(err.Error(), "not found") {
			t.Logf("got error (may be wrapped): %v", err)
		}
	}
}

func TestReadUint64_HexNotAccepted(t *testing.T) {
	// EDAC sysfs counters are always base-10 decimal
	dir := t.TempDir()
	path := filepath.Join(dir, "ce_count")
	if err := os.WriteFile(path, []byte("0x1a\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := readUint64(path)
	if err == nil {
		t.Fatal("expected error for hex value, got nil")
	}
}

func TestReadUint64_ParametrizedValues(t *testing.T) {
	cases := []uint64{0, 1, 255, 1000, 1<<31 - 1, 1 << 32, 1<<63 - 1}
	for _, want := range cases {
		t.Run(fmt.Sprintf("val=%d", want), func(t *testing.T) {
			dir := t.TempDir()
			path := filepath.Join(dir, "count")
			if err := os.WriteFile(path, []byte(fmt.Sprintf("%d\n", want)), 0o644); err != nil {
				t.Fatal(err)
			}
			got, err := readUint64(path)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != want {
				t.Errorf("want %d, got %d", want, got)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// EDACCollector.Collect tests
// ---------------------------------------------------------------------------

func newEDACCollector(t *testing.T, sysfsRoot string) *EDACCollector {
	t.Helper()
	return NewEDACCollector(Options{
		SysfsRoot: sysfsRoot,
		Logger:    newTestLogger(),
	})
}

func TestEDACCollector_SingleController_KnownCounts(t *testing.T) {
	root := t.TempDir()
	// mc0: 2 csrows × 2 channels
	// csrow0: ch0=3, ch1=0  UE=0
	// csrow1: ch0=0, ch1=1  UE=0
	buildMCSysfs(t, root, "mc0",
		[][]uint64{{3, 0}, {0, 1}},
		[]uint64{0, 0},
	)

	c := newEDACCollector(t, root)

	// Verify Describe emits all expected descriptors
	descCh := make(chan *prometheus.Desc, 16)
	c.Describe(descCh)
	close(descCh)
	var descs []*prometheus.Desc
	for d := range descCh {
		descs = append(descs, d)
	}
	if len(descs) == 0 {
		t.Error("Describe returned no descriptors")
	}
	descStrings := make([]string, len(descs))
	for i, d := range descs {
		descStrings[i] = d.String()
	}
	for _, want := range []string{
		"edac_correctable_errors_total",
		"edac_uncorrectable_errors_total",
		"edac_controller_ce_total",
		"edac_controller_ue_total",
	} {
		found := false
		for _, ds := range descStrings {
			if strings.Contains(ds, want) {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("descriptor for %q not found in Describe output", want)
		}
	}

	// Collect metrics and verify via testutil
	reg := prometheus.NewRegistry()
	if err := reg.Register(c); err != nil {
		t.Fatalf("Register: %v", err)
	}

	// edac_controller_ce_total{controller="mc0"} should be 4 (3+0+0+1)
	// edac_controller_ue_total{controller="mc0"} should be 0
	const expectedMetrics = `
# HELP edac_controller_ce_total Total correctable EDAC errors on the memory controller since boot.
# TYPE edac_controller_ce_total counter
edac_controller_ce_total{controller="mc0"} 4
# HELP edac_controller_ue_total Total uncorrectable EDAC errors on the memory controller since boot.
# TYPE edac_controller_ue_total counter
edac_controller_ue_total{controller="mc0"} 0
`
	err := testutil.GatherAndCompare(reg, strings.NewReader(expectedMetrics),
		"edac_controller_ce_total",
		"edac_controller_ue_total",
	)
	if err != nil {
		t.Errorf("metric mismatch:\n%v", err)
	}
}

func TestEDACCollector_ChannelLevelCounts(t *testing.T) {
	root := t.TempDir()
	buildMCSysfs(t, root, "mc0",
		[][]uint64{{3, 0}, {0, 1}},
		[]uint64{0, 0},
	)

	reg := prometheus.NewRegistry()
	c := newEDACCollector(t, root)
	if err := reg.Register(c); err != nil {
		t.Fatalf("Register: %v", err)
	}

	const expectedCEMetrics = `
# HELP edac_correctable_errors_total Total correctable EDAC errors per channel since boot.
# TYPE edac_correctable_errors_total counter
edac_correctable_errors_total{channel="0",controller="mc0",csrow="0"} 3
edac_correctable_errors_total{channel="0",controller="mc0",csrow="1"} 0
edac_correctable_errors_total{channel="1",controller="mc0",csrow="0"} 0
edac_correctable_errors_total{channel="1",controller="mc0",csrow="1"} 1
`
	err := testutil.GatherAndCompare(reg, strings.NewReader(expectedCEMetrics),
		"edac_correctable_errors_total",
	)
	if err != nil {
		t.Errorf("channel CE metric mismatch:\n%v", err)
	}
}

func TestEDACCollector_MultipleControllers(t *testing.T) {
	root := t.TempDir()
	// mc0: has errors
	buildMCSysfs(t, root, "mc0",
		[][]uint64{{3, 0}, {0, 1}},
		[]uint64{0, 0},
	)
	// mc1: all zeros
	buildMCSysfs(t, root, "mc1",
		[][]uint64{{0, 0}, {0, 0}},
		[]uint64{0, 0},
	)

	reg := prometheus.NewRegistry()
	c := newEDACCollector(t, root)
	if err := reg.Register(c); err != nil {
		t.Fatalf("Register: %v", err)
	}

	const expectedCtrl = `
# HELP edac_controller_ce_total Total correctable EDAC errors on the memory controller since boot.
# TYPE edac_controller_ce_total counter
edac_controller_ce_total{controller="mc0"} 4
edac_controller_ce_total{controller="mc1"} 0
# HELP edac_controller_ue_total Total uncorrectable EDAC errors on the memory controller since boot.
# TYPE edac_controller_ue_total counter
edac_controller_ue_total{controller="mc0"} 0
edac_controller_ue_total{controller="mc1"} 0
`
	err := testutil.GatherAndCompare(reg, strings.NewReader(expectedCtrl),
		"edac_controller_ce_total",
		"edac_controller_ue_total",
	)
	if err != nil {
		t.Errorf("multi-controller metric mismatch:\n%v", err)
	}
}

func TestEDACCollector_CsrowNoChannelFiles(t *testing.T) {
	// A csrow directory with no ch*_ce_count files should not crash.
	root := t.TempDir()
	mcPath := filepath.Join(root, "devices", "system", "edac", "mc", "mc0")
	if err := os.MkdirAll(mcPath, 0o755); err != nil {
		t.Fatal(err)
	}
	writeFile(t, filepath.Join(mcPath, "ce_count"), "0\n")
	writeFile(t, filepath.Join(mcPath, "ue_count"), "0\n")
	csrowDir := filepath.Join(mcPath, "csrow0")
	if err := os.MkdirAll(csrowDir, 0o755); err != nil {
		t.Fatal(err)
	}
	writeFile(t, filepath.Join(csrowDir, "ce_count"), "0\n")
	writeFile(t, filepath.Join(csrowDir, "ue_count"), "0\n")
	// No ch*_ce_count files written

	c := newEDACCollector(t, root)
	ch := make(chan prometheus.Metric, 64)
	// Must not panic
	c.Collect(ch)
	close(ch)
}

func TestEDACCollector_MissingMCRoot_GracefulDegradation(t *testing.T) {
	// If the entire EDAC mc root doesn't exist, collector should not panic
	// and should emit scrape error metrics.
	root := t.TempDir()
	// Do NOT create devices/system/edac/mc

	c := newEDACCollector(t, root)
	ch := make(chan prometheus.Metric, 64)
	c.Collect(ch)
	close(ch)

	var metrics []prometheus.Metric
	for m := range ch {
		metrics = append(metrics, m)
	}
	// At minimum scrape health metrics should be emitted
	if len(metrics) == 0 {
		t.Error("expected at least scrape health metrics when mc root is missing")
	}
}

func TestEDACCollector_UncorrectableErrors(t *testing.T) {
	root := t.TempDir()
	// csrow0 has 1 UE, csrow1 has 2 UEs
	buildMCSysfs(t, root, "mc0",
		[][]uint64{{0, 0}, {0, 0}},
		[]uint64{1, 2},
	)

	reg := prometheus.NewRegistry()
	c := newEDACCollector(t, root)
	if err := reg.Register(c); err != nil {
		t.Fatalf("Register: %v", err)
	}

	const expectedUE = `
# HELP edac_uncorrectable_errors_total Total uncorrectable EDAC errors per csrow since boot.
# TYPE edac_uncorrectable_errors_total counter
edac_uncorrectable_errors_total{controller="mc0",csrow="0"} 1
edac_uncorrectable_errors_total{controller="mc0",csrow="1"} 2
`
	err := testutil.GatherAndCompare(reg, strings.NewReader(expectedUE),
		"edac_uncorrectable_errors_total",
	)
	if err != nil {
		t.Errorf("UE metric mismatch:\n%v", err)
	}
}

func TestEDACCollector_ScrapeHealthMetricsEmitted(t *testing.T) {
	root := t.TempDir()
	buildMCSysfs(t, root, "mc0",
		[][]uint64{{1, 0}},
		[]uint64{0},
	)

	c := newEDACCollector(t, root)
	metrics := collectAllMetrics(t, c)

	var hasDuration, hasErrors bool
	for _, name := range metricNames(metrics) {
		if name == "hw_fault_exporter_scrape_duration_seconds" {
			hasDuration = true
		}
		if name == "hw_fault_exporter_scrape_errors_total" {
			hasErrors = true
		}
	}
	if !hasDuration {
		t.Error("missing hw_fault_exporter_scrape_duration_seconds metric")
	}
	if !hasErrors {
		t.Error("missing hw_fault_exporter_scrape_errors_total metric")
	}
}

func TestEDACCollector_ScrapeErrorCountZeroOnCleanSysfs(t *testing.T) {
	root := t.TempDir()
	buildMCSysfs(t, root, "mc0",
		[][]uint64{{3, 0}, {0, 1}},
		[]uint64{0, 0},
	)

	reg := prometheus.NewRegistry()
	c := newEDACCollector(t, root)
	if err := reg.Register(c); err != nil {
		t.Fatalf("Register: %v", err)
	}

	const expectedErrors = `
# HELP hw_fault_exporter_scrape_errors_total Total sysfs read errors encountered by this collector.
# TYPE hw_fault_exporter_scrape_errors_total counter
hw_fault_exporter_scrape_errors_total{collector="edac"} 0
`
	err := testutil.GatherAndCompare(reg, strings.NewReader(expectedErrors),
		"hw_fault_exporter_scrape_errors_total",
	)
	if err != nil {
		t.Errorf("scrape error count mismatch:\n%v", err)
	}
}

func TestEDACCollector_NonMCDirEntriesIgnored(t *testing.T) {
	// Extraneous files/dirs inside the mc root should be silently skipped.
	root := t.TempDir()
	buildMCSysfs(t, root, "mc0",
		[][]uint64{{1, 0}},
		[]uint64{0},
	)
	mcRoot := filepath.Join(root, "devices", "system", "edac", "mc")
	// Add noise
	writeFile(t, filepath.Join(mcRoot, "some_file"), "noise\n")
	if err := os.MkdirAll(filepath.Join(mcRoot, "not_an_mc_dir"), 0o755); err != nil {
		t.Fatal(err)
	}

	c := newEDACCollector(t, root)
	ch := make(chan prometheus.Metric, 64)
	c.Collect(ch) // must not panic
	close(ch)
}

// ---------------------------------------------------------------------------
// Internal helpers for test-side metric inspection
// ---------------------------------------------------------------------------

// collectAllMetrics drains the collector into a slice.
func collectAllMetrics(t *testing.T, c prometheus.Collector) []prometheus.Metric {
	t.Helper()
	ch := make(chan prometheus.Metric, 256)
	c.Collect(ch)
	close(ch)
	var out []prometheus.Metric
	for m := range ch {
		out = append(out, m)
	}
	return out
}

// metricNames extracts the metric family name from each Metric via its Desc.
func metricNames(metrics []prometheus.Metric) []string {
	names := make([]string, 0, len(metrics))
	for _, m := range metrics {
		desc := m.Desc().String()
		// Desc.String() format: Desc{fqName: "...", ...}
		start := strings.Index(desc, `"`)
		end := strings.Index(desc[start+1:], `"`)
		if start >= 0 && end >= 0 {
			names = append(names, desc[start+1:start+1+end])
		}
	}
	return names
}
