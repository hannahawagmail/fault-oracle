# replay — Postmortem Trace Replay Toolkit

This toolkit converts a kernel log containing EDAC/AER/MCE events into a
structured event sequence, then replays that sequence through the fault injection
layer — preserving the original inter-event timing.

## Why Replay?

When an unusual fault pattern occurs, the ability to reproduce it deterministically
is as important as fixing it. Two reasons:

**Timing-sensitive races:** A CE storm that precedes a UE by 200ms may trigger a
race in the page-offline path that a CE followed 30 seconds later by a UE does
not. Replay at original timing is the only reliable way to reproduce such bugs.

**Test-driven postmortem:** Once a trace is captured, it can be replayed in CI
against every subsequent kernel version until the bug is fixed — providing a
regression test that requires no hardware to reproduce.

## Files

| File | Purpose |
|------|---------|
| `parse_edac_trace.py` | Parse kernel log → structured JSON event list |
| `replay_kernel_state.sh` | Re-inject parsed events via fault-injection layer |
| `example_traces/sample_ce_storm.log` | Synthetic CE storm trace for demonstration |

## Quickstart

```bash
# Parse the sample trace
python3 parse_edac_trace.py \
    --input example_traces/sample_ce_storm.log \
    --output /tmp/ce_storm.json \
    --pretty

# Inspect the parsed events
cat /tmp/ce_storm.json | python3 -m json.tool | head -60

# Dry run — see what would be injected
bash replay_kernel_state.sh --events /tmp/ce_storm.json --dry-run

# Replay at 10× speed, skipping UEs (safer for environments without memory_failure configured)
sudo bash replay_kernel_state.sh \
    --events /tmp/ce_storm.json \
    --speed 10.0 \
    --skip-types UE

# Full replay at original timing
sudo bash replay_kernel_state.sh --events /tmp/ce_storm.json
```

## Supported Event Types

| Type | Parser support | Replay support |
|------|---------------|----------------|
| `CE` | Full | Via inject_edac_ce.sh |
| `UE` | Full | Via inject_edac_ue.sh |
| `AER_CE` | Full | Via inject_aer.sh (if aer-inject available) |
| `AER_UE` | Full | Via inject_aer.sh (if aer-inject available) |
| `MCE` | Detected | Logged only (MCE injection requires ACPI EINJ) |

## Capturing Your Own Trace

```bash
# Capture a live dmesg trace (useful during a test run)
dmesg -T --follow 2>&1 | tee my_trace.log

# After the event, parse:
python3 parse_edac_trace.py --input my_trace.log --output events.json --pretty

# Or from journald:
journalctl -k --since "2024-01-01 00:00:00" | python3 parse_edac_trace.py --input - --output events.json
```
