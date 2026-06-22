#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# replay_kernel_state.sh — Replay parsed EDAC/AER event sequences.
#
# Takes a JSON event file produced by parse_edac_trace.py and re-injects each
# event through the driver's fault injection interface, preserving the original
# inter-event timing. This allows an engineer to reproduce the exact kernel
# state of a past fault sequence for postmortem analysis.
#
# Why timing preservation matters:
#   Many EDAC bugs are timing-dependent. A CE storm that precedes a UE by 50ms
#   may trigger a race in the page-offline path that a CE followed 30 seconds
#   later by a UE does not. Replay at original timing is the only reliable way
#   to reproduce timing-sensitive fault sequences.
#
# Usage:
#   sudo bash replay_kernel_state.sh --events events.json [options]
#
# Options:
#   --events <file>    JSON events file from parse_edac_trace.py (required)
#   --dry-run          Print what would be injected without injecting
#   --speed 1.0        Replay speed multiplier (2.0 = 2× faster, default: 1.0)
#   --no-timing        Inject all events immediately (ignore original timing)
#   --skip-types UE    Comma-separated event types to skip (e.g. UE to avoid memory_failure)
#   --controller mc0   Override injection controller (default: from event data)
#   --verbose          Verbose output
#
# Requirements:
#   - jq (JSON processor)
#   - edac_cortex_ref module loaded (for EDAC events)
#   - aer-inject or ACPI EINJ (for AER events)
#
# Exit: 0 = replay complete, 1 = error

set -euo pipefail

EVENTS_FILE=""
DRY_RUN=false
SPEED="1.0"
NO_TIMING=false
SKIP_TYPES=""
CONTROLLER_OVERRIDE=""
VERBOSE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --events)           EVENTS_FILE="$2"; shift 2 ;;
        --dry-run)          DRY_RUN=true; shift ;;
        --speed)            SPEED="$2"; shift 2 ;;
        --no-timing)        NO_TIMING=true; shift ;;
        --skip-types)       SKIP_TYPES="$2"; shift 2 ;;
        --controller)       CONTROLLER_OVERRIDE="$2"; shift 2 ;;
        --verbose)          VERBOSE=true; shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INJECT_DIR="${SCRIPT_DIR}/../fault-injection"
DEBUGFS_ROOT="/sys/kernel/debug/edac_cortex_ref"

log()    { echo "[replay] $*"; }
vlog()   { $VERBOSE && echo "[replay] $*" || true; }
err()    { echo "[replay] ERROR: $*" >&2; }

# ----- Preflight checks ----------------------------------------------------

if [[ -z "$EVENTS_FILE" ]]; then
    err "--events <file> is required"
    exit 1
fi

if [[ ! -f "$EVENTS_FILE" ]]; then
    err "Events file not found: $EVENTS_FILE"
    exit 1
fi

if ! command -v jq &>/dev/null; then
    err "jq is required for JSON parsing. Install: apt-get install jq"
    exit 1
fi

if [[ $EUID -ne 0 ]] && ! $DRY_RUN; then
    err "Must be root to write to debugfs (or use --dry-run)"
    exit 1
fi

# Validate events file
if ! jq empty "$EVENTS_FILE" 2>/dev/null; then
    err "Invalid JSON in events file: $EVENTS_FILE"
    exit 1
fi

EVENT_COUNT=$(jq 'length' "$EVENTS_FILE")
log "Events file: $EVENTS_FILE ($EVENT_COUNT events)"

# Build skip set
declare -A SKIP_SET
if [[ -n "$SKIP_TYPES" ]]; then
    IFS=',' read -ra skip_arr <<< "$SKIP_TYPES"
    for t in "${skip_arr[@]}"; do
        SKIP_SET["$t"]=1
    done
    log "Skipping event types: ${SKIP_TYPES}"
fi

# ----- Replay loop ---------------------------------------------------------

log "Starting replay (speed=${SPEED}× timing=${NO_TIMING})"
$DRY_RUN && log "(DRY RUN — no injection)"

PREV_MONO=0
INJECTED=0
SKIPPED=0
ERRORS=0

# Parse the entire events file once into an array of compact JSON objects,
# rather than re-reading and re-parsing the whole file for every event
# (which is O(n^2) and slow on large CE-storm traces).
mapfile -t EVENTS < <(jq -c '.[]' "$EVENTS_FILE")

for i in "${!EVENTS[@]}"; do
    EVENT="${EVENTS[$i]}"

    EVENT_TYPE=$(echo "$EVENT" | jq -r '.event_type')
    MONO_S=$(echo "$EVENT" | jq -r '.monotonic_s // 0')
    COUNT=$(echo "$EVENT" | jq -r '.count // 1')
    CONTROLLER=$(echo "$EVENT" | jq -r '.controller // "MC0"')
    CSROW=$(echo "$EVENT" | jq -r '.csrow // 0')
    CHANNEL=$(echo "$EVENT" | jq -r '.channel // 0')
    PCI_DEVICE=$(echo "$EVENT" | jq -r '.pci_device // ""')
    RAW=$(echo "$EVENT" | jq -r '.raw_message')

    vlog "Event $i: type=$EVENT_TYPE controller=$CONTROLLER csrow=$CSROW ch=$CHANNEL count=$COUNT"
    vlog "  Raw: $RAW"

    # Check skip set
    if [[ -n "${SKIP_SET[$EVENT_TYPE]:-}" ]]; then
        log "SKIP [$i]: $EVENT_TYPE (in --skip-types)"
        SKIPPED=$((SKIPPED+1))
        continue
    fi

    # Apply inter-event timing
    if ! $NO_TIMING && [[ "$MONO_S" != "0" && "$MONO_S" != "null" ]]; then
        if [[ "$PREV_MONO" != "0" ]]; then
            DELTA=$(echo "scale=6; ($MONO_S - $PREV_MONO) / $SPEED" | bc)
            # Clamp delta to [0, 60] seconds to avoid unreasonable waits
            DELTA=$(echo "if ($DELTA < 0) 0 else if ($DELTA > 60) 60 else $DELTA" | bc)
            if [[ $(echo "$DELTA > 0.001" | bc) -eq 1 ]]; then
                vlog "Waiting ${DELTA}s (inter-event gap / speed=${SPEED})"
                $DRY_RUN || sleep "$DELTA"
            fi
        fi
        PREV_MONO="$MONO_S"
    fi

    # Determine effective controller
    EFFECTIVE_CTRL="${CONTROLLER_OVERRIDE:-${CONTROLLER,,}}"
    # Normalize: MC0 → mc0
    EFFECTIVE_CTRL="${EFFECTIVE_CTRL/MC/mc}"

    # Perform injection
    if $DRY_RUN; then
        log "DRY-RUN [$i]: would inject $EVENT_TYPE count=$COUNT ctrl=$EFFECTIVE_CTRL csrow=$CSROW ch=$CHANNEL"
        INJECTED=$((INJECTED+1))
        continue
    fi

    EXIT_CODE=0
    case "$EVENT_TYPE" in
        CE)
            bash "${INJECT_DIR}/inject_edac_ce.sh" \
                --controller "$EFFECTIVE_CTRL" \
                --csrow "$CSROW" \
                --channel "$CHANNEL" \
                --count "$COUNT" \
                --no-verify \
                > /tmp/replay_event_${i}.log 2>&1 || EXIT_CODE=$?
            ;;
        UE)
            bash "${INJECT_DIR}/inject_edac_ue.sh" \
                --controller "$EFFECTIVE_CTRL" \
                --csrow "$CSROW" \
                --channel "$CHANNEL" \
                --count "$COUNT" \
                --no-verify \
                > /tmp/replay_event_${i}.log 2>&1 || EXIT_CODE=$?
            ;;
        AER_CE)
            bash "${INJECT_DIR}/inject_aer.sh" \
                --type correctable \
                ${PCI_DEVICE:+--device "$PCI_DEVICE"} \
                --no-verify \
                > /tmp/replay_event_${i}.log 2>&1 || EXIT_CODE=$?
            [[ $EXIT_CODE -eq 2 ]] && EXIT_CODE=0  # skip if no AER device
            ;;
        AER_UE)
            bash "${INJECT_DIR}/inject_aer.sh" \
                --type nonfatal \
                ${PCI_DEVICE:+--device "$PCI_DEVICE"} \
                --no-verify \
                > /tmp/replay_event_${i}.log 2>&1 || EXIT_CODE=$?
            [[ $EXIT_CODE -eq 2 ]] && EXIT_CODE=0
            ;;
        MCE)
            log "INFO [$i]: MCE events cannot be injected via debugfs — logging only"
            SKIPPED=$((SKIPPED+1))
            continue
            ;;
        *)
            log "SKIP [$i]: unknown event type $EVENT_TYPE"
            SKIPPED=$((SKIPPED+1))
            continue
            ;;
    esac

    if [[ $EXIT_CODE -ne 0 ]]; then
        err "FAIL [$i]: $EVENT_TYPE injection failed (exit $EXIT_CODE)"
        err "  Log: /tmp/replay_event_${i}.log"
        ERRORS=$((ERRORS+1))
    else
        vlog "OK [$i]: $EVENT_TYPE injected"
        INJECTED=$((INJECTED+1))
    fi
done

# ----- Summary -------------------------------------------------------------

log ""
log "=== Replay Complete ==="
log "Events processed: $EVENT_COUNT"
log "Injected:         $INJECTED"
log "Skipped:          $SKIPPED"
log "Errors:           $ERRORS"

if [[ $ERRORS -gt 0 ]]; then
    err "Replay finished with $ERRORS error(s). Check /tmp/replay_event_*.log"
    exit 1
fi

log "Replay successful."
exit 0
