#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# connection-monitor.sh — Monitor network connectivity and trigger NM failover
#
# Usage:
#   bash connection-monitor.sh [--status] [--monitor] [--failover]
#                              [--ping-target 8.8.8.8] [--interval 30]
#                              [--failure-threshold 3] [--dry-run]
#
# Modes:
#   --status     Print current connection state and routing table, then exit.
#   --monitor    Loop: ping target, trigger failover after N consecutive failures.
#   --failover   Immediately perform one failover cycle (for testing).
#
# Metrics file: /var/lib/prometheus/node-exporter/network.prom
#   Written on every monitor iteration for node_exporter textfile collection.
#
# Syslog: all failover events are logged via logger(1) to facility daemon.
#
# Exit codes:
#   0 — Success / healthy
#   1 — Connectivity failure detected (in --status mode)
#   2 — Required tools (nmcli or ip) not available
#   3 — Argument error

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
PING_TARGET="8.8.8.8"
INTERVAL=30
FAILURE_THRESHOLD=3
DRY_RUN=false
MODE=""

METRICS_DIR="/var/lib/prometheus/node-exporter"
METRICS_FILE="${METRICS_DIR}/network.prom"
FAILOVER_COUNT_FILE="/tmp/nm-failover-count"
PING_FAILURE_STREAK=0
FAILOVER_TOTAL=0

# Ordered list of connection IDs by priority (highest first)
# NM profile names must match the id= field in the keyfiles.
NM_CONNECTIONS_ORDERED=("primary-ethernet" "wifi-backup" "lte-fallback")

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2
}

have_cmd() {
    command -v "$1" &>/dev/null
}

require_tools() {
    if ! have_cmd nmcli && ! have_cmd ip; then
        log "ERROR: neither 'nmcli' nor 'ip' command found. Cannot manage connections."
        exit 2
    fi
}

# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------
parse_args() {
    if [[ $# -eq 0 ]]; then
        echo "Usage: $0 [--status] [--monitor] [--failover]" \
             "[--ping-target IP] [--interval SEC] [--failure-threshold N] [--dry-run]" >&2
        exit 3
    fi

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --status)    MODE="status"   ; shift ;;
            --monitor)   MODE="monitor"  ; shift ;;
            --failover)  MODE="failover" ; shift ;;
            --ping-target)
                PING_TARGET="$2"; shift 2 ;;
            --interval)
                INTERVAL="$2"; shift 2 ;;
            --failure-threshold)
                FAILURE_THRESHOLD="$2"; shift 2 ;;
            --dry-run)
                DRY_RUN=true; shift ;;
            -h|--help)
                grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -20
                exit 0 ;;
            *)
                log "Unknown argument: $1"
                exit 3 ;;
        esac
    done

    if [[ -z "$MODE" ]]; then
        log "ERROR: one of --status, --monitor, or --failover is required."
        exit 3
    fi
}

# ---------------------------------------------------------------------------
# get_active_connection_id
# Returns the id of the currently active connection with a default route, or
# the highest-priority active connection if no default is found.
# ---------------------------------------------------------------------------
get_active_connection_id() {
    if have_cmd nmcli; then
        # Find activated connections (STATE=activated)
        nmcli -t -f NAME,STATE connection show 2>/dev/null \
            | awk -F: '$2=="activated"{print $1}' \
            | head -1
    fi
}

# ---------------------------------------------------------------------------
# get_active_connection_priority
# ---------------------------------------------------------------------------
get_active_connection_priority() {
    local conn_id="$1"
    if [[ -z "$conn_id" ]]; then
        echo 0; return
    fi
    if have_cmd nmcli; then
        nmcli -t -f connection.autoconnect-priority connection show id "$conn_id" \
            2>/dev/null | awk -F: '{print $2}' | tr -d ' ' || echo 0
    else
        echo 0
    fi
}

# ---------------------------------------------------------------------------
# get_next_connection
# Given the current active connection id, returns the next lower-priority one.
# ---------------------------------------------------------------------------
get_next_connection() {
    local current="$1"
    local found=false
    for conn in "${NM_CONNECTIONS_ORDERED[@]}"; do
        if $found; then
            echo "$conn"
            return
        fi
        [[ "$conn" == "$current" ]] && found=true
    done
    # If current not found or is last, return empty (no fallback)
    echo ""
}

# ---------------------------------------------------------------------------
# decode_proc_net_route_default_gw
# Fallback when 'ip route' is not available: parse /proc/net/route.
# Fields: Iface Destination Gateway ... (hex, little-endian)
# ---------------------------------------------------------------------------
decode_proc_net_route_default_gw() {
    # Default route has Destination=00000000
    local line
    line=$(awk '$2=="00000000"{print}' /proc/net/route 2>/dev/null | head -1)
    if [[ -z "$line" ]]; then
        echo "No default route in /proc/net/route"
        return
    fi
    local iface hex_gw
    iface=$(echo "$line" | awk '{print $1}')
    hex_gw=$(echo "$line" | awk '{print $3}')
    # Convert little-endian hex to dotted-decimal
    local b1 b2 b3 b4
    b1=$(( 16#${hex_gw:6:2} ))
    b2=$(( 16#${hex_gw:4:2} ))
    b3=$(( 16#${hex_gw:2:2} ))
    b4=$(( 16#${hex_gw:0:2} ))
    echo "default via ${b1}.${b2}.${b3}.${b4} dev ${iface} (from /proc/net/route)"
}

# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------
cmd_status() {
    echo "=== NetworkManager Connection Status ==="
    if have_cmd nmcli; then
        nmcli -f NAME,STATE,AUTOCONNECT,AUTOCONNECT-PRIORITY,DEVICE connection show 2>/dev/null \
            || echo "(nmcli returned error)"
    else
        echo "(nmcli not available)"
    fi

    echo ""
    echo "=== Default Route ==="
    if have_cmd ip; then
        ip route show default 2>/dev/null || echo "(no default route)"
    else
        echo "(ip command not available — reading /proc/net/route)"
        decode_proc_net_route_default_gw
    fi

    echo ""
    echo "=== Ping Test (${PING_TARGET}) ==="
    if ping -c 1 -W 2 "$PING_TARGET" &>/dev/null; then
        echo "REACHABLE"
        return 0
    else
        echo "UNREACHABLE"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# write_metrics
# ---------------------------------------------------------------------------
write_metrics() {
    local streak="$1"
    local priority="$2"
    local failovers="$3"

    # Create directory if writable
    if [[ -d "$METRICS_DIR" ]] || mkdir -p "$METRICS_DIR" 2>/dev/null; then
        {
            echo "# HELP network_ping_failure_streak Consecutive ping failures to ${PING_TARGET}"
            echo "# TYPE network_ping_failure_streak gauge"
            echo "network_ping_failure_streak ${streak}"
            echo "# HELP network_active_connection_priority autoconnect-priority of active NM connection"
            echo "# TYPE network_active_connection_priority gauge"
            echo "network_active_connection_priority ${priority}"
            echo "# HELP network_connection_failovers_total Total NM failover events since process start"
            echo "# TYPE network_connection_failovers_total counter"
            echo "network_connection_failovers_total ${failovers}"
        } > "${METRICS_FILE}.tmp" && mv "${METRICS_FILE}.tmp" "$METRICS_FILE"
    fi
}

# ---------------------------------------------------------------------------
# cmd_failover
# ---------------------------------------------------------------------------
cmd_failover() {
    local current_conn
    current_conn=$(get_active_connection_id)

    local next_conn
    next_conn=$(get_next_connection "$current_conn")

    log "FAILOVER: current='${current_conn:-none}' next='${next_conn:-none}'"

    if $DRY_RUN; then
        log "DRY-RUN: would run: nmcli connection down id '${current_conn}'"
        log "DRY-RUN: would run: nmcli connection up id '${next_conn}'"
        return 0
    fi

    if ! have_cmd nmcli; then
        log "ERROR: nmcli not available; cannot perform failover."
        return 1
    fi

    if [[ -n "$current_conn" ]]; then
        log "Bringing down: ${current_conn}"
        nmcli connection down id "$current_conn" 2>/dev/null || true
    fi

    if [[ -n "$next_conn" ]]; then
        log "Bringing up: ${next_conn}"
        if nmcli connection up id "$next_conn" 2>/dev/null; then
            log "Failover to '${next_conn}' succeeded."
            logger -t connection-monitor -p daemon.warning \
                "Network failover: '${current_conn}' -> '${next_conn}'"
        else
            log "ERROR: Failed to activate '${next_conn}'."
            logger -t connection-monitor -p daemon.err \
                "Network failover FAILED: could not activate '${next_conn}'"
            return 1
        fi
    else
        log "WARNING: No further fallback connection available after '${current_conn}'."
        logger -t connection-monitor -p daemon.crit \
            "Network failover exhausted: no connection available after '${current_conn}'"
        return 1
    fi

    # Increment persistent failover counter
    local count=0
    [[ -f "$FAILOVER_COUNT_FILE" ]] && count=$(cat "$FAILOVER_COUNT_FILE" 2>/dev/null || echo 0)
    count=$(( count + 1 ))
    echo "$count" > "$FAILOVER_COUNT_FILE"
    FAILOVER_TOTAL=$count
}

# ---------------------------------------------------------------------------
# cmd_monitor
# ---------------------------------------------------------------------------
cmd_monitor() {
    log "Starting monitor: target=${PING_TARGET}, interval=${INTERVAL}s, threshold=${FAILURE_THRESHOLD}"

    # Load previous failover count if it exists
    [[ -f "$FAILOVER_COUNT_FILE" ]] && FAILOVER_TOTAL=$(cat "$FAILOVER_COUNT_FILE" 2>/dev/null || echo 0)

    while true; do
        local active_conn priority

        if ping -c 1 -W 2 "$PING_TARGET" &>/dev/null; then
            if [[ $PING_FAILURE_STREAK -gt 0 ]]; then
                log "Connectivity restored after ${PING_FAILURE_STREAK} failures."
            fi
            PING_FAILURE_STREAK=0
        else
            PING_FAILURE_STREAK=$(( PING_FAILURE_STREAK + 1 ))
            log "Ping failure streak: ${PING_FAILURE_STREAK}/${FAILURE_THRESHOLD}"

            if [[ $PING_FAILURE_STREAK -ge $FAILURE_THRESHOLD ]]; then
                log "Failure threshold reached — triggering failover."
                cmd_failover || true
                PING_FAILURE_STREAK=0
            fi
        fi

        active_conn=$(get_active_connection_id)
        priority=$(get_active_connection_priority "$active_conn")
        [[ -f "$FAILOVER_COUNT_FILE" ]] && FAILOVER_TOTAL=$(cat "$FAILOVER_COUNT_FILE" 2>/dev/null || echo 0)

        write_metrics "$PING_FAILURE_STREAK" "${priority:-0}" "$FAILOVER_TOTAL"

        sleep "$INTERVAL"
    done
}

# ---------------------------------------------------------------------------
# Cleanup on exit
# ---------------------------------------------------------------------------
cleanup() {
    local exit_code=$?
    log "Shutting down. Writing final metrics."
    local active_conn priority
    active_conn=$(get_active_connection_id 2>/dev/null || echo "")
    priority=$(get_active_connection_priority "$active_conn" 2>/dev/null || echo 0)
    [[ -f "$FAILOVER_COUNT_FILE" ]] && FAILOVER_TOTAL=$(cat "$FAILOVER_COUNT_FILE" 2>/dev/null || echo 0)
    write_metrics "$PING_FAILURE_STREAK" "${priority:-0}" "$FAILOVER_TOTAL" || true
    exit $exit_code
}

trap cleanup EXIT SIGTERM SIGINT

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
parse_args "$@"
require_tools

case "$MODE" in
    status)   cmd_status ;;
    monitor)  cmd_monitor ;;
    failover) cmd_failover ;;
esac
