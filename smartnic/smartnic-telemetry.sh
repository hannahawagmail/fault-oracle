#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# smartnic-telemetry.sh — Scrape NIC statistics from sysfs and emit
# Prometheus node_exporter textfile format.
#
# Reads /sys/class/net/<iface>/statistics/* for each eligible interface
# and writes the results to stdout (or a .prom file for node_exporter).
#
# Eligible interfaces: those present in /sys/class/net/ excluding
# loopback, bonding masters, and virtual bridges (unless --include-virtual).
#
# Usage:
#   bash smartnic-telemetry.sh [--output /var/lib/node_exporter/smartnic.prom]
#                              [--interfaces eth0,eth1]
#                              [--include-virtual]
#                              [--dry-run]
#                              [--once]
#
# Integration with node_exporter textfile collector:
#   1. Set --output /var/lib/node_exporter/textfile_collector/smartnic.prom
#   2. Run via cron or systemd timer every 30 seconds
#   3. node_exporter will expose the metrics at /metrics

set -euo pipefail

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
OUTPUT=""
IFACE_FILTER=""
INCLUDE_VIRTUAL=false
DRY_RUN=false
ONCE=false
SYS_NET="/sys/class/net"

log_info()  { echo "[INFO]  ${SCRIPT_NAME}: $*" >&2; }
log_warn()  { echo "[WARN]  ${SCRIPT_NAME}: $*" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output)           OUTPUT="$2"; shift ;;
        --interfaces)       IFACE_FILTER="$2"; shift ;;
        --include-virtual)  INCLUDE_VIRTUAL=true ;;
        --dry-run)          DRY_RUN=true ;;
        --once)             ONCE=true ;;
        --sys-net)          SYS_NET="$2"; shift ;;  # override for testing
        --help|-h)
            sed -n 's/^# \?//p' "${BASH_SOURCE[0]}" | head -30
            exit 0 ;;
        *) echo "[ERROR] Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# Interface selection
# ---------------------------------------------------------------------------
get_interfaces() {
    local ifaces=()
    for iface_path in "${SYS_NET}"/*/; do
        local iface
        iface=$(basename "${iface_path}")
        [[ "${iface}" == "lo" ]] && continue
        # Skip virtual interfaces unless explicitly included
        if ! "${INCLUDE_VIRTUAL}"; then
            # Bonding masters, bridges, vlans, tun/tap: they have these subdirs
            [[ -d "${iface_path}/bridge" ]]  && continue
            [[ -d "${iface_path}/bonding" ]] && continue
            [[ -f "${iface_path}/tun_flags" ]] && continue
        fi
        [[ ! -d "${iface_path}/statistics" ]] && continue
        ifaces+=("${iface}")
    done
    echo "${ifaces[@]:-}"
}

# ---------------------------------------------------------------------------
# Statistics scraping
# ---------------------------------------------------------------------------
scrape_iface() {
    local iface="$1"
    local stats_dir="${SYS_NET}/${iface}/statistics"
    local speed_mbps="0"
    local operstate="unknown"
    local mac="unknown"

    # Read link metadata
    operstate=$(cat "${SYS_NET}/${iface}/operstate" 2>/dev/null || echo "unknown")
    mac=$(cat "${SYS_NET}/${iface}/address" 2>/dev/null | tr -d ':' || echo "unknown")
    speed_mbps=$(cat "${SYS_NET}/${iface}/speed" 2>/dev/null || echo "0")

    # Emit standard statistics
    local stat
    for stat_file in "${stats_dir}"/*; do
        [[ -f "${stat_file}" ]] || continue
        stat=$(basename "${stat_file}")
        local val
        val=$(cat "${stat_file}" 2>/dev/null || echo "0")
        # Normalise: only emit numeric values
        [[ "${val}" =~ ^[0-9]+$ ]] || continue
        echo "smartnic_${stat}{iface=\"${iface}\",operstate=\"${operstate}\"} ${val}"
    done

    # Emit link-up gauge (1=up, 0=other)
    local link_up=0
    [[ "${operstate}" == "up" ]] && link_up=1
    echo "smartnic_link_up{iface=\"${iface}\"} ${link_up}"

    # Emit speed
    echo "smartnic_link_speed_mbps{iface=\"${iface}\"} ${speed_mbps}"
}

# ---------------------------------------------------------------------------
# Main emission
# ---------------------------------------------------------------------------
emit_metrics() {
    local timestamp_ms
    timestamp_ms=$(date +%s%3N)

    echo "# HELP smartnic_rx_bytes_total Total bytes received."
    echo "# TYPE smartnic_rx_bytes_total counter"
    echo "# HELP smartnic_tx_bytes_total Total bytes transmitted."
    echo "# TYPE smartnic_tx_bytes_total counter"
    echo "# HELP smartnic_rx_errors_total Total receive errors."
    echo "# TYPE smartnic_rx_errors_total counter"
    echo "# HELP smartnic_tx_errors_total Total transmit errors."
    echo "# TYPE smartnic_tx_errors_total counter"
    echo "# HELP smartnic_rx_dropped_total Total receive drops."
    echo "# TYPE smartnic_rx_dropped_total counter"
    echo "# HELP smartnic_tx_dropped_total Total transmit drops."
    echo "# TYPE smartnic_tx_dropped_total counter"
    echo "# HELP smartnic_link_up 1 if the interface is operstate=up, 0 otherwise."
    echo "# TYPE smartnic_link_up gauge"
    echo "# HELP smartnic_link_speed_mbps Interface speed in Mbps (0 if unknown)."
    echo "# TYPE smartnic_link_speed_mbps gauge"

    local ifaces
    if [[ -n "${IFACE_FILTER}" ]]; then
        IFS=',' read -ra ifaces <<< "${IFACE_FILTER}"
    else
        read -ra ifaces <<< "$(get_interfaces)"
    fi

    for iface in "${ifaces[@]}"; do
        [[ -z "${iface}" ]] && continue
        if "${DRY_RUN}"; then
            log_info "DRY-RUN: would scrape ${iface}"
            echo "smartnic_link_up{iface=\"${iface}\",operstate=\"dry-run\"} 0"
            continue
        fi
        if [[ ! -d "${SYS_NET}/${iface}/statistics" ]]; then
            log_warn "Interface ${iface} not found or has no statistics"
            continue
        fi
        scrape_iface "${iface}"
    done
}

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
if [[ -n "${OUTPUT}" ]]; then
    tmpfile="${OUTPUT}.tmp.$$"
    emit_metrics > "${tmpfile}"
    mv "${tmpfile}" "${OUTPUT}"
    log_info "Written: ${OUTPUT}"
else
    emit_metrics
fi
