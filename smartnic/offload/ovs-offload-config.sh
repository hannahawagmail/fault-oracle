#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# ovs-offload-config.sh — Configure Open vSwitch TC-flower hardware offload on DPU
#
# Enables TC flower offload so OvS match-action rules execute in DPU ASIC
# (not host CPU softirq), reducing forwarding latency from ~50µs to <1µs.
#
# Requirements: openvswitch >= 2.13, kernel >= 5.10, iproute2 >= 5.10
# DPU interface: representor netdev (e.g. pf0hpf, p0, enp3s0f0)
#
# Usage:
#   bash ovs-offload-config.sh [--status] [--enable-offload <iface>]
#                              [--add-vxlan-flow <vnid> <remote-ip>]
#                              [--show-flows] [--benchmark] [--dry-run]
#
# Examples:
#   bash ovs-offload-config.sh --status
#   bash ovs-offload-config.sh --dry-run --enable-offload pf0hpf
#   bash ovs-offload-config.sh --add-vxlan-flow 100 192.168.10.1
#   bash ovs-offload-config.sh --show-flows
#   bash ovs-offload-config.sh --benchmark

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
OVS_BRIDGE="br0"
VXLAN_IFACE="vxlan0"
VXLAN_DST_PORT=4789

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
DRY_RUN=false
PRIMARY_IFACE=""

# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------
log_info()  { echo "[INFO]  $*"; }
log_warn()  { echo "[WARN]  $*" >&2; }
log_error() { echo "[ERROR] $*" >&2; }

run_cmd() {
    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "[DRY-RUN] $*"
    else
        "$@"
    fi
}

graceful_skip() {
    log_warn "$*"
    exit 2
}

# Check that ovs-vsctl is present; print install instructions if missing.
require_ovs_vsctl() {
    if ! command -v ovs-vsctl &>/dev/null; then
        log_warn "ovs-vsctl not found. Open vSwitch is not installed."
        echo ""
        echo "To install Open vSwitch on DPU Debian/Ubuntu:"
        echo "  apt-get update && apt-get install -y openvswitch-switch"
        echo ""
        echo "To install on DPU RHEL/CentOS/Rocky:"
        echo "  dnf install -y openvswitch"
        echo "  systemctl enable --now openvswitch"
        echo ""
        echo "Minimum versions required:"
        echo "  openvswitch >= 2.13 (TC flower offload support)"
        echo "  kernel       >= 5.10 (stable TC flower + repr netdev)"
        echo "  iproute2     >= 5.10 (tc filter with flower + action mirror)"
        graceful_skip "Install Open vSwitch, then re-run this script."
    fi
}

# ---------------------------------------------------------------------------
# --status
# ---------------------------------------------------------------------------
cmd_status() {
    log_info "=== OvS Hardware Offload Status ==="

    require_ovs_vsctl

    # Global OvS hw-offload config
    local hw_offload
    hw_offload=$(ovs-vsctl get Open_vSwitch . other_config 2>/dev/null || echo "{}")
    log_info "OvS other_config: ${hw_offload}"

    local offload_enabled="false"
    if echo "${hw_offload}" | grep -q "hw-offload=\"true\""; then
        offload_enabled="true"
    fi
    log_info "hw-offload enabled: ${offload_enabled}"

    # List bridges and their ports
    echo ""
    log_info "=== OvS Bridges and Ports ==="
    ovs-vsctl show 2>/dev/null || log_warn "ovs-vsctl show failed."

    # TC flower rules on primary interface (if set)
    if [[ -n "${PRIMARY_IFACE}" ]]; then
        echo ""
        log_info "=== TC flower rules on ${PRIMARY_IFACE} ==="
        tc filter show dev "${PRIMARY_IFACE}" ingress 2>/dev/null \
            || log_warn "No TC ingress filter on ${PRIMARY_IFACE}."
    fi

    # Count offloaded vs software flows
    echo ""
    log_info "=== OvS Upcall Statistics ==="
    if command -v ovs-appctl &>/dev/null; then
        ovs-appctl upcall/show 2>/dev/null || log_warn "ovs-appctl upcall/show failed."
    else
        log_warn "ovs-appctl not found; cannot show upcall statistics."
    fi

    log_info "Status check complete."
}

# ---------------------------------------------------------------------------
# --enable-offload <iface>
# ---------------------------------------------------------------------------
cmd_enable_offload() {
    local iface="${1:-}"
    if [[ -z "${iface}" ]]; then
        log_error "--enable-offload requires <iface> argument."
        exit 1
    fi

    require_ovs_vsctl

    log_info "=== Enabling OvS TC-flower Hardware Offload on ${iface} ==="

    # 1. Enable hw-offload globally in OvS
    log_info "Setting hw-offload=true in OvS other_config ..."
    run_cmd ovs-vsctl set Open_vSwitch . other_config:hw-offload=true

    # 2. Restart OvS to apply the offload setting
    log_info "Restarting openvswitch-switch ..."
    run_cmd systemctl restart openvswitch-switch

    # 3. Ensure bridge exists
    if ! ovs-vsctl br-exists "${OVS_BRIDGE}" 2>/dev/null; then
        log_info "Creating OvS bridge ${OVS_BRIDGE} ..."
        run_cmd ovs-vsctl add-br "${OVS_BRIDGE}"
    else
        log_info "Bridge ${OVS_BRIDGE} already exists."
    fi

    # 4. Add interface to bridge (idempotent)
    if ! ovs-vsctl list-ports "${OVS_BRIDGE}" 2>/dev/null | grep -qx "${iface}"; then
        log_info "Adding ${iface} to bridge ${OVS_BRIDGE} ..."
        run_cmd ovs-vsctl add-port "${OVS_BRIDGE}" "${iface}"
    else
        log_info "Interface ${iface} already in bridge ${OVS_BRIDGE}."
    fi

    # 5. Add TC ingress qdisc (needed for TC flower rules)
    log_info "Adding TC ingress qdisc on ${iface} ..."
    # Remove existing ingress qdisc if present (idempotent)
    if [[ "${DRY_RUN}" == "false" ]]; then
        tc qdisc del dev "${iface}" ingress 2>/dev/null || true
    fi
    run_cmd tc qdisc add dev "${iface}" ingress

    # 6. Verify hardware offload capability
    log_info "Verifying hw-tc-offload capability on ${iface} ..."
    echo ""
    if command -v ethtool &>/dev/null; then
        local offload_line
        offload_line=$(ethtool -k "${iface}" 2>/dev/null | grep "hw-tc-offload" || echo "hw-tc-offload: unknown")
        log_info "ethtool -k ${iface} | grep hw-tc-offload:"
        log_info "  ${offload_line}"
        if echo "${offload_line}" | grep -q "on"; then
            log_info "SUCCESS: hw-tc-offload is ON — flows will be offloaded to ASIC."
        else
            log_warn "hw-tc-offload is OFF. Driver may not support TC flower offload."
            log_warn "For BlueField-3: ensure mlx5_core driver is loaded with tc_police support."
            log_warn "For Pensando:    ensure ionic driver version >= 5.15 is loaded."
        fi
    else
        log_warn "ethtool not found; cannot verify hw-tc-offload capability."
    fi

    log_info "Offload configuration complete for ${iface}."
}

# ---------------------------------------------------------------------------
# --add-vxlan-flow <vnid> <remote-ip>
# ---------------------------------------------------------------------------
cmd_add_vxlan_flow() {
    local vnid="${1:-}"
    local remote_ip="${2:-}"

    if [[ -z "${vnid}" ]] || [[ -z "${remote_ip}" ]]; then
        log_error "--add-vxlan-flow requires <vnid> and <remote-ip> arguments."
        exit 1
    fi

    require_ovs_vsctl

    log_info "=== Adding VXLAN Flow: VNI=${vnid} remote=${remote_ip} ==="

    # Determine the primary interface (first non-loopback, or pf0hpf if present)
    local iface="${PRIMARY_IFACE}"
    if [[ -z "${iface}" ]]; then
        if ip link show pf0hpf &>/dev/null 2>&1; then
            iface="pf0hpf"
        else
            iface=$(ip -o link show 2>/dev/null | awk -F': ' '$2 !~ /lo|vxlan/ {print $2; exit}')
        fi
    fi
    log_info "Using interface: ${iface}"

    # 1. Add OvS VXLAN port if not already present
    if ! ovs-vsctl list-ports "${OVS_BRIDGE}" 2>/dev/null | grep -qx "${VXLAN_IFACE}"; then
        log_info "Adding VXLAN tunnel port ${VXLAN_IFACE} to bridge ${OVS_BRIDGE} ..."
        run_cmd ovs-vsctl add-port "${OVS_BRIDGE}" "${VXLAN_IFACE}" \
            -- set interface "${VXLAN_IFACE}" \
               type=vxlan \
               options:remote_ip="${remote_ip}" \
               options:key="${vnid}" \
               options:dst_port="${VXLAN_DST_PORT}"
    else
        log_info "VXLAN port ${VXLAN_IFACE} already present in ${OVS_BRIDGE}."
    fi

    # 2. Add OvS flow rule for VXLAN decap (tunnel → internal port)
    log_info "Adding OvS flow: tun_id=${vnid} → output to internal port ..."
    run_cmd ovs-ofctl add-flow "${OVS_BRIDGE}" \
        "priority=100,tun_id=${vnid},in_port=${VXLAN_IFACE},actions=strip_vlan,output:1"

    # 3. Show the equivalent tc filter command for reference
    #
    # The TC flower rule below matches on VXLAN VNI (enc_key_id) and the
    # encapsulating destination IP (enc_dst_ip), strips the tunnel header
    # (tunnel_key unset = VXLAN decap), then redirects the inner Ethernet
    # frame to the uplink interface (mirred egress redirect dev vxlan0).
    # This rule executes entirely in the DPU ASIC — zero host CPU cycles.
    echo ""
    log_info "Equivalent TC flower rule (executed in DPU ASIC):"
    echo "  tc filter add dev ${iface} ingress \\"
    echo "      protocol ip flower \\"
    echo "      enc_key_id ${vnid} \\"
    echo "      enc_dst_ip ${remote_ip} \\"
    echo "      action tunnel_key unset \\"
    echo "      action mirred egress redirect dev ${VXLAN_IFACE}"

    echo ""
    log_info "VXLAN flow added. VNI=${vnid} remote=${remote_ip}."
}

# ---------------------------------------------------------------------------
# --show-flows
# ---------------------------------------------------------------------------
cmd_show_flows() {
    require_ovs_vsctl

    log_info "=== OvS Flow Table: ${OVS_BRIDGE} ==="

    local flow_dump
    flow_dump=$(ovs-ofctl dump-flows "${OVS_BRIDGE}" 2>/dev/null || echo "")

    if [[ -z "${flow_dump}" ]]; then
        log_warn "No flows found or bridge '${OVS_BRIDGE}' does not exist."
        return 0
    fi

    echo "${flow_dump}"
    echo ""

    # Parse and summarize
    local total_flows
    total_flows=$(echo "${flow_dump}" | grep -c "^" || echo 0)
    log_info "Total flows: ${total_flows}"

    # Match type breakdown
    local l2_count l3_count l4_count vxlan_count
    l2_count=$(echo "${flow_dump}"  | grep -c "dl_dst\|dl_src\|dl_type"      || true)
    l3_count=$(echo "${flow_dump}"  | grep -c "nw_src\|nw_dst\|ip"           || true)
    l4_count=$(echo "${flow_dump}"  | grep -c "tp_src\|tp_dst\|tcp\|udp"     || true)
    vxlan_count=$(echo "${flow_dump}" | grep -c "tun_id\|vxlan\|tunnel"      || true)

    log_info "Match types:"
    log_info "  L2 (MAC/EtherType): ${l2_count}"
    log_info "  L3 (IP src/dst):    ${l3_count}"
    log_info "  L4 (TCP/UDP port):  ${l4_count}"
    log_info "  VXLAN (tun_id):     ${vxlan_count}"

    # Action type breakdown
    local output_count encap_count decap_count drop_count
    output_count=$(echo "${flow_dump}" | grep -c "actions=output\|actions=.*output" || true)
    encap_count=$(echo "${flow_dump}"  | grep -c "set_tunnel\|push_vxlan"           || true)
    decap_count=$(echo "${flow_dump}"  | grep -c "strip_vlan\|pop_vxlan\|tun_id"    || true)
    drop_count=$(echo "${flow_dump}"   | grep -c "actions=drop"                     || true)

    log_info "Action types:"
    log_info "  Output/forward: ${output_count}"
    log_info "  Encap (VXLAN):  ${encap_count}"
    log_info "  Decap (VXLAN):  ${decap_count}"
    log_info "  Drop:           ${drop_count}"
}

# ---------------------------------------------------------------------------
# --benchmark
# ---------------------------------------------------------------------------
cmd_benchmark() {
    log_info "=== OvS Hardware Offload Throughput Benchmark ==="
    log_info "Note: This is a basic sanity check using iperf3."
    log_info "      True line-rate benchmarks require a dedicated traffic generator"
    log_info "      (e.g., Spirent, IXIA, or MoonGen with DPDK) connected to DPU ports."
    echo ""

    if ! command -v iperf3 &>/dev/null; then
        log_warn "iperf3 not found. Install with: apt-get install -y iperf3"
        graceful_skip "iperf3 required for benchmark."
    fi

    # Start iperf3 server in background on localhost
    log_info "Starting iperf3 server (background, port 5201) ..."
    iperf3 --server --port 5201 --one-off --daemon --logfile /tmp/iperf3-server.log 2>/dev/null || true
    sleep 1

    # Run iperf3 client against localhost for 10 seconds
    log_info "Running iperf3 client for 10 seconds (loopback — tests CPU, not ASIC offload) ..."
    local result
    result=$(iperf3 --client 127.0.0.1 --port 5201 --time 10 --json 2>/dev/null || echo "{}")

    # Parse throughput
    local bps
    bps=$(echo "${result}" | python3 -c \
        "import sys,json; d=json.load(sys.stdin); print(d['end']['sum_received']['bits_per_second'])" \
        2>/dev/null || echo "0")
    local gbps
    gbps=$(echo "${bps}" | awk '{printf "%.2f", $1/1e9}')

    log_info "Loopback throughput: ${gbps} Gbps"
    echo ""
    log_warn "IMPORTANT: Loopback iperf3 measures CPU/memory bandwidth, NOT DPU ASIC offload."
    log_warn "To measure offload benefit:"
    log_warn "  1. Connect traffic generator to DPU physical port"
    log_warn "  2. Run flows WITHOUT offload: ovs-vsctl set Open_vSwitch . other_config:hw-offload=false"
    log_warn "  3. Record throughput and CPU usage"
    log_warn "  4. Enable offload:             ovs-vsctl set Open_vSwitch . other_config:hw-offload=true"
    log_warn "  5. Record throughput and CPU usage again"
    log_warn "  Expected: CPU drops from ~100% to <5% per core; latency from ~50µs to <1µs"

    # Clean up server log
    rm -f /tmp/iperf3-server.log
}

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
    grep "^# Usage:" "$0" -A3 | sed 's/^# //'
    echo ""
    echo "Options:"
    echo "  --status                       Show OvS offload config and TC rules"
    echo "  --enable-offload <iface>       Enable hw-offload and configure TC ingress"
    echo "  --add-vxlan-flow <vnid> <ip>   Add VXLAN encap/decap flow rule"
    echo "  --show-flows                   Dump and summarize OvS flow table"
    echo "  --benchmark                    Run iperf3 throughput sanity check"
    echo "  --dry-run                      Print commands without executing"
    echo "  -h, --help                     Show this help"
}

# ---------------------------------------------------------------------------
# Main argument parsing
# ---------------------------------------------------------------------------
if [[ $# -eq 0 ]]; then
    usage
    exit 0
fi

ACTION=""
ENABLE_IFACE=""
VXLAN_VNID=""
VXLAN_REMOTE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --status)
            ACTION="status"
            shift
            ;;
        --enable-offload)
            ACTION="enable-offload"
            if [[ -n "${2:-}" ]] && [[ "${2}" != --* ]]; then
                ENABLE_IFACE="$2"
                PRIMARY_IFACE="$2"
                shift 2
            else
                log_error "--enable-offload requires <iface> argument."
                exit 1
            fi
            ;;
        --add-vxlan-flow)
            ACTION="add-vxlan-flow"
            if [[ -n "${2:-}" ]] && [[ -n "${3:-}" ]]; then
                VXLAN_VNID="$2"
                VXLAN_REMOTE="$3"
                shift 3
            else
                log_error "--add-vxlan-flow requires <vnid> and <remote-ip> arguments."
                exit 1
            fi
            ;;
        --show-flows)
            ACTION="show-flows"
            shift
            ;;
        --benchmark)
            ACTION="benchmark"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            log_error "Unknown argument: $1"
            usage
            exit 1
            ;;
    esac
done

case "${ACTION}" in
    status)         cmd_status ;;
    enable-offload) cmd_enable_offload "${ENABLE_IFACE}" ;;
    add-vxlan-flow) cmd_add_vxlan_flow "${VXLAN_VNID}" "${VXLAN_REMOTE}" ;;
    show-flows)     cmd_show_flows ;;
    benchmark)      cmd_benchmark ;;
    *)
        log_error "No action specified."
        usage
        exit 1
        ;;
esac
