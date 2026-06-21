#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# recovery/handlers/exporter-down.sh — Auto-remediation handler for exporter-down alert.
# Called by webhook-server.py with alert labels exported as ALERT_* env vars.
set -euo pipefail
NODE="${ALERT_NODE:-unknown}"
DRY_RUN="${DRY_RUN:-false}"
echo "[HANDLER] exporter-down: node=${NODE}"

echo "[HANDLER] exporter-down: restarting exporter on node=${NODE}"
if [[ "${DRY_RUN}" == "true" ]]; then
    echo "[HANDLER] DRY-RUN: would restart hw-fault-exporter on ${NODE}"
    exit 0
fi
# In-cluster: delete the pod (operator handles it).
# On bare-metal: restart the systemd unit via SSH.
if command -v kubectl >/dev/null 2>&1; then
    kubectl delete pod -n monitoring -l "app.kubernetes.io/name=hw-fault-exporter,node=${NODE}" || true
else
    ssh "${NODE}" "systemctl restart hw-fault-exporter" || true
fi
