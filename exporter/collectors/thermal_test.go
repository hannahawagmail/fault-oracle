// SPDX-License-Identifier: Apache-2.0
package collectors_test

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
	"go.uber.org/zap"
)

func buildMockThermalRoot(t *testing.T, zones map[string][2]string, coolers map[string][2]string) string {
	t.Helper()
	root := t.TempDir()
	thermalDir := filepath.Join(root, "class", "thermal")

	for zone, vals := range zones { // vals: [type, temp_millideg]
		d := filepath.Join(thermalDir, zone)
		os.MkdirAll(d, 0755)
		os.WriteFile(filepath.Join(d, "type"), []byte(vals[0]+"\n"), 0644)
		os.WriteFile(filepath.Join(d, "temp"), []byte(vals[1]+"\n"), 0644)
	}
	for cooler, vals := range coolers { // vals: [type, cur_state]
		d := filepath.Join(thermalDir, cooler)
		os.MkdirAll(d, 0755)
		os.WriteFile(filepath.Join(d, "type"), []byte(vals[0]+"\n"), 0644)
		os.WriteFile(filepath.Join(d, "cur_state"), []byte(vals[1]+"\n"), 0644)
	}
	return root
}

func TestThermalCollector_ZoneTemperature(t *testing.T) {
	root := buildMockThermalRoot(t,
		map[string][2]string{"thermal_zone0": {"cpu-thermal", "55000"}},
		nil,
	)
	c := NewThermalCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)
	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	for _, mf := range mfs {
		if mf.GetName() != "cpu_thermal_zone_celsius" {
			continue
		}
		for _, m := range mf.GetMetric() {
			if m.GetGauge().GetValue() != 55.0 {
				t.Errorf("expected 55.0°C (55000 millideg), got %v", m.GetGauge().GetValue())
			}
		}
	}
}

func TestThermalCollector_CoolingDevice(t *testing.T) {
	root := buildMockThermalRoot(t, nil,
		map[string][2]string{"cooling_device0": {"Fan", "3"}},
	)
	c := NewThermalCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)
	mfs, _ := reg.Gather()
	for _, mf := range mfs {
		if mf.GetName() == "cpu_cooling_device_state" {
			for _, m := range mf.GetMetric() {
				if m.GetGauge().GetValue() != 3 {
					t.Errorf("cooling state: want 3, got %v", m.GetGauge().GetValue())
				}
			}
		}
	}
}

func TestThermalCollector_MissingSysfs(t *testing.T) {
	c := NewThermalCollector(Options{SysfsRoot: "/nonexistent", Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)
	mfs, _ := reg.Gather()
	for _, mf := range mfs {
		if mf.GetName() == "fault_resilience_collector_up" {
			for _, m := range mf.GetMetric() {
				for _, lp := range m.GetLabel() {
					if lp.GetName() == "collector" && lp.GetValue() == "thermal" {
						if m.GetGauge().GetValue() != 0 {
							t.Error("collector_up should be 0 when sysfs missing")
						}
					}
				}
			}
		}
	}
}

func buildMockCpufreqRoot(t *testing.T, cpus map[string]map[string]string) string {
	t.Helper()
	root := t.TempDir()
	for cpu, files := range cpus {
		freqDir := filepath.Join(root, "devices", "system", "cpu", cpu, "cpufreq")
		os.MkdirAll(freqDir, 0755)
		for k, v := range files {
			os.WriteFile(filepath.Join(freqDir, k), []byte(v+"\n"), 0644)
		}
	}
	return root
}

func TestCpufreqCollector_FrequencyConversion(t *testing.T) {
	root := buildMockCpufreqRoot(t, map[string]map[string]string{
		"cpu0": {
			"scaling_cur_freq": "2400000", // 2.4 GHz in kHz
			"scaling_min_freq": "600000",
			"scaling_max_freq": "3200000",
			"scaling_governor": "performance",
		},
	})
	c := NewCpufreqCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)
	mfs, _ := reg.Gather()
	for _, mf := range mfs {
		if mf.GetName() == "cpu_frequency_hz" {
			for _, m := range mf.GetMetric() {
				want := 2.4e9
				if m.GetGauge().GetValue() != want {
					t.Errorf("cpu_frequency_hz: want %v, got %v", want, m.GetGauge().GetValue())
				}
			}
		}
	}
}

func TestCpufreqCollector_GovernorLabel(t *testing.T) {
	root := buildMockCpufreqRoot(t, map[string]map[string]string{
		"cpu0": {"scaling_cur_freq": "1000000", "scaling_governor": "schedutil"},
	})
	c := NewCpufreqCollector(Options{SysfsRoot: root, Logger: zap.NewNop()})
	reg := prometheus.NewRegistry()
	reg.MustRegister(c)
	mfs, _ := reg.Gather()
	for _, mf := range mfs {
		if mf.GetName() != "cpu_governor" {
			continue
		}
		for _, m := range mf.GetMetric() {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "governor" && lp.GetValue() == "schedutil" {
					return // found
				}
			}
		}
	}
	t.Error("cpu_governor{governor=schedutil} not found")
}
