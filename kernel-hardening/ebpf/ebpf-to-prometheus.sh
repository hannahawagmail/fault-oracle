#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# ebpf-to-prometheus.sh — Run eBPF detection scripts and export metrics to Prometheus
#
# Architecture:
#   bpftrace script → stdout → this parser → /var/lib/prometheus/node-exporter/ebpf.prom
#   Prometheus node_exporter textfile collector reads *.prom files from that dir.
#
# Usage:
#   bash ebpf-to-prometheus.sh [--script detect-fork-storm.bt] [--outdir /var/lib/...]
#                              [--duration 60] [--loop] [--dry-run] [--all]
#
# Options:
#   --script <file>   Path to a .bt bpftrace script (repeatable)
#   --outdir <dir>    Directory for .prom output files (default: /var/lib/prometheus/node-exporter)
#   --duration <N>    Run each script for N seconds then collect output (default: 30)
#   --loop            Run continuously, updating .prom every --duration seconds
#   --all             Run all .bt scripts in the same directory as this script
#   --dry-run         Validate environment but do not run bpftrace or write files
#
# Exit codes: 0=success, 1=error, 2=bpftrace not available

set -e

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
OUTDIR="/var/lib/prometheus/node-exporter"
DURATION=30
DO_LOOP=false
DO_DRY_RUN=false
DO_ALL=false
SCRIPTS=()
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
BLU='\033[0;34m'
RST='\033[0m'

log()  { echo -e "${BLU}[ebpf-to-prom]${RST} $*"; }
ok()   { echo -e "${GRN}[OK]${RST} $*"; }
warn() { echo -e "${YLW}[WARN]${RST} $*"; }
err()  { echo -e "${RED}[ERR]${RST} $*" >&2; }

usage() {
    sed -n '3,17p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
[[ $# -eq 0 ]] && usage

while [[ $# -gt 0 ]]; do
    case "$1" in
        --script)
            shift; [[ -z "$1" ]] && { err "--script requires a file argument"; exit 1; }
            SCRIPTS+=("$1")
            ;;
        --outdir)
            shift; [[ -z "$1" ]] && { err "--outdir requires a directory argument"; exit 1; }
            OUTDIR="$1"
            ;;
        --duration)
            shift
            if [[ -z "$1" || ! "$1" =~ ^[0-9]+$ ]]; then
                err "--duration requires a positive integer"
                exit 1
            fi
            DURATION="$1"
            ;;
        --loop)     DO_LOOP=true ;;
        --dry-run)  DO_DRY_RUN=true ;;
        --all)      DO_ALL=true ;;
        --help|-h)  usage ;;
        *)
            err "Unknown option: $1"
            exit 1
            ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# Collect all scripts if --all was specified
# ---------------------------------------------------------------------------
if $DO_ALL; then
    while IFS= read -r -d '' bt_file; do
        SCRIPTS+=("$bt_file")
    done < <(find "$SCRIPT_DIR" -maxdepth 1 -name '*.bt' -print0 2>/dev/null)
    log "Found ${#SCRIPTS[@]} .bt scripts in ${SCRIPT_DIR}"
fi

if [[ ${#SCRIPTS[@]} -eq 0 ]]; then
    err "No scripts specified. Use --script <file> or --all"
    exit 1
fi

# ---------------------------------------------------------------------------
# Check for bpftrace
# ---------------------------------------------------------------------------
check_bpftrace() {
    if ! command -v bpftrace &>/dev/null; then
        err "bpftrace not found in PATH"
        err ""
        err "Install instructions:"
        err "  Debian/Ubuntu:  apt install bpftrace"
        err "  Fedora/RHEL:    dnf install bpftrace"
        err "  Arch Linux:     pacman -S bpftrace"
        err "  From source:    https://github.com/bpftrace/bpftrace"
        err ""
        err "Minimum version: bpftrace 0.14"
        err "Check version:   bpftrace --version"
        exit 2
    fi

    local version
    version=$(bpftrace --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
    log "bpftrace version: ${version}"

    # Check BPF JIT is enabled (important on ARM)
    local jit_file="/proc/sys/net/core/bpf_jit_enable"
    if [[ -r "$jit_file" ]]; then
        local jit
        jit=$(cat "$jit_file")
        if [[ "$jit" -eq 0 ]]; then
            warn "BPF JIT is disabled. Enable for better performance:"
            warn "  echo 1 > ${jit_file}"
        else
            ok "BPF JIT is enabled (${jit_file}=${jit})"
        fi
    fi
}

# ---------------------------------------------------------------------------
# Check capabilities
# ---------------------------------------------------------------------------
check_capabilities() {
    if [[ $EUID -ne 0 ]]; then
        # Check for CAP_BPF and CAP_PERFMON
        if command -v capsh &>/dev/null; then
            local caps
            caps=$(capsh --print 2>/dev/null | grep "cap_bpf" || true)
            if [[ -z "$caps" ]]; then
                err "Not root and CAP_BPF not detected. bpftrace requires root or CAP_BPF+CAP_PERFMON"
                exit 1
            fi
        else
            err "Not running as root. bpftrace requires root or CAP_BPF + CAP_PERFMON"
            err "Run with: sudo $0 $*"
            exit 1
        fi
    fi
}

# ---------------------------------------------------------------------------
# Parse bpftrace output into Prometheus text format
# ---------------------------------------------------------------------------
# Input: raw bpftrace stdout
# Output: Prometheus text format lines
#
# Handles:
#   "ALERT: fork storm detected: comm=<name> rate=<N>/s"
#   "ALERT: OOM invoked by comm=<name> pid=<pid>"
#   "ALERT: OOM victim: comm=<name> pid=<pid>"
#   "ALERT: write storm: comm=<name> dev=<dev> writes/s=<N>"
#   "ALERT: I/O latency spike: dev=<dev> lat_ms=<N> comm=<name>"
#   "STAT oom_victim_total count=<N>"
#   "STAT io_write_rps dev=<dev> comm=<name> rps=<N>"
# ---------------------------------------------------------------------------
parse_bpftrace_output() {
    local script_name="$1"
    local input="$2"
    local script_base
    script_base=$(basename "$script_name" .bt)

    local output=""
    local ts
    ts=$(date -u '+%s')

    output+="# Generated by ebpf-to-prometheus.sh from ${script_name}\n"
    output+="# Timestamp: $(date -u '+%Y-%m-%dT%H:%M:%SZ')\n"
    output+="\n"

    # --- Fork storm alerts ---
    output+="# HELP ebpf_fork_storm_alert Fork storm alert: 1 if threshold exceeded in last interval\n"
    output+="# TYPE ebpf_fork_storm_alert gauge\n"
    while IFS= read -r line; do
        if [[ "$line" =~ ALERT:.*fork\ storm.*comm=([^[:space:]]+).*rate=([0-9]+) ]]; then
            local comm="${BASH_REMATCH[1]}"
            local rate="${BASH_REMATCH[2]}"
            output+="ebpf_fork_storm_alert{comm=\"${comm}\"} 1\n"
            output+="ebpf_fork_storm_rate_per_sec{comm=\"${comm}\"} ${rate}\n"
        fi
    done <<< "$input"

    # --- OOM invocation alerts ---
    output+="\n# HELP ebpf_oom_invocation_total Cumulative OOM killer invocations\n"
    output+="# TYPE ebpf_oom_invocation_total counter\n"
    while IFS= read -r line; do
        if [[ "$line" =~ ALERT:.*OOM\ invoked.*comm=([^[:space:]]+).*pid=([0-9]+) ]]; then
            local comm="${BASH_REMATCH[1]}"
            output+="ebpf_oom_invocation_total{comm=\"${comm}\"} 1\n"
        fi
    done <<< "$input"

    # --- OOM victim alerts ---
    output+="\n# HELP ebpf_oom_victim_total Cumulative OOM victim kill events\n"
    output+="# TYPE ebpf_oom_victim_total counter\n"
    while IFS= read -r line; do
        if [[ "$line" =~ ALERT:.*OOM\ victim.*comm=([^[:space:]]+).*pid=([0-9]+) ]]; then
            local comm="${BASH_REMATCH[1]}"
            local pid="${BASH_REMATCH[2]}"
            output+="ebpf_oom_victim_total{comm=\"${comm}\",pid=\"${pid}\"} 1\n"
        elif [[ "$line" =~ STAT\ oom_victim_total.*count=([0-9]+) ]]; then
            output+="ebpf_oom_victim_total{comm=\"aggregate\"} ${BASH_REMATCH[1]}\n"
        fi
    done <<< "$input"

    # --- I/O write storm alerts ---
    output+="\n# HELP ebpf_io_write_rps Block write requests per second per process per device\n"
    output+="# TYPE ebpf_io_write_rps gauge\n"
    output+="\n# HELP ebpf_io_write_storm_alert Write storm detected: 1 if threshold exceeded\n"
    output+="# TYPE ebpf_io_write_storm_alert gauge\n"
    while IFS= read -r line; do
        if [[ "$line" =~ ALERT:.*write\ storm.*comm=([^[:space:]]+).*dev=([^[:space:]]+).*writes/s=([0-9]+) ]]; then
            local comm="${BASH_REMATCH[1]}"
            local device="${BASH_REMATCH[2]}"
            local rps="${BASH_REMATCH[3]}"
            output+="ebpf_io_write_storm_alert{device=\"${device}\",comm=\"${comm}\"} 1\n"
            output+="ebpf_io_write_rps{device=\"${device}\",comm=\"${comm}\"} ${rps}\n"
        elif [[ "$line" =~ STAT\ io_write_rps.*dev=([^[:space:]]+).*comm=([^[:space:]]+).*rps=([0-9]+) ]]; then
            local device="${BASH_REMATCH[1]}"
            local comm="${BASH_REMATCH[2]}"
            local rps="${BASH_REMATCH[3]}"
            output+="ebpf_io_write_rps{device=\"${device}\",comm=\"${comm}\"} ${rps}\n"
        fi
    done <<< "$input"

    # --- I/O latency spike alerts ---
    output+="\n# HELP ebpf_io_latency_spike_alert I/O latency spike detected (>500ms)\n"
    output+="# TYPE ebpf_io_latency_spike_alert gauge\n"
    while IFS= read -r line; do
        if [[ "$line" =~ ALERT:.*I/O\ latency\ spike.*dev=([^[:space:]]+).*lat_ms=([0-9]+).*comm=([^[:space:]]+) ]]; then
            local device="${BASH_REMATCH[1]}"
            local lat="${BASH_REMATCH[2]}"
            local comm="${BASH_REMATCH[3]}"
            output+="ebpf_io_latency_spike_alert{device=\"${device}\",comm=\"${comm}\"} 1\n"
            output+="ebpf_io_latency_spike_ms{device=\"${device}\",comm=\"${comm}\"} ${lat}\n"
        fi
    done <<< "$input"

    # Metadata metric: script health
    output+="\n# HELP ebpf_script_last_run_timestamp_seconds Unix timestamp of last script run\n"
    output+="# TYPE ebpf_script_last_run_timestamp_seconds gauge\n"
    output+="ebpf_script_last_run_timestamp_seconds{script=\"${script_base}\"} ${ts}\n"

    echo -e "$output"
}

# ---------------------------------------------------------------------------
# Write a .prom file atomically
# ---------------------------------------------------------------------------
write_prom_file() {
    local script_name="$1"
    local content="$2"
    local script_base
    script_base=$(basename "$script_name" .bt)
    local out_file="${OUTDIR}/ebpf_${script_base}.prom"
    local tmp_file="${out_file}.tmp.$$"

    if $DO_DRY_RUN; then
        log "[dry-run] Would write to ${out_file}:"
        echo "$content"
        return 0
    fi

    mkdir -p "$OUTDIR"
    echo "$content" > "$tmp_file"
    mv "$tmp_file" "$out_file"
    ok "Wrote ${out_file} ($(wc -l < "$out_file") lines)"
}

# ---------------------------------------------------------------------------
# Run a single bpftrace script and collect output
# ---------------------------------------------------------------------------
run_script() {
    local script="$1"

    if [[ ! -f "$script" ]]; then
        err "Script not found: ${script}"
        return 1
    fi

    log "Running: bpftrace ${script} (timeout ${DURATION}s)"

    local raw_output
    # timeout returns 124 on timeout, which is normal for bpftrace (it runs indefinitely)
    raw_output=$(timeout "$DURATION" bpftrace "$script" 2>&1) || {
        local rc=$?
        if [[ $rc -eq 124 ]]; then
            log "Script ${script} timed out after ${DURATION}s (expected)"
        else
            warn "bpftrace exited with code ${rc} for ${script}"
        fi
    }

    if [[ -z "$raw_output" ]]; then
        warn "No output from ${script}"
        return 0
    fi

    local prom_content
    prom_content=$(parse_bpftrace_output "$script" "$raw_output")
    write_prom_file "$script" "$prom_content"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if ! $DO_DRY_RUN; then
    check_bpftrace
    check_capabilities
else
    log "[dry-run] Skipping bpftrace and capability checks"
    if command -v bpftrace &>/dev/null; then
        ok "bpftrace is available: $(bpftrace --version 2>&1 | head -1)"
    else
        warn "bpftrace not found — would exit 2 in normal mode"
    fi
fi

log "Scripts to run: ${SCRIPTS[*]}"
log "Output directory: ${OUTDIR}"
log "Duration per script: ${DURATION}s"
echo ""

if $DO_LOOP; then
    log "Loop mode enabled — running continuously"
    while true; do
        for script in "${SCRIPTS[@]}"; do
            run_script "$script" &
        done
        # Wait for all background script runs to finish
        wait
        log "Interval complete. Sleeping 2s before next round."
        sleep 2
    done
else
    # Single-shot: run all scripts in parallel
    declare -a pids=()
    for script in "${SCRIPTS[@]}"; do
        run_script "$script" &
        pids+=($!)
    done

    # Wait for all and check exit codes
    failed=0
    for pid in "${pids[@]}"; do
        if ! wait "$pid"; then
            (( failed++ )) || true
        fi
    done

    if [[ $failed -gt 0 ]]; then
        warn "${failed} script(s) reported errors"
        exit 1
    fi

    ok "All scripts completed. Prometheus metrics written to ${OUTDIR}/"
fi
