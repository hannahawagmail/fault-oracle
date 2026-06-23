package collectors

import (
	"strings"
	"testing"
)

func TestParseDMIDecodeOutput(t *testing.T) {
	input := `Handle 0x0011, DMI type 17, 84 bytes
Memory Device
	Locator: DIMM_A1
	Bank Locator: Node 0 Channel 0 Slot 0
	Size: 32 GB
	Type: DDR4
	Speed: 3200 MT/s
	Manufacturer: Samsung
	Serial Number: 12345678
	Part Number: M393A4K40EB3-CWE
`
	m := NewDIMMMapper("/sys", nil)
	if err := m.ParseDMIDecodeOutput(strings.NewReader(input)); err != nil {
		t.Fatalf("ParseDMIDecodeOutput: %v", err)
	}
	dimms := m.All()
	if len(dimms) != 1 {
		t.Fatalf("expected 1 DIMM, got %d", len(dimms))
	}
	d := dimms[0]
	if d.Handle != 0x0011 {
		t.Errorf("Handle = 0x%04X, want 0x0011", d.Handle)
	}
	if d.SizeMB != 32*1024 {
		t.Errorf("SizeMB = %d, want %d", d.SizeMB, 32*1024)
	}
	if d.Type != "DDR4" {
		t.Errorf("Type = %q, want DDR4", d.Type)
	}
	if d.SpeedMHz != 3200 {
		t.Errorf("SpeedMHz = %d, want 3200", d.SpeedMHz)
	}
	if d.EDACController != "mc0" || d.EDACCsrow != 0 || d.EDACChannel != 0 {
		t.Errorf("EDAC = (%s, %d, %d), want (mc0, 0, 0)", d.EDACController, d.EDACCsrow, d.EDACChannel)
	}
}

func TestParseSMBIOSType17(t *testing.T) {
	// Minimal Type 17 binary: formatted area + string table.
	buf := make([]byte, 0x1B) // formatted area length = 0x1B
	buf[0] = 0x11             // Type
	buf[1] = 0x1B             // Length
	buf[2] = 0x20             // Handle low
	buf[3] = 0x00             // Handle high
	buf[0x0C] = 0x00          // Size low = 16384 MB
	buf[0x0D] = 0x40          // Size high
	buf[0x10] = 1             // Device Locator → string 1
	buf[0x11] = 2             // Bank Locator → string 2
	buf[0x12] = 0x1A          // Memory Type = DDR4
	buf[0x15] = 0x80          // Speed low = 2933
	buf[0x16] = 0x0B          // Speed high
	buf[0x17] = 3             // Manufacturer → string 3
	buf[0x18] = 4             // Serial → string 4
	buf[0x1A] = 5             // Part Number → string 5
	// String table (after formatted area).
	strs := "DIMM_B0\x00Node 1 Channel 1 Slot 0\x00Micron\x00SN999\x00  MTA18ASF2G  \x00\x00"
	data := append(buf, []byte(strs)...)

	info, err := parseSMBIOSType17(data)
	if err != nil {
		t.Fatalf("parseSMBIOSType17: %v", err)
	}
	if info.Handle != 0x0020 {
		t.Errorf("Handle = 0x%04X, want 0x0020", info.Handle)
	}
	if info.SizeMB != 16384 {
		t.Errorf("SizeMB = %d, want 16384", info.SizeMB)
	}
	if info.Type != "DDR4" {
		t.Errorf("Type = %q, want DDR4", info.Type)
	}
	if info.PartNumber != "MTA18ASF2G" {
		t.Errorf("PartNumber = %q, want trimmed", info.PartNumber)
	}
}

func TestDecodeSMBIOSMemType(t *testing.T) {
	cases := []struct {
		code byte
		want string
	}{
		{0x1A, "DDR4"}, {0x22, "DDR5"}, {0x23, "LPDDR5"}, {0x20, "HBM"}, {0xFF, "Unknown(0xFF)"},
	}
	for _, c := range cases {
		if got := decodeSMBIOSMemType(c.code); got != c.want {
			t.Errorf("decodeSMBIOSMemType(0x%02X) = %q, want %q", c.code, got, c.want)
		}
	}
}

func TestInferEDACLocation(t *testing.T) {
	d := &DIMMInfo{BankLocator: "Node 2 Channel 1 Slot 3"}
	inferEDACLocation(d)
	if d.EDACController != "mc2" || d.EDACCsrow != 3 || d.EDACChannel != 1 {
		t.Errorf("got (%s,%d,%d), want (mc2,3,1)", d.EDACController, d.EDACCsrow, d.EDACChannel)
	}

	d2 := &DIMMInfo{Locator: "DIMM_C2"}
	inferEDACLocation(d2)
	if d2.EDACController != "mc0" || d2.EDACChannel != 2 || d2.EDACCsrow != 2 {
		t.Errorf("locator got (%s,%d,%d), want (mc0,2,2)", d2.EDACController, d2.EDACCsrow, d2.EDACChannel)
	}
}

func TestTrimmed(t *testing.T) {
	tbl := smbiosStringTable{"hello", "  padded  ", "clean"}
	if got := tbl.trimmed(2); got != "padded" {
		t.Errorf("trimmed(2) = %q, want %q", got, "padded")
	}
	if got := tbl.trimmed(0); got != "" {
		t.Errorf("trimmed(0) = %q, want empty", got)
	}
	if got := tbl.trimmed(99); got != "" {
		t.Errorf("trimmed(99) = %q, want empty", got)
	}
}
