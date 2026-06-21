<!-- SPDX-License-Identifier: Apache-2.0 -->
# Hardware Compatibility Matrix

This document tracks which SoCs and boards have been tested with each module.
Community contributions are welcome — open a [Hardware Compatibility Report](../.github/ISSUE_TEMPLATE/hardware_compat.yml) issue to add your results.

**Legend:** ✅ Tested and working · ⚠️ Partial / workaround needed · ❌ Not working · 🔲 Not tested

---

## Tested platforms

| SoC | Board example | Kernel | EDAC CE/UE | Watchdog | OTA A/B | PCIe AER | Net failover | Exporter |
|-----|--------------|--------|-----------|---------|---------|---------|-------------|---------|
| QEMU virt (ARM64) | — | 6.6 LTS | ✅ (simulated) | ✅ | ✅ | ✅ (simulated) | ✅ | ✅ |
| NXP i.MX8MP | Phytec phyBOARD-Pollux | 6.1 LTS | ⚠️ `imx_ddr` driver — CE only | ✅ `imx2_wdt` | ✅ RAUC | 🔲 | ✅ | ✅ |
| Marvell OCTEON TX2 (CN9130) | SolidRun ClearFog CN9130 | 6.6 LTS | 🔲 | ✅ `orion_wdt` | 🔲 | ✅ | ✅ | ✅ |
| Ampere Altra (Q80-30) | ADLINK COM-HPC-ALT | 6.1 LTS | ✅ | ✅ `sp805_wdt` | ✅ RAUC | ✅ | ✅ | ✅ |
| Broadcom BCM2711 | Raspberry Pi 4B | 6.6 LTS | ❌ No ECC support | ✅ `bcm2835_wdt` | 🔲 | 🔲 | ✅ | ✅ |
| Broadcom BCM2712 | Raspberry Pi 5 | 6.6 LTS | 🔲 | ✅ | 🔲 | 🔲 | ✅ | 🔲 |
| Marvell Armada 8040 | Macchiatobin | 5.15 LTS | 🔲 | ✅ `orion_wdt` | 🔲 | ✅ | ✅ | ✅ |

---

## Feature notes by SoC

### QEMU virt (ARM64) — reference platform

The QEMU `virt` machine is the primary CI target. EDAC errors are injected
via `fault-injection/inject_edac_ce.sh --backend none` (simulated) or via
the `edac-reference` kernel module's debugfs interface inside the guest.
All features are fully exercised in the QEMU CI job.

### NXP i.MX8MP

- EDAC driver: `imx_ddr`. Only correctable errors (CE) are reported; UE
  counting requires a kernel patch to enable the `ue_count` sysfs file.
- Watchdog: `imx2_wdt` — timeout max 128 s. Use `WDT_TIMEOUT=120` in
  `boot-resilience/wdt-setup.sh`.
- OTA: RAUC with eMMC A/B partitions works out of the box.
- PCIe AER: requires PCIe controller clock enabled in device tree.

### Marvell OCTEON TX2 (CN9130)

- EDAC: `mvebu_edac` driver may need backporting from mainline 6.7+ for
  full CE/UE sysfs support on CN9130.
- PCIe AER: fully supported, Gen3 × 4 lanes.

### Ampere Altra

- EDAC: `qcom_edac` (upstream) or Ampere-specific out-of-tree driver.
  128-core topology requires the DIMM collector to handle interleaved
  memory controllers. See `exporter/collectors/dimm.go`.
- CCIX RAS: not yet integrated — tracked in issue #XX.

### Raspberry Pi 4B (BCM2711)

- LPDDR4X on RPi4 does not expose ECC to software. The EDAC module will
  show no memory controllers. All other modules work normally.
- Useful for testing watchdog, network resilience, and OTA flows without
  ECC hardware.

---

## Kernel driver quick reference

| Kernel module | SoCs | sysfs path |
|--------------|------|-----------|
| `edac_mc` (core) | all EDAC-capable | `/sys/devices/system/edac/mc<N>/` |
| `imx_ddr` | NXP i.MX6, i.MX8 | `/sys/devices/system/edac/mc0/` |
| `qcom_edac` | Ampere Altra, Qualcomm | `/sys/devices/system/edac/mc<N>/` |
| `mvebu_edac` | Marvell Armada, OCTEON | `/sys/devices/system/edac/mc<N>/` |
| `sp805_wdt` | ARM Versatile, Ampere | `/dev/watchdog0` |
| `imx2_wdt` | NXP i.MX | `/dev/watchdog0` |
| `bcm2835_wdt` | Broadcom BCM2835/2711 | `/dev/watchdog0` |
| `orion_wdt` | Marvell Armada/OCTEON | `/dev/watchdog0` |

---

## Contributing test results

1. Test the modules on your hardware following the instructions in each
   module's `README.md`.
2. Open a [Hardware Compatibility Report](../.github/ISSUE_TEMPLATE/hardware_compat.yml)
   issue with your results.
3. Or submit a PR directly updating this table.

Please include the kernel version, SoC revision (check `cat /proc/cpuinfo`),
and any configuration changes needed.
