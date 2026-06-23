// SPDX-License-Identifier: Apache-2.0
//
// dimm.go — DIMM slot mapper: correlates EDAC csrow/channel to SMBIOS slot labels.
//
// SMBIOS Type 17 (Memory Device) records describe each physical DIMM slot: its
// locator string (e.g. "DIMM_A1"), bank locator (e.g. "Node 0 Channel 0 Slot 0"),
// size, speed, type, and identity fields (manufacturer, serial, part number).
//
// The Linux EDAC subsystem identifies errors by memory controller index (mc<N>),
// chip-select row (csrow<M>), and channel (ch<K>).  The mapping between EDAC
// topology and physical DIMM slots is platform-specific and not exposed by the
// kernel directly.  This file infers the mapping by:
//
//  1. Parsing dmidecode -t 17 output (preferred — most portable).
//  2. Parsing raw SMBIOS binary entries under /sys/firmware/dmi/entries/17-*/.
//
// The DIMMCollector Prometheus collector emits an info-style gauge metric
// hw_dimm_info that associates EDAC location labels with human-readable DIMM
// identity, enabling operators to trace an alert to a physical slot without
// leaving Grafana.

package collectors

import (
	"bufio"
	"encoding/binary"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"github.com/prometheus/client_golang/prometheus"
	"go.uber.org/zap"
)

// -----------------------------------------------------------------------------
// SMBIOS Type 17 field offsets (SMBIOS 3.x spec, section 7.18)
//
// Offset  Length  Description
//   0x00   1 B    Type (must be 0x11 = 17)
//   0x01   1 B    Length of formatted area (varies by SMBIOS version)
//   0x02   2 B    Handle (little-endian uint16)
//   0x04   2 B    Physical Memory Array Handle
//   0x06   2 B    Memory Error Information Handle
//   0x08   2 B    Total Width (bits, 0xFFFF = unknown)
//   0x0A   2 B    Data Width (bits)
//   0x0C   2 B    Size (MB; bit 15 set means units are KB)
//   0x0E   1 B    Form Factor
//   0x0F   1 B    Device Set
//   0x10   1 B    Device Locator (string index)
//   0x11   1 B    Bank Locator (string index)
//   0x12   1 B    Memory Type
//   0x13   2 B    Type Detail
//   0x15   2 B    Speed (MHz; 0 = unknown)
//   0x17   1 B    Manufacturer (string index)
//   0x18   1 B    Serial Number (string index)
//   0x19   1 B    Asset Tag (string index)
//   0x1A   1 B    Part Number (string index)
//   0x1B   1 B    Attributes (rank, etc.)
//   0x1C   4 B    Extended Size (MB, used when Size == 0x7FFF)
//   0x20   2 B    Configured Memory Speed (MHz)
//   ... (additional fields in SMBIOS 2.8+, 3.x)
//
// After the formatted area, SMBIOS stores a variable-length string table:
// null-terminated strings indexed starting at 1, terminated by a double-null.
// -----------------------------------------------------------------------------

// smbiosType17MinLen is the minimum formatted area length for SMBIOS 2.1+.
const smbiosType17MinLen = 0x15

// DIMMInfo holds parsed SMBIOS Type 17 data for one memory device.
type DIMMInfo struct {
	Handle       uint16 // SMBIOS handle (unique per entry)
	Locator      string // Device locator, e.g. "DIMM_A1" or "ChannelA-DIMM0"
	BankLocator  string // Bank locator, e.g. "Node 0 Channel 0 Slot 0"
	SizeMB       uint64 // Installed size in MiB (0 if slot is empty)
	Type         string // Memory type string, e.g. "DDR4", "DDR5", "LPDDR5"
	SpeedMHz     uint32 // Configured speed in MHz (0 = unknown)
	Manufacturer string // JEDEC manufacturer string
	SerialNumber string // Module serial number
	PartNumber   string // Module part number

	// EDAC topology, inferred from BankLocator by heuristic parsing.
	// Set to -1 if the mapping could not be determined.
	EDACController string // e.g. "mc0"
	EDACCsrow      int    // chip-select row index, or -1
	EDACChannel    int    // channel index, or -1
}

// DIMMMapper reads SMBIOS DMI entries and maps them to EDAC topology.
type DIMMMapper struct {
	sysfsRoot string
	dimms     []DIMMInfo
	logger    *zap.Logger
}

// NewDIMMMapper constructs a DIMMMapper.  sysfsRoot is typically "/sys" on a
// live system, or a synthetic tree for tests.
func NewDIMMMapper(sysfsRoot string, logger *zap.Logger) *DIMMMapper {
	if logger == nil {
		logger = zap.NewNop()
	}
	return &DIMMMapper{
		sysfsRoot: sysfsRoot,
		logger:    logger,
	}
}

// Load attempts to populate the DIMM table from available data sources:
//
//  1. Cached dmidecode output at /var/run/hw-fault-exporter/dmidecode.cache
//  2. Raw SMBIOS binary entries under {sysfsRoot}/firmware/dmi/entries/17-*/
//
// If neither source is available, Load returns nil (no error) and All() will
// return an empty slice.  This keeps the exporter functional on systems without
// DMI access.
func (m *DIMMMapper) Load() error {
	// Try cached dmidecode output first — always most portable.
	cachePath := "/var/run/hw-fault-exporter/dmidecode.cache"
	if f, err := os.Open(cachePath); err == nil {
		defer f.Close()
		m.logger.Debug("DIMM: loading from dmidecode cache", zap.String("path", cachePath))
		if parseErr := m.ParseDMIDecodeOutput(f); parseErr != nil {
			m.logger.Warn("DIMM: failed to parse dmidecode cache",
				zap.String("path", cachePath), zap.Error(parseErr))
		} else {
			m.logger.Info("DIMM: loaded from dmidecode cache",
				zap.Int("dimms", len(m.dimms)))
			return nil
		}
	}

	// Fall back to raw SMBIOS binary entries in sysfs.
	dmiRoot := filepath.Join(m.sysfsRoot, "firmware", "dmi", "entries")
	if err := m.loadFromSysfs(dmiRoot); err != nil {
		m.logger.Warn("DIMM: sysfs DMI not available — DIMM mapping disabled",
			zap.String("path", dmiRoot), zap.Error(err))
		// Not fatal — return nil so the exporter continues without DIMM info.
		return nil
	}
	m.logger.Info("DIMM: loaded from sysfs DMI", zap.Int("dimms", len(m.dimms)))
	return nil
}

// loadFromSysfs reads raw SMBIOS Type 17 entries from
// {dmiRoot}/17-<N>/raw binary files.
//
// Each "raw" file contains the complete Type 17 structure: formatted area
// followed by the null-terminated string table.
func (m *DIMMMapper) loadFromSysfs(dmiRoot string) error {
	pattern := filepath.Join(dmiRoot, "17-*", "raw")
	matches, err := filepath.Glob(pattern)
	if err != nil {
		return fmt.Errorf("glob %s: %w", pattern, err)
	}
	if len(matches) == 0 {
		return fmt.Errorf("no Type 17 entries found under %s", dmiRoot)
	}

	for _, rawPath := range matches {
		data, err := os.ReadFile(rawPath)
		if err != nil {
			m.logger.Debug("DIMM: cannot read DMI raw file",
				zap.String("path", rawPath), zap.Error(err))
			continue
		}
		info, err := parseSMBIOSType17(data)
		if err != nil {
			m.logger.Debug("DIMM: cannot parse Type 17 entry",
				zap.String("path", rawPath), zap.Error(err))
			continue
		}
		inferEDACLocation(info)
		m.dimms = append(m.dimms, *info)
	}
	return nil
}

// parseSMBIOSType17 decodes a raw SMBIOS Type 17 binary blob.
func parseSMBIOSType17(data []byte) (*DIMMInfo, error) {
	if len(data) < smbiosType17MinLen {
		return nil, fmt.Errorf("Type 17 entry too short: %d bytes", len(data))
	}
	if data[0] != 0x11 {
		return nil, fmt.Errorf("not a Type 17 entry (type=%d)", data[0])
	}

	fmtLen := int(data[1]) // Length of the formatted (fixed) area.
	handle := binary.LittleEndian.Uint16(data[2:4])
	rawSize := binary.LittleEndian.Uint16(data[0x0C : 0x0C+2])
	rawSpeed := uint32(0)
	if fmtLen > 0x15 {
		rawSpeed = uint32(binary.LittleEndian.Uint16(data[0x15 : 0x15+2]))
	}

	// String indices (1-based; 0 means "not specified").
	idxLocator := int(data[0x10])
	idxBankLoc := int(data[0x11])
	idxMfr := 0
	idxSerial := 0
	idxPart := 0
	if fmtLen > 0x1A {
		idxMfr = int(data[0x17])
		idxSerial = int(data[0x18])
		idxPart = int(data[0x1A])
	}

	// Parse the string table that follows the formatted area.
	strs := parseSMBIOSStrings(data[fmtLen:])

	getString := func(idx int) string {
		if idx < 1 || idx > len(strs) {
			return ""
		}
		return strs[idx-1]
	}

	// Decode size: bit 15 set → units are KB, otherwise MB.
	var sizeMB uint64
	if rawSize == 0xFFFF {
		// Extended size at offset 0x1C (SMBIOS 2.7+).
		if fmtLen >= 0x20 {
			extSizeMB := binary.LittleEndian.Uint32(data[0x1C : 0x1C+4])
			sizeMB = uint64(extSizeMB &^ uint32(1<<31)) // mask off "Granularity" bit
		}
	} else if rawSize&0x8000 != 0 {
		sizeMB = uint64(rawSize&0x7FFF) / 1024 // KB → MB
	} else {
		sizeMB = uint64(rawSize)
	}

	memType := decodeSMBIOSMemType(data[0x12])

	return &DIMMInfo{
		Handle:       handle,
		Locator:      getString(idxLocator),
		BankLocator:  getString(idxBankLoc),
		SizeMB:       sizeMB,
		Type:         memType,
		SpeedMHz:     rawSpeed,
		Manufacturer: getString(idxMfr),
		SerialNumber: getString(idxSerial),
		PartNumber:   strs.trimmed(idxPart),
	}, nil
}

// parseSMBIOSStrings splits the SMBIOS string section (double-null terminated)
// into a slice of strings.  Indices into this slice are 0-based; callers must
// subtract 1 from the SMBIOS 1-based index.
func parseSMBIOSStrings(data []byte) smbiosStringTable {
	var result []string
	for len(data) > 0 {
		if data[0] == 0 {
			break // Double null — end of string table.
		}
		end := 0
		for end < len(data) && data[end] != 0 {
			end++
		}
		result = append(result, string(data[:end]))
		if end+1 >= len(data) {
			break
		}
		data = data[end+1:]
	}
	return result
}

// smbiosStringTable wraps []string to add a helper used during parsing.
type smbiosStringTable []string

func (t smbiosStringTable) trimmed(idx int) string {
	if idx < 1 || idx > len(t) {
		return ""
	}
	return strings.TrimSpace(t[idx-1])
}

// decodeSMBIOSMemType converts the SMBIOS Memory Type byte (offset 0x12) to a
// human-readable string.  Values are from SMBIOS 3.x Table 77.
func decodeSMBIOSMemType(b byte) string {
	types := map[byte]string{
		0x01: "Other", 0x02: "Unknown", 0x03: "DRAM", 0x04: "EDRAM",
		0x05: "VRAM", 0x06: "SRAM", 0x07: "RAM", 0x08: "ROM",
		0x09: "FLASH", 0x0A: "EEPROM", 0x0B: "FEPROM", 0x0C: "EPROM",
		0x0D: "CDRAM", 0x0E: "3DRAM", 0x0F: "SDRAM", 0x10: "SGRAM",
		0x11: "RDRAM", 0x12: "DDR", 0x13: "DDR2", 0x14: "DDR2 FB-DIMM",
		0x18: "DDR3", 0x1A: "DDR4", 0x1B: "LPDDR", 0x1C: "LPDDR2",
		0x1D: "LPDDR3", 0x1E: "LPDDR4", 0x1F: "Logical non-volatile device",
		0x20: "HBM", 0x21: "HBM2", 0x22: "DDR5", 0x23: "LPDDR5",
		0x24: "HBM3",
	}
	if s, ok := types[b]; ok {
		return s
	}
	return fmt.Sprintf("Unknown(0x%02X)", b)
}

// -----------------------------------------------------------------------------
// ParseDMIDecodeOutput parses the text output of `dmidecode -t 17`.
//
// The expected format for each DIMM is:
//
//	Handle 0x0011, DMI type 17, 84 bytes
//	Memory Device
//		Array Handle: 0x0010
//		Locator: DIMM_A1
//		Bank Locator: Node 0 Channel 0 Slot 0
//		Size: 32 GB
//		Type: DDR4
//		Speed: 3200 MT/s
//		Manufacturer: Samsung
//		Serial Number: 12345678
//		Part Number: M393A4K40EB3-CWE
//		...
// -----------------------------------------------------------------------------

var (
	reDMIHandle   = regexp.MustCompile(`^Handle\s+(0x[0-9A-Fa-f]+)`)
	reDMIField    = regexp.MustCompile(`^\s+([^:]+):\s+(.*)$`)
	reDMISizeGB   = regexp.MustCompile(`^(\d+)\s*GB$`)
	reDMISizeMB   = regexp.MustCompile(`^(\d+)\s*MB$`)
	reDMISpeedMT  = regexp.MustCompile(`^(\d+)\s*MT/s$`)
	reDMISpeedMHz = regexp.MustCompile(`^(\d+)\s*MHz$`)
)

// ParseDMIDecodeOutput parses `dmidecode -t 17` text into m.dimms.
// r should be the stdout of dmidecode or a file containing its output.
func (m *DIMMMapper) ParseDMIDecodeOutput(r io.Reader) error {
	scanner := bufio.NewScanner(r)

	var cur *DIMMInfo
	inMemoryDevice := false

	flush := func() {
		if cur != nil {
			inferEDACLocation(cur)
			m.dimms = append(m.dimms, *cur)
			cur = nil
		}
		inMemoryDevice = false
	}

	for scanner.Scan() {
		line := scanner.Text()

		// Detect the start of a new Handle record.
		if hm := reDMIHandle.FindStringSubmatch(line); hm != nil {
			flush()
			handle64, _ := strconv.ParseUint(strings.TrimPrefix(hm[1], "0x"), 16, 16)
			cur = &DIMMInfo{
				Handle:      uint16(handle64),
				EDACCsrow:   -1,
				EDACChannel: -1,
			}
			continue
		}

		// Detect "Memory Device" section header.
		if strings.TrimSpace(line) == "Memory Device" {
			inMemoryDevice = true
			continue
		}

		if !inMemoryDevice || cur == nil {
			continue
		}

		// Empty line ends the current section.
		if strings.TrimSpace(line) == "" {
			continue
		}

		fm := reDMIField.FindStringSubmatch(line)
		if fm == nil {
			continue
		}
		key := strings.TrimSpace(fm[1])
		val := strings.TrimSpace(fm[2])

		switch key {
		case "Locator":
			cur.Locator = val
		case "Bank Locator":
			cur.BankLocator = val
		case "Size":
			if val == "No Module Installed" || val == "Unknown" {
				cur.SizeMB = 0
			} else if sm := reDMISizeGB.FindStringSubmatch(val); sm != nil {
				gb, _ := strconv.ParseUint(sm[1], 10, 64)
				cur.SizeMB = gb * 1024
			} else if sm := reDMISizeMB.FindStringSubmatch(val); sm != nil {
				cur.SizeMB, _ = strconv.ParseUint(sm[1], 10, 64)
			}
		case "Type":
			if val != "Unknown" {
				cur.Type = val
			}
		case "Speed", "Configured Memory Speed":
			if sm := reDMISpeedMT.FindStringSubmatch(val); sm != nil {
				// MT/s ≈ MHz for DDR (2× per clock, but the number is the same).
				spd, _ := strconv.ParseUint(sm[1], 10, 32)
				cur.SpeedMHz = uint32(spd)
			} else if sm := reDMISpeedMHz.FindStringSubmatch(val); sm != nil {
				spd, _ := strconv.ParseUint(sm[1], 10, 32)
				cur.SpeedMHz = uint32(spd)
			}
		case "Manufacturer":
			if val != "Unknown" && val != "Not Specified" {
				cur.Manufacturer = val
			}
		case "Serial Number":
			if val != "Unknown" && val != "Not Specified" {
				cur.SerialNumber = val
			}
		case "Part Number":
			cur.PartNumber = strings.TrimSpace(val)
		}
	}
	flush()

	if err := scanner.Err(); err != nil {
		return fmt.Errorf("scan dmidecode output: %w", err)
	}
	return nil
}

// inferEDACLocation attempts to derive EDAC mc/csrow/channel indices from the
// DIMM's BankLocator string.  Many BIOS implementations encode this as one of:
//
//	"Node 0 Channel 0 Slot 0"
//	"BANK 0"
//	"P0_Node0_Channel0_Dimm0"
//
// Heuristic regexps handle the common forms.  If parsing fails, the EDAC
// fields are left at their zero/negative defaults.
func inferEDACLocation(d *DIMMInfo) {
	if d == nil {
		return
	}
	d.EDACCsrow = -1
	d.EDACChannel = -1
	d.EDACController = ""

	bl := d.BankLocator

	// Pattern: "Node <N> Channel <C> Slot <S>" or similar.
	reNodeChanSlot := regexp.MustCompile(
		`(?i)node\s+(\d+)[^0-9]+channel\s+(\d+)[^0-9]+(?:slot|dimm)\s+(\d+)`,
	)
	if m := reNodeChanSlot.FindStringSubmatch(bl); m != nil {
		node, _ := strconv.Atoi(m[1])
		channel, _ := strconv.Atoi(m[2])
		slot, _ := strconv.Atoi(m[3])
		// EDAC maps: mc<node>, csrow = slot * channels_per_csrow, channel = channel.
		d.EDACController = fmt.Sprintf("mc%d", node)
		d.EDACCsrow = slot
		d.EDACChannel = channel
		return
	}

	// Pattern: "P<N>_Node<N>_Channel<C>_Dimm<S>" (HPE iLO format).
	reHPE := regexp.MustCompile(`(?i)Node(\d+)_Channel(\d+)_Dimm(\d+)`)
	if m := reHPE.FindStringSubmatch(bl); m != nil {
		node, _ := strconv.Atoi(m[1])
		channel, _ := strconv.Atoi(m[2])
		slot, _ := strconv.Atoi(m[3])
		d.EDACController = fmt.Sprintf("mc%d", node)
		d.EDACCsrow = slot
		d.EDACChannel = channel
		return
	}

	// Pattern: Try to parse controller from Device Locator (e.g. "DIMM_A1" → mc0, ch0).
	// A=0, B=1, … for channel; digit suffix for slot.
	reLocator := regexp.MustCompile(`(?i)DIMM[_\s]?([A-H])(\d+)`)
	if m := reLocator.FindStringSubmatch(d.Locator); m != nil {
		ch := int(strings.ToUpper(m[1])[0] - 'A')
		slot, _ := strconv.Atoi(m[2])
		d.EDACController = "mc0"
		d.EDACCsrow = slot
		d.EDACChannel = ch
	}
}

// MapEDACLocation returns the DIMMInfo for the given EDAC controller, csrow,
// and channel, or nil if no matching entry is found.
func (m *DIMMMapper) MapEDACLocation(controller string, csrow, channel int) *DIMMInfo {
	for i := range m.dimms {
		d := &m.dimms[i]
		if d.EDACController == controller &&
			d.EDACCsrow == csrow &&
			d.EDACChannel == channel {
			return d
		}
	}
	return nil
}

// All returns a copy of all parsed DIMM entries.
func (m *DIMMMapper) All() []DIMMInfo {
	result := make([]DIMMInfo, len(m.dimms))
	copy(result, m.dimms)
	return result
}

// -----------------------------------------------------------------------------
// DIMMCollector — Prometheus collector
// -----------------------------------------------------------------------------

// DIMMCollector implements prometheus.Collector, emitting an info-style metric
// hw_dimm_info with one time series per DIMM slot.  The metric value is always
// 1; all useful data is carried in labels.
//
// This pattern is the standard Prometheus approach for "inventory" or "info"
// data that doesn't change frequently but needs to be joinable with alert
// queries in PromQL.
type DIMMCollector struct {
	opts     Options
	mapper   *DIMMMapper
	dimmInfo *prometheus.Desc
}

// NewDIMMCollector creates a DIMMCollector.
func NewDIMMCollector(opts Options) *DIMMCollector {
	return &DIMMCollector{
		opts:   opts,
		mapper: NewDIMMMapper(opts.SysfsRoot, opts.Logger),
		dimmInfo: prometheus.NewDesc(
			"hw_dimm_info",
			"Information about an installed DIMM module (info-style metric, value is always 1).",
			[]string{
				"controller",   // EDAC mc index, e.g. "mc0"
				"csrow",        // EDAC csrow index as string
				"channel",      // EDAC channel index as string
				"locator",      // SMBIOS Device Locator, e.g. "DIMM_A1"
				"bank_locator", // SMBIOS Bank Locator, e.g. "Node 0 Channel 0 Slot 0"
				"type",         // Memory type, e.g. "DDR4"
				"manufacturer", // JEDEC manufacturer name
				"part_number",  // Module part number
				"size_mb",      // Installed size in MiB as string
				"speed_mhz",    // Configured speed in MHz as string
			},
			nil,
		),
	}
}

// Describe implements prometheus.Collector.
func (c *DIMMCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.dimmInfo
}

// Collect implements prometheus.Collector.
// Loads the DIMM map on every scrape so that hot-add/replace events are
// reflected without restarting the exporter.
func (c *DIMMCollector) Collect(ch chan<- prometheus.Metric) {
	// Re-create the mapper on each scrape so newly populated cache files are
	// picked up automatically.
	mapper := NewDIMMMapper(c.opts.SysfsRoot, c.opts.Logger)
	if err := mapper.Load(); err != nil {
		c.opts.Logger.Warn("DIMM: load failed", zap.Error(err))
		return
	}

	for _, d := range mapper.All() {
		// Skip empty slots.
		if d.SizeMB == 0 && d.Locator == "" {
			continue
		}

		ctrl := d.EDACController
		if ctrl == "" {
			ctrl = "unknown"
		}
		csrow := "unknown"
		if d.EDACCsrow >= 0 {
			csrow = strconv.Itoa(d.EDACCsrow)
		}
		channel := "unknown"
		if d.EDACChannel >= 0 {
			channel = strconv.Itoa(d.EDACChannel)
		}

		ch <- prometheus.MustNewConstMetric(
			c.dimmInfo,
			prometheus.GaugeValue,
			1.0,
			ctrl,
			csrow,
			channel,
			d.Locator,
			d.BankLocator,
			d.Type,
			d.Manufacturer,
			strings.TrimSpace(d.PartNumber),
			strconv.FormatUint(d.SizeMB, 10),
			strconv.FormatUint(uint64(d.SpeedMHz), 10),
		)
	}
}
