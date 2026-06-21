#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# oom-tuning.sh — Configure OOM killer priorities for embedded ARM systems
#
# Usage:
#   bash oom-tuning.sh [--status] [--protect <service>] [--deprioritize <service>]
#                      [--apply-defaults] [--cgroup-limit <service> <size_mb>]
#                      [--dry-run]
#
# Options:
#   --status                     Show all processes with OOM score, RSS, adj value
#   --protect <service>          Set oom_score_adj=-900 for all PIDs of a systemd service
#                                and write a persistent drop-in override
#   --deprioritize <service>     Set oom_score_adj=+500 for all PIDs of a service
#   --apply-defaults             Apply hardened defaults to a predefined set of services
#   --cgroup-limit <svc> <MB>    Write a MemoryMax= drop-in for the service
#   --dry-run                    Show what would be done without making changes
#
# Exit codes: 0=success, 1=error

set -e

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DO_STATUS=false
DO_APPLY_DEFAULTS=false
DO_DRY_RUN=false
PROTECT_SERVICE=""
DEPRIO_SERVICE=""
CGROUP_SERVICE=""
CGROUP_MB=""

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
BLU='\033[0;34m'
CYN='\033[0;36m'
RST='\033[0m'

log()  { echo -e "${BLU}[oom-tuning]${RST} $*"; }
ok()   { echo -e "${GRN}[OK]${RST} $*"; }
warn() { echo -e "${YLW}[WARN]${RST} $*"; }
err()  { echo -e "${RED}[ERR]${RST} $*" >&2; }

usage() {
    sed -n '3,16p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
[[ $# -eq 0 ]] && usage

while [[ $# -gt 0 ]]; do
    case "$1" in
        --status)           DO_STATUS=true ;;
        --apply-defaults)   DO_APPLY_DEFAULTS=true ;;
        --dry-run)          DO_DRY_RUN=true ;;
        --protect)
            shift; [[ -z "$1" ]] && { err "--protect requires a service name"; exit 1; }
            PROTECT_SERVICE="$1"
            ;;
        --deprioritize)
            shift; [[ -z "$1" ]] && { err "--deprioritize requires a service name"; exit 1; }
            DEPRIO_SERVICE="$1"
            ;;
        --cgroup-limit)
            shift; CGROUP_SERVICE="$1"
            shift; CGROUP_MB="$1"
            if [[ -z "$CGROUP_SERVICE" || -z "$CGROUP_MB" ]]; then
                err "--cgroup-limit requires <service> and <size_mb>"
                exit 1
            fi
            if [[ ! "$CGROUP_MB" =~ ^[0-9]+$ ]]; then
                err "--cgroup-limit size must be a positive integer (MB)"
                exit 1
            fi
            ;;
        --help|-h) usage ;;
        *)
            err "Unknown option: $1"
            exit 1
            ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# Helper: read a single field from /proc/<pid>/status
# ---------------------------------------------------------------------------
proc_status_field() {
    local pid="$1" field="$2"
    local f="/proc/${pid}/status"
    [[ -r "$f" ]] || return 1
    grep -m1 "^${field}:" "$f" | awk '{print $2}'
}

# ---------------------------------------------------------------------------
# Helper: get PIDs for a systemd service (or plain process name)
# ---------------------------------------------------------------------------
get_service_pids() {
    local svc="$1"
    local pids=""

    # Try systemctl first (most accurate for services)
    if command -v systemctl &>/dev/null 2>&1; then
        pids=$(systemctl show -p MainPID,ControlGroup --value "$svc" 2>/dev/null \
               | awk '/^[0-9]+$/ && $1 != "0" {print $1}')
        # Also collect all cgroup PIDs
        local cg
        cg=$(systemctl show -p ControlGroup --value "$svc" 2>/dev/null || true)
        if [[ -n "$cg" && -r "/sys/fs/cgroup${cg}/cgroup.procs" ]]; then
            pids="$pids $(cat "/sys/fs/cgroup${cg}/cgroup.procs" 2>/dev/null || true)"
        fi
    fi

    # Fall back to pgrep
    if [[ -z "$pids" ]]; then
        pids=$(pgrep -x "$svc" 2>/dev/null || pgrep "$svc" 2>/dev/null || true)
    fi

    # Deduplicate and filter valid PIDs
    echo "$pids" | tr ' ' '\n' | sort -u | grep -v '^$' || true
}

# ---------------------------------------------------------------------------
# Helper: set oom_score_adj for a list of PIDs
# ---------------------------------------------------------------------------
set_oom_adj() {
    local adj="$1"
    shift
    local pids=("$@")
    local changed=0

    for pid in "${pids[@]}"; do
        [[ -z "$pid" ]] && continue
        local oom_file="/proc/${pid}/oom_score_adj"
        if [[ ! -w "$oom_file" ]]; then
            warn "Cannot write to ${oom_file} (PID ${pid} may have exited or need root)"
            continue
        fi
        if $DO_DRY_RUN; then
            log "[dry-run] Would write ${adj} to ${oom_file} (PID ${pid})"
        else
            echo "$adj" > "$oom_file"
            ok "PID ${pid}: oom_score_adj set to ${adj}"
        fi
        (( changed++ )) || true
    done
    return 0
}

# ---------------------------------------------------------------------------
# Helper: write a systemd drop-in override
# ---------------------------------------------------------------------------
write_systemd_dropin() {
    local svc="$1"
    local dropin_dir="/etc/systemd/system/${svc}.d"
    local dropin_file="${dropin_dir}/oom.conf"
    local adj="$2"
    local content="[Service]
OOMScoreAdjust=${adj}"

    if $DO_DRY_RUN; then
        log "[dry-run] Would write to ${dropin_file}:"
        echo "  $content"
        return 0
    fi

    if [[ $EUID -ne 0 ]]; then
        warn "Not root — skipping systemd drop-in for ${svc} (run as root for persistence)"
        return 0
    fi

    mkdir -p "$dropin_dir"
    echo "$content" > "${dropin_file}.tmp"
    mv "${dropin_file}.tmp" "$dropin_file"
    ok "Wrote ${dropin_file}"
    systemctl daemon-reload 2>/dev/null && ok "systemctl daemon-reload done" || true
}

# ---------------------------------------------------------------------------
# Helper: show /proc/meminfo summary
# ---------------------------------------------------------------------------
show_meminfo() {
    echo ""
    log "Memory overview (/proc/meminfo):"
    echo ""
    if [[ -r /proc/meminfo ]]; then
        local fields=(MemTotal MemAvailable SwapTotal SwapFree Committed_AS)
        for f in "${fields[@]}"; do
            local val
            val=$(grep -m1 "^${f}:" /proc/meminfo | awk '{printf "%d %s", $2, $3}' || echo "N/A")
            printf "  %-20s %s\n" "${f}:" "$val"
        done
    else
        warn "Cannot read /proc/meminfo"
    fi
    echo ""
}

# ---------------------------------------------------------------------------
# --status: show all processes sorted by oom_score
# ---------------------------------------------------------------------------
if $DO_STATUS; then
    show_meminfo

    log "Process OOM scores (top 10 by oom_score):"
    echo ""
    printf "  ${CYN}%-8s %-30s %10s %12s %14s${RST}\n" \
        "PID" "Name" "oom_score" "oom_score_adj" "RSS (KB)"

    declare -a ROWS

    for pid_dir in /proc/[0-9]*/; do
        pid="${pid_dir//[^0-9]/}"
        [[ -z "$pid" ]] && continue

        score_file="/proc/${pid}/oom_score"
        adj_file="/proc/${pid}/oom_score_adj"
        status_file="/proc/${pid}/status"

        [[ -r "$score_file" && -r "$adj_file" && -r "$status_file" ]] || continue

        score=$(cat "$score_file" 2>/dev/null) || continue
        adj=$(cat "$adj_file" 2>/dev/null) || continue
        name=$(grep -m1 "^Name:" "$status_file" 2>/dev/null | awk '{print $2}') || continue
        rss=$(grep -m1 "^VmRSS:" "$status_file" 2>/dev/null | awk '{print $2}') || continue

        ROWS+=("${score} ${pid} ${name} ${adj} ${rss:-0}")
    done

    # Sort by oom_score descending, take top 10
    printf '%s\n' "${ROWS[@]}" | sort -rn | head -10 | while IFS= read -r row; do
        read -r score pid name adj rss <<< "$row"
        color="$RST"
        [[ "$score" -ge 500 ]] && color="$RED"
        [[ "$score" -ge 200 && "$score" -lt 500 ]] && color="$YLW"
        [[ "$adj" -le -500 ]] && color="$GRN"

        printf "  ${color}%-8s %-30s %10s %12s %14s${RST}\n" \
            "$pid" "$name" "$score" "$adj" "$rss"
    done

    echo ""
    log "Tip: run with --protect <service> to lower oom_score_adj for critical services"
    echo ""
fi

# ---------------------------------------------------------------------------
# --protect <service>
# ---------------------------------------------------------------------------
if [[ -n "$PROTECT_SERVICE" ]]; then
    log "Protecting ${PROTECT_SERVICE} (oom_score_adj=-900) ..."
    mapfile -t pids < <(get_service_pids "$PROTECT_SERVICE")

    if [[ ${#pids[@]} -eq 0 ]]; then
        warn "No PIDs found for service '${PROTECT_SERVICE}' — writing drop-in anyway"
    else
        set_oom_adj -900 "${pids[@]}"
    fi

    write_systemd_dropin "$PROTECT_SERVICE" -900
fi

# ---------------------------------------------------------------------------
# --deprioritize <service>
# ---------------------------------------------------------------------------
if [[ -n "$DEPRIO_SERVICE" ]]; then
    log "Deprioritizing ${DEPRIO_SERVICE} (oom_score_adj=+500) ..."
    mapfile -t pids < <(get_service_pids "$DEPRIO_SERVICE")

    if [[ ${#pids[@]} -eq 0 ]]; then
        warn "No PIDs found for service '${DEPRIO_SERVICE}' — writing drop-in anyway"
    else
        set_oom_adj 500 "${pids[@]}"
    fi

    write_systemd_dropin "$DEPRIO_SERVICE" 500
fi

# ---------------------------------------------------------------------------
# --cgroup-limit <service> <size_mb>
# ---------------------------------------------------------------------------
if [[ -n "$CGROUP_SERVICE" && -n "$CGROUP_MB" ]]; then
    dropin_dir="/etc/systemd/system/${CGROUP_SERVICE}.d"
    dropin_file="${dropin_dir}/memory-limit.conf"
    content="[Service]
# Hard memory limit — OOM killer will only look inside this cgroup when hit
MemoryMax=${CGROUP_MB}M
# Disable swap usage for this cgroup (important on flash devices)
MemorySwapMax=0
# Throttle allocations at 90% of the hard limit for earlier warning
MemoryHigh=$(( CGROUP_MB * 90 / 100 ))M"

    log "Writing cgroup MemoryMax=${CGROUP_MB}M for ${CGROUP_SERVICE} ..."

    if $DO_DRY_RUN; then
        log "[dry-run] Would write to ${dropin_file}:"
        echo "$content"
    else
        if [[ $EUID -ne 0 ]]; then
            err "Root required to write systemd overrides"
            exit 1
        fi
        mkdir -p "$dropin_dir"
        echo "$content" > "${dropin_file}.tmp"
        mv "${dropin_file}.tmp" "$dropin_file"
        ok "Wrote ${dropin_file}"
        systemctl daemon-reload 2>/dev/null && ok "systemctl daemon-reload done" || true
        ok "Restart ${CGROUP_SERVICE} to apply: systemctl restart ${CGROUP_SERVICE}"
    fi
fi

# ---------------------------------------------------------------------------
# --apply-defaults
# ---------------------------------------------------------------------------
if $DO_APPLY_DEFAULTS; then
    log "Applying default OOM tuning for critical and non-critical services..."
    echo ""

    PROTECT_LIST=(sshd NetworkManager systemd-journald hw-fault-exporter containerd)
    DEPRIO_LIST=(user@1000.service systemd-tmpfiles-clean.service)

    protected_count=0
    deprioritized_count=0

    for svc in "${PROTECT_LIST[@]}"; do
        log "Protecting: ${svc}"
        mapfile -t pids < <(get_service_pids "$svc")
        if [[ ${#pids[@]} -gt 0 ]]; then
            set_oom_adj -900 "${pids[@]}"
            (( protected_count++ )) || true
        else
            warn "  ${svc}: no PIDs found (may not be running)"
        fi
        write_systemd_dropin "$svc" -900
    done

    echo ""
    for svc in "${DEPRIO_LIST[@]}"; do
        log "Deprioritizing: ${svc}"
        mapfile -t pids < <(get_service_pids "$svc")
        if [[ ${#pids[@]} -gt 0 ]]; then
            set_oom_adj 500 "${pids[@]}"
            (( deprioritized_count++ )) || true
        else
            warn "  ${svc}: no PIDs found (may not be running)"
        fi
        write_systemd_dropin "$svc" 500
    done

    echo ""
    show_meminfo

    # Compute a rough total memory pressure score (sum of oom_scores of top 20 processes)
    total_pressure=0
    for pid_dir in /proc/[0-9]*/; do
        pid="${pid_dir//[^0-9]/}"
        score_file="/proc/${pid}/oom_score"
        [[ -r "$score_file" ]] || continue
        score=$(cat "$score_file" 2>/dev/null) || continue
        (( total_pressure += score )) || true
    done

    echo ""
    log "Summary:"
    printf "  Protected services:     %d\n" "$protected_count"
    printf "  Deprioritized services: %d\n" "$deprioritized_count"
    printf "  Total memory pressure score (sum of all oom_scores): %d\n" "$total_pressure"
    echo ""
    ok "Default OOM tuning applied. Restart services to pick up drop-in overrides."
fi
