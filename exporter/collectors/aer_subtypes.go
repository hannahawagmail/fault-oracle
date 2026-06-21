// SPDX-License-Identifier: Apache-2.0
//
// collectors/aer_subtypes.go — Per-subtype AER error parsing helpers.
//
// The kernel exposes AER error counts as space-separated "Name Count" pairs in
// aer_dev_correctable, aer_dev_nonfatal, and aer_dev_fatal. This file provides
// parsers that break those counts into individual Prometheus metrics with an
// error_subtype label.

package collectors

import (
	"os"
	"strconv"
	"strings"
)

// aerSubtype maps a kernel AER error name to its canonical label.
var aerCorrectableSubtypes = map[string]string{
	"RxErr":        "receiver_error",
	"BadTLP":       "bad_tlp",
	"BadDLLP":      "bad_dllp",
	"Rollover":     "replay_num_rollover",
	"Timeout":      "replay_timer_timeout",
	"NonFatalErr":  "advisory_non_fatal",
	"CorrIntErr":   "corrected_internal_error",
	"HeaderOF":     "header_log_overflow",
}

var aerNonfatalSubtypes = map[string]string{
	"DLP":          "data_link_protocol_error",
	"SDES":         "surprise_down",
	"TLP":          "poisoned_tlp",
	"FCP":          "flow_control_protocol",
	"CmpltTO":      "completion_timeout",
	"CmpltAbrt":    "completer_abort",
	"UnxCmplt":     "unexpected_completion",
	"RxOF":         "receiver_overflow",
	"MalfTLP":      "malformed_tlp",
	"ECRC":         "ecrc_error",
	"UnsupReq":     "unsupported_request",
	"ACSViol":      "acs_violation",
	"UncorrIntErr": "uncorrectable_internal_error",
}

var aerFatalSubtypes = map[string]string{
	"DLP":     "data_link_protocol_error",
	"SDES":    "surprise_down",
	"TLP":     "poisoned_tlp",
	"MalfTLP": "malformed_tlp",
}

// aerParsedCount holds a single parsed AER error count.
type aerParsedCount struct {
	subtype string
	value   float64
}

// parseAERSysfsFile reads a space-separated "Name Count Name Count ..." AER sysfs file
// and returns parsed counts, skipping the TOTAL_ERR_* summary entry.
func parseAERSysfsFile(path string, subtypeMap map[string]string) ([]aerParsedCount, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}

	tokens := strings.Fields(string(data))
	var out []aerParsedCount

	for i := 0; i+1 < len(tokens); i += 2 {
		name := tokens[i]
		countStr := tokens[i+1]

		// Skip TOTAL_ERR_* summary entries
		if strings.HasPrefix(name, "TOTAL_ERR") {
			continue
		}

		count, err := strconv.ParseFloat(countStr, 64)
		if err != nil {
			continue
		}

		label, ok := subtypeMap[name]
		if !ok {
			label = strings.ToLower(name) // fallback: use raw name
		}
		out = append(out, aerParsedCount{subtype: label, value: count})
	}
	return out, nil
}
