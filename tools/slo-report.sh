#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# tools/slo-report.sh — Query Prometheus for current 7-day collector availability.
#
# Usage:
#   PROMETHEUS_URL=http://localhost:9090 bash tools/slo-report.sh
#   bash tools/slo-report.sh --url http://prometheus:9090 [--json]

set -euo pipefail
PROM_URL="${PROMETHEUS_URL:-http://localhost:9090}"
JSON_OUT=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --url)  PROM_URL="$2"; shift ;;
        --json) JSON_OUT=true ;;
        *) echo "Unknown: $1" >&2; exit 1 ;;
    esac
    shift
done

query() {
    local expr="$1"
    curl -sf "${PROM_URL}/api/v1/query" \
        --data-urlencode "query=${expr}" \
        | python3 -c "
import json,sys
d=json.load(sys.stdin)
for r in d['data']['result']:
    print(r['metric'].get('collector','?'), r['metric'].get('node','?'), r['value'][1])
" 2>/dev/null || echo "(no data)"
}

echo "=== hw-fault-exporter SLO Report ==="
echo "Prometheus: ${PROM_URL}"
echo "Window: 7-day rolling availability"
echo ""
echo "Collector            Node                 Availability"
echo "----------------------------------------------------"
query 'avg_over_time(fault_resilience_collector_up[7d])'
