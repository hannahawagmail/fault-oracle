// SPDX-License-Identifier: Apache-2.0
package collectors

import (
	"encoding/binary"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
	"go.uber.org/zap"
)

func newAPEICollector(t *testing.T, sysfsRoot string) *APEICollector {
	t.Helper()
	return NewAPEICollector(Options{SysfsRoot: sysfsRoot, Logger: zap.NewNop()})
}

// buildBERTFile creates a minimal BERT table with one CPER record of the given severity.
func buildBERTFile(t *testing.T, root string, severity uint32) {
	t.Helper()
	bertDir := filepath.Join(root, "firmware", "acpi", "tables")
	if err := os.MkdirAll(bertDir, 0o755); err != nil {
		t.Fatal(err)
	}

	// ACPI header (36B) + BERT body (regionLength=4B, regionOffset=8B) + CPER record
	recordLen := uint32(cperMinSize)
	regionLen := recordLen
	regionOff := uint64(acpiTableHeaderSize + bertBodySize)

	buf := make([]byte, int(regionOff)+int(regionLen))
	// BERT body: regionLength + regionOffset
	binary.LittleEndian.PutUint32(buf[acpiTableHeaderSize:], regionLen)
	binary.LittleEndian.PutUint64(buf[acpiTableHeaderSize+4:], regionOff)
	// CPER record header at regionOff
	binary.LittleEndian.PutUint32(buf[regionOff:], cperSignature)
	binary.LittleEndian.PutUint32(buf[regionOff+20:], recordLen)
	binary.LittleEndian.PutUint32(buf[regionOff+24:], severity)

	if err := os.WriteFile(filepath.Join(bertDir, "BERT"), buf, 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestNewAPEICollector_NoPanic(t *testing.T) {
	c := newAPEICollector(t, t.TempDir())
	if c == nil {
		t.Fatal("NewAPEICollector returned nil")
	}
}

func TestAPEICollector_Collect_EmitsBERTRecords(t *testing.T) {
	root := t.TempDir()
	buildBERTFile(t, root, 2) // severity=2 → "corrected"

	reg := prometheus.NewRegistry()
	if err := reg.Register(newAPEICollector(t, root)); err != nil {
		t.Fatal(err)
	}

	expected := `
# HELP apei_bert_record_count Number of CPER records in the ACPI BERT boot error region.
# TYPE apei_bert_record_count gauge
apei_bert_record_count{severity="corrected"} 1
`
	if err := testutil.GatherAndCompare(reg, strings.NewReader(expected), "apei_bert_record_count"); err != nil {
		t.Error(err)
	}
}

func TestAPEICollector_Collect_MissingSysfs(t *testing.T) {
	root := t.TempDir() // no BERT file

	reg := prometheus.NewRegistry()
	if err := reg.Register(newAPEICollector(t, root)); err != nil {
		t.Fatal(err)
	}

	expected := `
# HELP fault_resilience_collector_up 1 if the collector is running and producing metrics, 0 if disabled or hardware absent
# TYPE fault_resilience_collector_up gauge
fault_resilience_collector_up{collector="apei"} 0
`
	if err := testutil.GatherAndCompare(reg, strings.NewReader(expected), "fault_resilience_collector_up"); err != nil {
		t.Error(err)
	}
}

func TestParseBERTRecordCounts_Valid(t *testing.T) {
	root := t.TempDir()
	buildBERTFile(t, root, 1) // fatal
	data, _ := os.ReadFile(filepath.Join(root, "firmware", "acpi", "tables", "BERT"))
	counts, err := parseBERTRecordCounts(data)
	if err != nil {
		t.Fatal(err)
	}
	if counts["fatal"] != 1 {
		t.Errorf("want fatal=1, got %v", counts)
	}
}

func TestParseBERTRecordCounts_TooShort(t *testing.T) {
	_, err := parseBERTRecordCounts([]byte{0, 1, 2})
	if err == nil {
		t.Fatal("expected error for short data")
	}
}

func TestSeverityName(t *testing.T) {
	cases := map[uint32]string{0: "recoverable", 1: "fatal", 2: "corrected", 3: "informational", 99: "unknown_99"}
	for code, want := range cases {
		if got := severityName(code); got != want {
			t.Errorf("severityName(%d) = %q, want %q", code, got, want)
		}
	}
}
