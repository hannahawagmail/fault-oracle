#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# recovery/handlers/pcie-fatal.sh — Auto-remediation handler for pcie-fatal alert.
# Called by webhook-server.py with alert labels exported as ALERT_* env vars.
set -euo pipefail
NODE="${ALERT_NODE:-unknown}"
DRY_RUN="${DRY_RUN:-false}"
echo "[HANDLER] pcie-fatal: node=${NODE}"

BDF="${ALERT_DEV:-unknown}"
echo "[HANDLER] pcie-fatal: rescanning PCIe bus on node=${NODE} (device=${BDF})"
if [[ "${DRY_RUN}" == "true" ]]; then
    echo "[HANDLER] DRY-RUN: would rescan PCI bus on ${NODE}"
    exit 0
fi
ssh "${NODE}" "echo 1 > /sys/bus/pci/rescan" 2>/dev/null || true
logger -t hw-fault-recovery "PCIe fatal error on ${NODE} dev ${BDF} — bus rescanned"
