// SPDX-License-Identifier: Apache-2.0
//
// collectors/aer_test.go — Unit tests for the PCIe AER Prometheus collector.
//
// Tests use a temporary directory tree mimicking:
//
//	<tmpdir>/bus/pci/devices/<BDF>/
//	    aer_dev_correctable
//	    aer_dev_nonfatal
//	    aer_dev_fatal
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
	dto "github.com/prometheus/client_model/go"
	"go.uber.org/zap"
)

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// labelMap extracts label name→value pairs from a gathered metric.
func labelMap(m *dto.Metric) map[string]string {
	out := make(map[string]string)
	for _, lp := range m.GetLabel() {
		out[lp.GetName()] = lp.GetValue()
	}
	return out
}

// buildAERDevice creates a mock PCIe AER device under root/bus/pci/devices/<bdf>/.
// correctable, nonfatal, and fatal map error_name → count.
func buildAERDevice(t *testing.T, root, bdf string,
	correctable, nonfatal, fatal map[string]uint64) {
	t.Helper()
	devPath := filepath.Join(root, "bus", "pci", "devices", bdf)
	if err := os.MkdirAll(devPath, 0o755); err != nil {
		t.Fatalf("MkdirAll %s: %v", devPath, err)
	}

	writeAERFile := func(name string, counts map[string]uint64) {
		if counts == nil {
			return
		}
		var sb strings.Builder
		for k, v := range counts {
			fmt.Fprintf(&sb, "%s %d\n", k, v)
		}
		if err := os.WriteFile(filepath.Join(devPath, name), []byte(sb.String()), 0o644); err != nil {
			t.Fatalf("WriteFile %s: %v", name, err)
		}
	}

	writeAERFile("aer_dev_correctable", correctable)
	writeAERFile("aer_dev_nonfatal", nonfatal)
	writeAERFile("aer_dev_fatal", fatal)
}

func newAERCollector(t *testing.T, sysfsRoot string) *AERCollector {
	t.Helper()
	return NewAERCollector(Options{
		SysfsRoot: sysfsRoot,
		Logger:    zap.NewNop(),
	})
}

// ---------------------------------------------------------------------------
// parseAERFile tests
// ---------------------------------------------------------------------------

func TestParseAERFile_StandardCorrectableFormat(t *testing.T) {
	dir := t.TempDir()
	content := "RxErr 0\nBadTLP 3\nBadDLLP 0\nRollover 0\nTOTAL_ERR_COR 3\n"
	path := filepath.Join(dir, "aer_dev_correctable")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	result, err := parseAERFile(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result["RxErr"] != 0 {
		t.Errorf("RxErr: want 0, got %d", result["RxErr"])
	}
	if result["BadTLP"] != 3 {
		t.Errorf("BadTLP: want 3, got %d", result["BadTLP"])
	}
	if result["TOTAL_ERR_COR"] != 3 {
		t.Errorf("TOTAL_ERR_COR: want 3, got %d", result["TOTAL_ERR_COR"])
	}
	if result["BadDLLP"] != 0 {
		t.Errorf("BadDLLP: want 0, got %d", result["BadDLLP"])
	}
}

func TestParseAERFile_AllZeros(t *testing.T) {
	dir := t.TempDir()
	content := "RxErr 0\nBadTLP 0\nBadDLLP 0\nTOTAL_ERR_COR 0\n"
	path := filepath.Join(dir, "aer_dev_correctable")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	result, err := parseAERFile(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	for name, count := range result {
		if count != 0 {
			t.Errorf("expected all zeros, got %s=%d", name, count)
		}
	}
}

func TestParseAERFile_EmptyFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "aer_dev_correctable")
	if err := os.WriteFile(path, []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}

	result, err := parseAERFile(path)
	if err != nil {
		t.Fatalf("unexpected error on empty file: %v", err)
	}
	if len(result) != 0 {
		t.Errorf("expected empty map for empty file, got %v", result)
	}
}

func TestParseAERFile_MalformedLinesSkipped(t *testing.T) {
	dir := t.TempDir()
	content := "RxErr 0\nBAD_LINE_NO_COUNT\nBadTLP 3\n: malformed\nTOTAL_ERR_COR 3\n"
	path := filepath.Join(dir, "aer_dev_correctable")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	result, err := parseAERFile(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if _, bad := result["BAD_LINE_NO_COUNT"]; bad {
		t.Error("malformed line should have been skipped")
	}
	if result["BadTLP"] != 3 {
		t.Errorf("BadTLP: want 3, got %d", result["BadTLP"])
	}
}

func TestParseAERFile_NonNumericCountSkipped(t *testing.T) {
	dir := t.TempDir()
	content := "RxErr abc\nBadTLP 3\n"
	path := filepath.Join(dir, "aer_dev_correctable")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	result, err := parseAERFile(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if _, ok := result["RxErr"]; ok {
		t.Error("non-numeric count line should have been skipped")
	}
	if result["BadTLP"] != 3 {
		t.Errorf("BadTLP: want 3, got %d", result["BadTLP"])
	}
}

func TestParseAERFile_BlankLinesIgnored(t *testing.T) {
	dir := t.TempDir()
	content := "\nRxErr 1\n\nBadTLP 2\n\n"
	path := filepath.Join(dir, "aer_dev_correctable")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	result, err := parseAERFile(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result["RxErr"] != 1 {
		t.Errorf("RxErr: want 1, got %d", result["RxErr"])
	}
	if result["BadTLP"] != 2 {
		t.Errorf("BadTLP: want 2, got %d", result["BadTLP"])
	}
}

func TestParseAERFile_LargeCount(t *testing.T) {
	dir := t.TempDir()
	const large = uint64(1<<32 + 999)
	content := fmt.Sprintf("BadTLP %d\nTOTAL_ERR_COR %d\n", large, large)
	path := filepath.Join(dir, "aer_dev_correctable")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	result, err := parseAERFile(path)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result["BadTLP"] != large {
		t.Errorf("BadTLP: want %d, got %d", large, result["BadTLP"])
	}
}

func TestParseAERFile_MissingFile_Error(t *testing.T) {
	_, err := parseAERFile("/nonexistent/path/aer_dev_correctable")
	if err == nil {
		t.Fatal("expected error for missing file, got nil")
	}
}

func TestParseAERFile_AllStandardCorrectableErrorNames(t *testing.T) {
	names := []string{
		"RxErr", "BadTLP", "BadDLLP", "Rollover",
		"Timeout", "NonFatalErr", "CorrIntErr", "HeaderOF", "TOTAL_ERR_COR",
	}
	for _, name := range names {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			content := fmt.Sprintf("%s 1\n", name)
			path := filepath.Join(dir, "aer_dev_correctable")
			if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
				t.Fatal(err)
			}
			result, err := parseAERFile(path)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if result[name] != 1 {
				t.Errorf("%s: want 1, got %d", name, result[name])
			}
		})
	}
}

// ---------------------------------------------------------------------------
// AERCollector.Collect tests
// ---------------------------------------------------------------------------

func TestAERCollector_SingleDevice_CorrectableErrors(t *testing.T) {
	root := t.TempDir()
	buildAERDevice(t, root, "0000:01:00.0",
		map[string]uint64{
			"RxErr": 0, "BadTLP": 3, "BadDLLP": 0,
			"Rollover": 0, "Timeout": 0, "TOTAL_ERR_COR": 3,
		},
		map[string]uint64{"TOTAL_ERR_UNCOR": 0},
		map[string]uint64{"TOTAL_ERR_UNCOR": 0},
	)

	reg := prometheus.NewRegistry()
	c := newAERCollector(t, root)
	if err := reg.Register(c); err != nil {
		t.Fatalf("Register: %v", err)
	}

	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("Gather: %v", err)
	}

	// Find pcie_aer_correctable_total
	var found bool
	for _, mf := range mfs {
		if mf.GetName() == "pcie_aer_correctable_total" {
			found = true
			for _, m := range mf.GetMetric() {
				labels := labelMap(m)
				if labels["error_type"] == "BadTLP" && labels["device"] == "0000:01:00.0" {
					if m.Counter.GetValue() != 3 {
						t.Errorf("BadTLP: want 3, got %f", m.Counter.GetValue())
					}
				}
			}
		}
	}
	if !found {
		t.Error("pcie_aer_correctable_total not found in output")
	}
}

func TestAERCollector_CorrectableLabelsBDFAndErrorType(t *testing.T) {
	root := t.TempDir()
	buildAERDevice(t, root, "0000:02:00.0",
		map[string]uint64{"BadTLP": 7, "RxErr": 1, "TOTAL_ERR_COR": 8},
		map[string]uint64{"TOTAL_ERR_UNCOR": 0},
		map[string]uint64{"TOTAL_ERR_UNCOR": 0},
	)

	c := newAERCollector(t, root)
	ch := make(chan prometheus.Metric, 64)
	c.Collect(ch)
	close(ch)

	// drain Collect output (not used below)
	for range ch {
	}
	// Verify label names via Describe — avoids importing prometheus/client_model proto types.
	var sawBDFLabel, sawErrorTypeLabel bool
	descCh := make(chan *prometheus.Desc, 16)
	c.Describe(descCh)
	close(descCh)
	for d := range descCh {
		ds := d.String()
		if strings.Contains(ds, "pcie_aer_correctable_total") {
			if strings.Contains(ds, "device") {
				sawBDFLabel = true
			}
			if strings.Contains(ds, "error_type") {
				sawErrorTypeLabel = true
			}
		}
	}
	if !sawBDFLabel {
		t.Error("pcie_aer_correctable_total missing 'device' label")
	}
	if !sawErrorTypeLabel {
		t.Error("pcie_aer_correctable_total missing 'error_type' label")
	}
}

func TestAERCollector_AllZeroCountsEmitMetrics(t *testing.T) {
	// A device with all-zero AER counts should still emit metrics (count=0).
	root := t.TempDir()
	buildAERDevice(t, root, "0000:03:00.0",
		map[string]uint64{"RxErr": 0, "BadTLP": 0, "TOTAL_ERR_COR": 0},
		map[string]uint64{"TOTAL_ERR_UNCOR": 0},
		map[string]uint64{"TOTAL_ERR_UNCOR": 0},
	)

	reg := prometheus.NewRegistry()
	c := newAERCollector(t, root)
	if err := reg.Register(c); err != nil {
		t.Fatalf("Register: %v", err)
	}

	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("Gather: %v", err)
	}
	var corrFound bool
	for _, mf := range mfs {
		if mf.GetName() == "pcie_aer_correctable_total" {
			corrFound = true
			for _, m := range mf.GetMetric() {
				if m.Counter.GetValue() != 0 {
					t.Errorf("expected 0 count, got %f", m.Counter.GetValue())
				}
			}
		}
	}
	if !corrFound {
		t.Error("pcie_aer_correctable_total not emitted for all-zero device")
	}
}

func TestAERCollector_DevicesTotal(t *testing.T) {
	root := t.TempDir()
	buildAERDevice(t, root, "0000:01:00.0",
		map[string]uint64{"RxErr": 0, "TOTAL_ERR_COR": 0},
		map[string]uint64{"TOTAL_ERR_UNCOR": 0},
		map[string]uint64{"TOTAL_ERR_UNCOR": 0},
	)
	buildAERDevice(t, root, "0000:02:00.0",
		map[string]uint64{"RxErr": 0, "TOTAL_ERR_COR": 0},
		map[string]uint64{"TOTAL_ERR_UNCOR": 0},
		map[string]uint64{"TOTAL_ERR_UNCOR": 0},
	)

	reg := prometheus.NewRegistry()
	c := newAERCollector(t, root)
	if err := reg.Register(c); err != nil {
		t.Fatalf("Register: %v", err)
	}

	const expected = `
# HELP pcie_aer_devices_total Number of PCIe devices with AER capability visible in sysfs.
# TYPE pcie_aer_devices_total gauge
pcie_aer_devices_total 2
`
	err := testutil.GatherAndCompare(reg, strings.NewReader(expected), "pcie_aer_devices_total")
	if err != nil {
		t.Errorf("devices_total mismatch:\n%v", err)
	}
}

func TestAERCollector_DeviceWithoutAERSkipped(t *testing.T) {
	// A device directory without aer_dev_correctable should not contribute to devicesTotal.
	root := t.TempDir()
	// This device has AER files
	buildAERDevice(t, root, "0000:01:00.0",
		map[string]uint64{"RxErr": 0, "TOTAL_ERR_COR": 0},
		map[string]uint64{"TOTAL_ERR_UNCOR": 0},
		map[string]uint64{"TOTAL_ERR_UNCOR": 0},
	)
	// This one does NOT have AER files
	noAERPath := filepath.Join(root, "bus", "pci", "devices", "0000:04:00.0")
	if err := os.MkdirAll(noAERPath, 0o755); err != nil {
		t.Fatal(err)
	}

	reg := prometheus.NewRegistry()
	c := newAERCollector(t, root)
	if err := reg.Register(c); err != nil {
		t.Fatalf("Register: %v", err)
	}

	const expected = `
# HELP pcie_aer_devices_total Number of PCIe devices with AER capability visible in sysfs.
# TYPE pcie_aer_devices_total gauge
pcie_aer_devices_total 1
`
	err := testutil.GatherAndCompare(reg, strings.NewReader(expected), "pcie_aer_devices_total")
	if err != nil {
		t.Errorf("devices_total mismatch:\n%v", err)
	}
}

func TestAERCollector_ScrapeHealthMetricsEmitted(t *testing.T) {
	root := t.TempDir()
	buildAERDevice(t, root, "0000:01:00.0",
		map[string]uint64{"RxErr": 0, "TOTAL_ERR_COR": 0},
		nil, nil,
	)

	c := newAERCollector(t, root)
	ch := make(chan prometheus.Metric, 64)
	c.Collect(ch)
	close(ch)

	var names []string
	for m := range ch {
		names = append(names, descFQName(m.Desc()))
	}

	var hasDuration, hasErrors bool
	for _, n := range names {
		if n == "hw_fault_exporter_scrape_duration_seconds" {
			hasDuration = true
		}
		if n == "hw_fault_exporter_scrape_errors_total" {
			hasErrors = true
		}
	}
	if !hasDuration {
		t.Error("missing hw_fault_exporter_scrape_duration_seconds")
	}
	if !hasErrors {
		t.Error("missing hw_fault_exporter_scrape_errors_total")
	}
}

func TestAERCollector_TotalErrCorExcludedFromOutput(t *testing.T) {
	// TOTAL_ERR_COR is a sum and should NOT appear as a separate metric label value.
	root := t.TempDir()
	buildAERDevice(t, root, "0000:01:00.0",
		map[string]uint64{"RxErr": 0, "BadTLP": 2, "TOTAL_ERR_COR": 2},
		map[string]uint64{"TOTAL_ERR_UNCOR": 0},
		map[string]uint64{"TOTAL_ERR_UNCOR": 0},
	)

	c := newAERCollector(t, root)
	ch := make(chan prometheus.Metric, 64)
	c.Collect(ch)
	close(ch)

	for m := range ch {
		if strings.Contains(descFQName(m.Desc()), "pcie_aer_correctable_total") {
			// Check the metric proto to see if error_type=TOTAL_ERR_COR ever appears
			// We verify indirectly: gather the registry and inspect metric labels.
		}
	}
	// Use registry for a cleaner assertion
	reg := prometheus.NewRegistry()
	c2 := newAERCollector(t, root)
	_ = reg.Register(c2)
	mfs, _ := reg.Gather()
	for _, mf := range mfs {
		if mf.GetName() != "pcie_aer_correctable_total" {
			continue
		}
		for _, m := range mf.GetMetric() {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "error_type" && lp.GetValue() == "TOTAL_ERR_COR" {
					t.Error("TOTAL_ERR_COR should not appear as error_type label value")
				}
			}
		}
	}
}

func TestAERCollector_MissingPCIRoot_GracefulDegradation(t *testing.T) {
	// If bus/pci/devices doesn't exist, should not panic, should emit scrape error metrics.
	root := t.TempDir()
	// Do NOT create bus/pci/devices

	c := newAERCollector(t, root)
	ch := make(chan prometheus.Metric, 64)
	c.Collect(ch) // must not panic
	close(ch)

	var metrics []prometheus.Metric
	for m := range ch {
		metrics = append(metrics, m)
	}
	if len(metrics) == 0 {
		t.Error("expected at least scrape health metrics when PCI root is missing")
	}
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

// descFQName extracts the fully-qualified metric name from a Desc.
func descFQName(d *prometheus.Desc) string {
	s := d.String()
	start := strings.Index(s, `"`)
	if start < 0 {
		return ""
	}
	end := strings.Index(s[start+1:], `"`)
	if end < 0 {
		return ""
	}
	return s[start+1 : start+1+end]
}
