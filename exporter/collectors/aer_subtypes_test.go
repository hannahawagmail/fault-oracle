// SPDX-License-Identifier: Apache-2.0
package collectors_test

import (
	"os"
	"path/filepath"
	"testing"
)

func writeAERFile(t *testing.T, dir, name, content string) string {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte(content), 0644); err != nil {
		t.Fatalf("write %s: %v", name, err)
	}
	return p
}

func TestParseAERSysfsFile_Correctable(t *testing.T) {
	dir := t.TempDir()
	path := writeAERFile(t, dir, "aer_dev_correctable",
		"RxErr 0 BadTLP 3 BadDLLP 1 Rollover 0 Timeout 0 NonFatalErr 0 CorrIntErr 0 HeaderOF 0 TOTAL_ERR_COR 4\n")

	counts, err := parseAERSysfsFile(path, aerCorrectableSubtypes)
	if err != nil {
		t.Fatalf("parse error: %v", err)
	}

	bySubtype := map[string]float64{}
	for _, c := range counts {
		bySubtype[c.subtype] = c.value
	}

	if bySubtype["bad_tlp"] != 3 {
		t.Errorf("bad_tlp: want 3, got %v", bySubtype["bad_tlp"])
	}
	if bySubtype["bad_dllp"] != 1 {
		t.Errorf("bad_dllp: want 1, got %v", bySubtype["bad_dllp"])
	}
}

func TestParseAERSysfsFile_SkipsTotalEntry(t *testing.T) {
	dir := t.TempDir()
	path := writeAERFile(t, dir, "aer_dev_correctable",
		"BadTLP 2 TOTAL_ERR_COR 2\n")

	counts, err := parseAERSysfsFile(path, aerCorrectableSubtypes)
	if err != nil {
		t.Fatalf("parse error: %v", err)
	}
	for _, c := range counts {
		if c.subtype == "total_err_cor" || c.subtype == "TOTAL_ERR_COR" {
			t.Error("TOTAL_ERR_COR should be skipped")
		}
	}
}

func TestParseAERSysfsFile_AllZeros(t *testing.T) {
	dir := t.TempDir()
	path := writeAERFile(t, dir, "aer_dev_correctable",
		"RxErr 0 BadTLP 0 BadDLLP 0 TOTAL_ERR_COR 0\n")

	counts, err := parseAERSysfsFile(path, aerCorrectableSubtypes)
	if err != nil {
		t.Fatalf("parse error: %v", err)
	}
	for _, c := range counts {
		if c.value != 0 {
			t.Errorf("expected 0 for %s, got %v", c.subtype, c.value)
		}
	}
}

func TestParseAERSysfsFile_MissingFile(t *testing.T) {
	_, err := parseAERSysfsFile("/nonexistent/aer_dev_correctable", aerCorrectableSubtypes)
	if err == nil {
		t.Error("expected error for missing file")
	}
}

func TestParseAERSysfsFile_FatalSubtypes(t *testing.T) {
	dir := t.TempDir()
	path := writeAERFile(t, dir, "aer_dev_fatal",
		"DLP 1 SDES 0 TLP 0 MalfTLP 2 TOTAL_ERR_FATAL 3\n")

	counts, err := parseAERSysfsFile(path, aerFatalSubtypes)
	if err != nil {
		t.Fatalf("parse error: %v", err)
	}

	bySubtype := map[string]float64{}
	for _, c := range counts {
		bySubtype[c.subtype] = c.value
	}

	if bySubtype["data_link_protocol_error"] != 1 {
		t.Errorf("DLP: want 1, got %v", bySubtype["data_link_protocol_error"])
	}
	if bySubtype["malformed_tlp"] != 2 {
		t.Errorf("MalfTLP: want 2, got %v", bySubtype["malformed_tlp"])
	}
}
