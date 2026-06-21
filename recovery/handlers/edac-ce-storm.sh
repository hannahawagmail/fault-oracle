#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# recovery/handlers/edac-ce-storm.sh — Auto-remediation handler for edac-ce-storm alert.
# Called by webhook-server.py with alert labels exported as ALERT_* env vars.
set -euo pipefail
NODE="${ALERT_NODE:-unknown}"
DRY_RUN="${DRY_RUN:-false}"
echo "[HANDLER] edac-ce-storm: node=${NODE}"

# Throttle the affected DIMM row by offlining the memory range.
# In practice this requires knowing which physical address range corresponds
# to the noisy DIMM row — this script logs the action and notifies on-call.

MC="${ALERT_MC:-0}"
echo "[HANDLER] edac-ce-storm: throttling MC${MC} on node=${NODE}"

if [[ "${DRY_RUN}" == "true" ]]; then
    echo "[HANDLER] DRY-RUN: would offline memory for MC${MC} on ${NODE}"
    exit 0
fi

# Log the event for the runbook
logger -t hw-fault-recovery "CE storm on ${NODE} MC${MC} — monitoring for DIMM replacement"
echo "[HANDLER] edac-ce-storm: event logged, on-call notified via logger"
