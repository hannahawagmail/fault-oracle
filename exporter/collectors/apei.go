// SPDX-License-Identifier: Apache-2.0
// apei.go — ACPI APEI BERT record count collector.
//
// Reads /sys/firmware/acpi/tables/BERT (raw table) and counts the total
// number of CPER records present in the boot error region. This provides
// a lightweight Prometheus gauge without parsing the full CPER structure
// (that is handled by apei/bert-reader.py for the textfile collector).
//
// The collector emits 0 records (not an error) when BERT is absent, since
// most cloud ARM64 instances don't expose BERT.
//
// Package collectors provides Prometheus collectors for ARM Linux hardware
// error reporting subsystems (EDAC, PCIe AER, APEI BERT, MCE, eBPF tracepoints).
package collectors

import (
	"encoding/binary"
	"fmt"
	"os"
	"path/filepath"

	"github.com/prometheus/client_golang/prometheus"
	"go.uber.org/zap"
)

const (
	// ACPI common table header: 4(sig)+4(len)+1(cksum)+1(oemid_len)+6(oemid)+
	// 8(oem_table_id)+4(oem_revision)+4(creator_id)+4(creator_revision) = 36B
	acpiTableHeaderSize = 36
	// BERT body: BootErrorRegionLength (4B) + BootErrorRegionOffset (8B)
	bertBodySize = 12
	// Minimum CPER record header size (UEFI 2.9 Appendix N)
	cperMinSize = 128
	// CPER_HEADER_SIZE is the minimum header size used for bounds-checking the
	// boot error region before slicing. Identical to cperMinSize; named for
	// clarity at the call site.
	CPER_HEADER_SIZE = cperMinSize
	// CPER signature
	cperSignature = uint32(0x52455043) // "CPER" little-endian
)

// APEICollector reads ACPI BERT boot error records.
type APEICollector struct {
	opts Options

	bertRecordsDesc *prometheus.Desc
}

// NewAPEICollector returns a new APEICollector.
func NewAPEICollector(opts Options) *APEICollector {
	const ns = "apei"
	return &APEICollector{
		opts: opts,
		bertRecordsDesc: prometheus.NewDesc(
			ns+"_bert_record_count",
			"Number of CPER records in the ACPI BERT boot error region.",
			[]string{"severity"}, nil,
		),
	}
}

// Describe implements prometheus.Collector.
func (c *APEICollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.bertRecordsDesc
	ch <- collectorUpDesc
}

// Collect implements prometheus.Collector.
func (c *APEICollector) Collect(ch chan<- prometheus.Metric) {
	bertPath := filepath.Join(c.opts.SysfsRoot, "firmware/acpi/tables/BERT")

	data, err := os.ReadFile(bertPath)
	if err != nil {
		// BERT absent — normal on cloud VMs
		c.opts.Logger.Debug("APEI BERT not found", zap.String("path", bertPath))
		ch <- prometheus.MustNewConstMetric(collectorUpDesc, prometheus.GaugeValue, 0, "apei")
		// Emit 0 so dashboards don't show a gap
		ch <- prometheus.MustNewConstMetric(c.bertRecordsDesc, prometheus.GaugeValue, 0, "none")
		return
	}

	counts, err := parseBERTRecordCounts(data)
	if err != nil {
		c.opts.Logger.Warn("APEI BERT parse error", zap.Error(err))
		ch <- prometheus.MustNewConstMetric(collectorUpDesc, prometheus.GaugeValue, 0, "apei")
		return
	}

	ch <- prometheus.MustNewConstMetric(collectorUpDesc, prometheus.GaugeValue, 1, "apei")
	for severity, cnt := range counts {
		ch <- prometheus.MustNewConstMetric(c.bertRecordsDesc, prometheus.GaugeValue, float64(cnt), severity)
	}
	if len(counts) == 0 {
		ch <- prometheus.MustNewConstMetric(c.bertRecordsDesc, prometheus.GaugeValue, 0, "none")
	}
}

// severityName maps CPER severity field (uint32) to a string label.
func severityName(s uint32) string {
	switch s {
	case 0:
		return "recoverable"
	case 1:
		return "fatal"
	case 2:
		return "corrected"
	case 3:
		return "informational"
	default:
		return fmt.Sprintf("unknown_%d", s)
	}
}

// parseBERTRecordCounts walks CPER records in the BERT region and returns
// a map of severity_name → count.
func parseBERTRecordCounts(data []byte) (map[string]int, error) {
	if len(data) < acpiTableHeaderSize+bertBodySize {
		return nil, fmt.Errorf("BERT table too short (%d bytes)", len(data))
	}

	regionLength := binary.LittleEndian.Uint32(data[acpiTableHeaderSize:])
	regionOffset := binary.LittleEndian.Uint64(data[acpiTableHeaderSize+4:])

	if uint64(regionOffset) > uint64(len(data)) {
		return nil, fmt.Errorf("BERT: regionOffset %d exceeds table size %d", regionOffset, len(data))
	}

	var region []byte
	if regionOffset > 0 &&
		uint64(regionOffset)+uint64(CPER_HEADER_SIZE) <= uint64(len(data)) &&
		regionOffset < uint64(len(data)) &&
		uint64(regionLength) <= uint64(len(data))-regionOffset {
		region = data[regionOffset : regionOffset+uint64(regionLength)]
	} else {
		region = data[acpiTableHeaderSize+bertBodySize:]
	}

	counts := map[string]int{}
	offset := 0
	for offset+cperMinSize <= len(region) {
		sig := binary.LittleEndian.Uint32(region[offset:])
		if sig != cperSignature {
			break
		}
		recordLength := binary.LittleEndian.Uint32(region[offset+20:])
		severityRaw := binary.LittleEndian.Uint32(region[offset+24:])
		counts[severityName(severityRaw)]++

		if int(recordLength) < cperMinSize {
			break
		}
		offset += int(recordLength)
	}
	return counts, nil
}
