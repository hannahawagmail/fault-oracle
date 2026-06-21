// SPDX-License-Identifier: Apache-2.0
//
// collectors/smartnic_test.go — Tests for SmartNICCollector.
//
// Uses a mock sysfs tree under a tmpdir to avoid requiring real NIC hardware.

package collectors_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
	"go.uber.org/zap"
)

// buildMockNetRoot creates a minimal /sys/class/net tree with one or more NICs.
func buildMockNetRoot(t *testing.T, ifaces map[string]map[string]string) string {
	t.Helper()
	root := t.TempDir()
	netDir := filepath.Join(root, "class", "net")

	for name, vals := range ifaces {
		statsDir := filepath.Join(netDir, name, "statistics")
		if err := os.MkdirAll(statsDir, 0755); err != nil {
			t.Fatalf("mkdir stats: %v", err)
		}
		// Write statistics files
		for k, v := range vals {
			if k == "operstate" || k == "speed" {
				// top-level files, not under statistics/
				if err := os.WriteFile(filepath.Join(netDir, name, k), []byte(v+"\n"), 0644); err != nil {
					t.Fatalf("write %s: %v", k, err)
				}
				continue
			}
			if err := os.WriteFile(filepath.Join(statsDir, k), []byte(v+"\n"), 0644); err != nil {
				t.Fatalf("write stat %s: %v", k, err)
			}
		}
	}
	return root
}

func newSmartNICCollector(sysfsRoot string) *SmartNICCollector {
	return NewSmartNICCollector(Options{
		SysfsRoot: sysfsRoot,
		Logger:    zap.NewNop(),
	})
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

func TestSmartNICCollector_SingleInterface(t *testing.T) {
	root := buildMockNetRoot(t, map[string]map[string]string{
		"eth0": {
			"operstate": "up",
			"speed":     "1000",
			"rx_bytes":  "100000",
			"tx_bytes":  "200000",
			"rx_errors": "0",
			"tx_errors": "0",
			"rx_dropped": "0",
			"tx_dropped": "0",
		},
	})

	c := newSmartNICCollector(root)
	reg := prometheus.NewPedanticRegistry()
	reg.MustRegister(c)

	problems, err := testutil.GatherAndLint(reg)
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	if len(problems) > 0 {
		t.Errorf("lint problems: %v", problems)
	}

	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather metrics: %v", err)
	}

	names := make(map[string]bool)
	for _, mf := range mfs {
		names[mf.GetName()] = true
	}
	for _, want := range []string{
		"smartnic_rx_bytes_total",
		"smartnic_tx_bytes_total",
		"smartnic_link_up",
		"smartnic_link_speed_mbps",
		"fault_resilience_collector_up",
	} {
		if !names[want] {
			t.Errorf("missing metric: %s", want)
		}
	}
}

func TestSmartNICCollector_LinkUpValue(t *testing.T) {
	root := buildMockNetRoot(t, map[string]map[string]string{
		"eth0": {"operstate": "up", "speed": "10000", "rx_bytes": "0", "tx_bytes": "0"},
		"eth1": {"operstate": "down", "speed": "-1", "rx_bytes": "0", "tx_bytes": "0"},
	})

	c := newSmartNICCollector(root)
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)

	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}

	byIface := map[string]float64{}
	for _, mf := range mfs {
		if mf.GetName() != "smartnic_link_up" {
			continue
		}
		for _, m := range mf.GetMetric() {
			var iface string
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "iface" {
					iface = lp.GetValue()
				}
			}
			byIface[iface] = m.GetGauge().GetValue()
		}
	}

	if byIface["eth0"] != 1.0 {
		t.Errorf("eth0 link_up: want 1, got %v", byIface["eth0"])
	}
	if byIface["eth1"] != 0.0 {
		t.Errorf("eth1 link_up: want 0, got %v", byIface["eth1"])
	}
}

func TestSmartNICCollector_LoopbackExcluded(t *testing.T) {
	root := buildMockNetRoot(t, map[string]map[string]string{
		"lo":   {"operstate": "unknown", "speed": "0", "rx_bytes": "999"},
		"eth0": {"operstate": "up", "speed": "1000", "rx_bytes": "1"},
	})

	c := newSmartNICCollector(root)
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)

	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}

	for _, mf := range mfs {
		for _, m := range mf.GetMetric() {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "iface" && lp.GetValue() == "lo" {
					t.Errorf("loopback 'lo' should be excluded, found in %s", mf.GetName())
				}
			}
		}
	}
}

func TestSmartNICCollector_MissingSysfsReturnsCollectorDown(t *testing.T) {
	c := newSmartNICCollector("/nonexistent/sysfs/path")
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)

	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}

	found := false
	for _, mf := range mfs {
		if mf.GetName() != "fault_resilience_collector_up" {
			continue
		}
		for _, m := range mf.GetMetric() {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "collector" && lp.GetValue() == "smartnic" {
					found = true
					if m.GetGauge().GetValue() != 0 {
						t.Errorf("collector_up should be 0 when sysfs missing, got %v", m.GetGauge().GetValue())
					}
				}
			}
		}
	}
	if !found {
		t.Error("fault_resilience_collector_up{collector=smartnic} not found")
	}
}

func TestSmartNICCollector_Describe(t *testing.T) {
	c := newSmartNICCollector("/nonexistent")
	ch := make(chan *prometheus.Desc, 64)
	c.Describe(ch)
	close(ch)

	var descs []string
	for d := range ch {
		descs = append(descs, d.String())
	}
	if len(descs) == 0 {
		t.Error("Describe produced no descriptors")
	}
	found := false
	for _, d := range descs {
		if strings.Contains(d, "smartnic_rx_bytes_total") {
			found = true
		}
	}
	if !found {
		t.Error("smartnic_rx_bytes_total not found in Describe output")
	}
}

func TestSmartNICCollector_SpeedNegativeClampedToZero(t *testing.T) {
	root := buildMockNetRoot(t, map[string]map[string]string{
		"eth0": {"operstate": "down", "speed": "-1", "rx_bytes": "0", "tx_bytes": "0"},
	})

	c := newSmartNICCollector(root)
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)

	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}

	for _, mf := range mfs {
		if mf.GetName() != "smartnic_link_speed_mbps" {
			continue
		}
		for _, m := range mf.GetMetric() {
			if m.GetGauge().GetValue() < 0 {
				t.Errorf("speed should be clamped to >=0, got %v", m.GetGauge().GetValue())
			}
		}
	}
}
