// SPDX-License-Identifier: Apache-2.0
package collectors

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

func buildMockNUMARoot(t *testing.T, mcToNode map[int]string) string {
	t.Helper()
	root := t.TempDir()
	for mc, node := range mcToNode {
		mcDevDir := filepath.Join(root, "devices", "system", "edac", "mc",
			fmt.Sprintf("mc%d", mc), "device")
		os.MkdirAll(mcDevDir, 0755)
		os.WriteFile(filepath.Join(mcDevDir, "numa_node"), []byte(node+"\n"), 0644)
	}
	return root
}

func TestNUMAMapper_DirectNUMAFile(t *testing.T) {
	root := buildMockNUMARoot(t, map[int]string{0: "0", 1: "1"})
	m := NewNUMAMapper(root)
	if got := m.NodeForMC(0); got != "0" {
		t.Errorf("mc0 node: want '0', got %q", got)
	}
	if got := m.NodeForMC(1); got != "1" {
		t.Errorf("mc1 node: want '1', got %q", got)
	}
}

func TestNUMAMapper_UnknownWhenMissing(t *testing.T) {
	m := NewNUMAMapper(t.TempDir())
	if got := m.NodeForMC(99); got != "unknown" {
		t.Errorf("missing mc: want 'unknown', got %q", got)
	}
}

func TestNUMAMapper_CachesResult(t *testing.T) {
	root := buildMockNUMARoot(t, map[int]string{0: "0"})
	m := NewNUMAMapper(root)
	got1 := m.NodeForMC(0)
	os.RemoveAll(filepath.Join(root, "devices"))
	got2 := m.NodeForMC(0)
	if got1 != got2 {
		t.Errorf("cache mismatch: %q vs %q", got1, got2)
	}
}

func TestNUMAMapper_NegativeOneIgnored(t *testing.T) {
	root := t.TempDir()
	mcDevDir := filepath.Join(root, "devices", "system", "edac", "mc", "mc0", "device")
	os.MkdirAll(mcDevDir, 0755)
	os.WriteFile(filepath.Join(mcDevDir, "numa_node"), []byte("-1\n"), 0644)
	m := NewNUMAMapper(root)
	got := m.NodeForMC(0)
	if got == "-1" {
		t.Error("should not return raw -1 as NUMA node")
	}
}

func TestNUMAMapper_MultipleNodes(t *testing.T) {
	root := buildMockNUMARoot(t, map[int]string{0: "0", 1: "0", 2: "1", 3: "1"})
	m := NewNUMAMapper(root)
	for mc, want := range map[int]string{0: "0", 1: "0", 2: "1", 3: "1"} {
		if got := m.NodeForMC(mc); got != want {
			t.Errorf("mc%d: want node %q, got %q", mc, want, got)
		}
	}
}
