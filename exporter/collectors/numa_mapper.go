// SPDX-License-Identifier: Apache-2.0
//
// collectors/numa_mapper.go — Maps EDAC memory controller indices to NUMA nodes.
//
// On multi-socket or NUMA ARM systems, each EDAC mc<N> controller corresponds
// to a NUMA node. The kernel links them via:
//   /sys/devices/system/edac/mc/mc<N>/device/numa_node   (direct)
// or via the PCI device:
//   /sys/devices/system/edac/mc/mc<N>/device -> PCI device
//   /sys/bus/pci/devices/<bdf>/numa_node
//
// Falls back to "unknown" when the link is not resolvable.

package collectors

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// NUMAMapper caches mc_index → NUMA node mappings.
type NUMAMapper struct {
	sysfsRoot string
	cache     map[int]string // mc_index → "0", "1", ... or "unknown"
}

// NewNUMAMapper creates a NUMAMapper. The mapping is built lazily on first call.
func NewNUMAMapper(sysfsRoot string) *NUMAMapper {
	return &NUMAMapper{
		sysfsRoot: sysfsRoot,
		cache:     make(map[int]string),
	}
}

// NodeForMC returns the NUMA node string for a given memory controller index.
func (m *NUMAMapper) NodeForMC(mcIdx int) string {
	if node, ok := m.cache[mcIdx]; ok {
		return node
	}
	node := m.resolveNode(mcIdx)
	m.cache[mcIdx] = node
	return node
}

func (m *NUMAMapper) resolveNode(mcIdx int) string {
	mcPath := filepath.Join(m.sysfsRoot, "devices", "system", "edac", "mc",
		fmt.Sprintf("mc%d", mcIdx))

	// Method 1: direct numa_node file under mc device
	numaFile := filepath.Join(mcPath, "device", "numa_node")
	if data, err := os.ReadFile(numaFile); err == nil {
		node := strings.TrimSpace(string(data))
		if node != "" && node != "-1" {
			return node
		}
	}

	// Method 2: follow the device symlink to PCI device and read numa_node there
	deviceLink := filepath.Join(mcPath, "device")
	target, err := os.Readlink(deviceLink)
	if err == nil {
		// target is a relative path like ../../../0000:00:00.0
		pciDir := filepath.Join(filepath.Dir(deviceLink), target)
		pciNumaFile := filepath.Join(pciDir, "numa_node")
		if data, err := os.ReadFile(pciNumaFile); err == nil {
			node := strings.TrimSpace(string(data))
			if node != "" && node != "-1" {
				return node
			}
		}
	}

	// Method 3: infer from /sys/devices/system/node/node*/memory* symlinks
	// Each NUMA node lists its memory blocks. This is best-effort.
	nodeDir := filepath.Join(m.sysfsRoot, "devices", "system", "node")
	nodes, err := os.ReadDir(nodeDir)
	if err == nil {
		for _, n := range nodes {
			name := n.Name()
			if !strings.HasPrefix(name, "node") {
				continue
			}
			nodeID := strings.TrimPrefix(name, "node")
			// Check if this node has mc<N> in its memory block list
			memCtrlPath := filepath.Join(nodeDir, name, fmt.Sprintf("memory_mc%d", mcIdx))
			if _, err := os.Stat(memCtrlPath); err == nil {
				return nodeID
			}
		}
	}

	return "unknown"
}
