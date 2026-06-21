#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# chaos/chaos-runner.sh — Orchestrate fault injection across a fleet of ARM64 nodes.
#
# Executes a named chaos scenario on a subset of nodes via SSH.
# Waits for the expected alert to fire, then verifies it resolves within the SLO window.
#
# Usage:
#   bash chaos/chaos-runner.sh --scenario scenarios/ce-storm.yaml \
#       --nodes "node1,node2,node3" \
#       --prometheus http://prometheus:9090 \
#       [--subset 2] [--dry-run] [--verbose]
#
# Exit codes:
#   0 — all scenario assertions passed
#   1 — scenario assertion failed (alert didn't fire or didn't resolve in time)
#   2 — prerequisite missing
#   3 — timeout

set -euo pipefail

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
SCENARIO=""
NODES_CSV=""
PROM_URL="http://localhost:9090"
SUBSET=0          # 0 = all nodes
DRY_RUN=false
VERBOSE=false
SSH_USER="${SSH_USER:-root}"
SSH_KEY="${SSH_KEY:-}"

log_info()  { echo "[INFO]  ${SCRIPT_NAME}: $*"; }
log_warn()  { echo "[WARN]  ${SCRIPT_NAME}: $*" >&2; }
log_error() { echo "[ERROR] ${SCRIPT_NAME}: $*" >&2; }
log_ok()    { echo "[PASS]  ${SCRIPT_NAME}: $*"; }
log_fail()  { echo "[FAIL]  ${SCRIPT_NAME}: $*" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --scenario)  SCENARIO="$2";  shift ;;
        --nodes)     NODES_CSV="$2"; shift ;;
        --prometheus) PROM_URL="$2"; shift ;;
        --subset)    SUBSET="$2";    shift ;;
        --dry-run)   DRY_RUN=true    ;;
        --verbose)   VERBOSE=true    ;;
        --help|-h)   sed -n 's/^# \?//p' "${BASH_SOURCE[0]}" | head -20; exit 0 ;;
        *) log_error "Unknown: $1"; exit 1 ;;
    esac
    shift
done

[[ -z "${SCENARIO}" ]] && { log_error "--scenario required"; exit 2; }
[[ -z "${NODES_CSV}" ]] && { log_error "--nodes required"; exit 2; }
[[ -f "${SCENARIO}" ]]  || { log_error "Scenario file not found: ${SCENARIO}"; exit 2; }

command -v python3 >/dev/null 2>&1 || { log_error "python3 required"; exit 2; }

# Parse scenario YAML
parse_scenario() {
    python3 - "${SCENARIO}" << 'PY'
import sys, yaml
s = yaml.safe_load(open(sys.argv[1]))
print(f"name={s['name']}")
print(f"inject_script={s['inject']['script']}")
print(f"inject_args={s['inject'].get('args', '')}")
print(f"expected_alert={s['expect']['alert']}")
print(f"alert_timeout={s['expect'].get('alert_timeout_s', 300)}")
print(f"resolve_timeout={s['expect'].get('resolve_timeout_s', 600)}")
print(f"cleanup_script={s.get('cleanup', {}).get('script', '')}")
PY
}

eval "$(parse_scenario)"
log_info "Scenario: ${name}"
log_info "  Inject:  ${inject_script} ${inject_args}"
log_info "  Expect:  alert ${expected_alert} within ${alert_timeout}s"
log_info "  Resolve: within ${resolve_timeout}s"

# Select nodes
IFS=',' read -ra ALL_NODES <<< "${NODES_CSV}"
if [[ ${SUBSET} -gt 0 && ${SUBSET} -lt ${#ALL_NODES[@]} ]]; then
    # Pick SUBSET random nodes
    TARGET_NODES=("${ALL_NODES[@]:0:${SUBSET}}")
else
    TARGET_NODES=("${ALL_NODES[@]}")
fi
log_info "Target nodes (${#TARGET_NODES[@]}): ${TARGET_NODES[*]}"

# Run injection on all target nodes in parallel
run_on_node() {
    local node="$1"
    local script="$2"
    local args="$3"

    if "${DRY_RUN}"; then
        log_info "DRY-RUN: would run on ${node}: bash ${script} ${args}"
        return 0
    fi

    local ssh_opts=(-o StrictHostKeyChecking=no -o ConnectTimeout=10)
    [[ -n "${SSH_KEY}" ]] && ssh_opts+=(-i "${SSH_KEY}")

    # shellcheck disable=SC2029
    ssh "${ssh_opts[@]}" "${SSH_USER}@${node}" "bash -s ${args}" < "${script}" || {
        log_warn "Injection failed on ${node}"
        return 1
    }
}

log_info "Injecting fault on ${#TARGET_NODES[@]} nodes ..."
for node in "${TARGET_NODES[@]}"; do
    run_on_node "${node}" "${inject_script}" "${inject_args}" &
done
wait
log_info "Injection complete"

if "${DRY_RUN}"; then
    log_info "DRY-RUN: skipping alert assertion"
    exit 0
fi

# Wait for alert to fire
wait_for_alert() {
    local alert_name="$1"
    local timeout_s="$2"
    local elapsed=0
    log_info "Waiting for alert ${alert_name} (timeout: ${timeout_s}s) ..."

    while [[ ${elapsed} -lt ${timeout_s} ]]; do
        local state
        state="$(curl -sf "${PROM_URL}/api/v1/alerts" 2>/dev/null \
            | python3 -c "
import json,sys
d=json.load(sys.stdin)
alerts=[a for a in d['data']['alerts'] if a['labels'].get('alertname')=='${alert_name}' and a['state']=='firing']
print('firing' if alerts else 'pending')
" 2>/dev/null || echo "error")"

        if [[ "${state}" == "firing" ]]; then
            log_ok "Alert ${alert_name} is firing after ${elapsed}s"
            return 0
        fi
        sleep 10
        elapsed=$((elapsed + 10))
    done

    log_fail "Alert ${alert_name} did not fire within ${timeout_s}s"
    return 1
}

wait_for_alert_resolve() {
    local alert_name="$1"
    local timeout_s="$2"

    # Run cleanup script (if any)
    if [[ -n "${cleanup_script}" && -f "${cleanup_script}" ]]; then
        log_info "Running cleanup: ${cleanup_script}"
        for node in "${TARGET_NODES[@]}"; do
            run_on_node "${node}" "${cleanup_script}" "" &
        done
        wait
    fi

    log_info "Waiting for alert ${alert_name} to resolve (timeout: ${timeout_s}s) ..."
    local elapsed=0
    while [[ ${elapsed} -lt ${timeout_s} ]]; do
        local state
        state="$(curl -sf "${PROM_URL}/api/v1/alerts" 2>/dev/null \
            | python3 -c "
import json,sys
d=json.load(sys.stdin)
alerts=[a for a in d['data']['alerts'] if a['labels'].get('alertname')=='${alert_name}' and a['state']=='firing']
print('firing' if alerts else 'resolved')
" 2>/dev/null || echo "error")"

        if [[ "${state}" == "resolved" ]]; then
            log_ok "Alert ${alert_name} resolved after ${elapsed}s"
            return 0
        fi
        sleep 15
        elapsed=$((elapsed + 15))
    done

    log_fail "Alert ${alert_name} did not resolve within ${timeout_s}s (SLO breach)"
    return 1
}

wait_for_alert "${expected_alert}" "${alert_timeout}" || exit 1
wait_for_alert_resolve "${expected_alert}" "${resolve_timeout}" || exit 1

log_ok "Scenario '${name}' passed"
