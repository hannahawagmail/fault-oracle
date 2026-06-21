# Go + eBPF Code Review Fixes Applied

## Fix 1: Shared collectorUpDesc

**File:** `exporter/collectors/shared.go` (already existed; description text updated to be more precise)
**Also modified:** `exporter/collectors/ebpf_edac.go`, `exporter/collectors/edac.go`

`ebpf_edac.go` had a local `c.upDesc` field initialised with its own `prometheus.NewDesc(...)` call
for `fault_resilience_collector_up`. This duplicated the descriptor already declared in `shared.go`,
causing a Prometheus `MustRegister` panic when both collectors were registered in the same process.

Changes:
- Removed the `upDesc *prometheus.Desc` field from `eBPFEDACCollector`.
- Removed the `prometheus.NewDesc(...)` call that created the duplicate descriptor in `NewEBPFEDACCollector`.
- Replaced all references to `c.upDesc` in `Describe` and `Collect` with the package-level `collectorUpDesc`.
- Fixed `edac.go` line 241 where `c.collectorUp` (a non-existent field) was referenced; changed to `collectorUpDesc`.

## Fix 2: Unsynchronized `available` field

**File:** `exporter/collectors/ebpf_edac.go`

The `available bool` field was read in `Collect` (called from Prometheus scrape goroutines) and written
in `NewEBPFEDACCollector` (called from the main goroutine). Without synchronisation this is a data race.

Changes:
- Changed `available bool` to `available atomic.Bool` in the `eBPFEDACCollector` struct.
  (`sync/atomic` was already imported for the existing `atomic.Uint64` counter fields.)
- Changed `c.available = false` / `c.available = true` to `c.available.Store(false)` / `c.available.Store(true)`.
- Changed `if c.available` / `if !c.available` to `if c.available.Load()` / `if !c.available.Load()`.

## Fix 3: BERT bounds check

**File:** `exporter/collectors/apei.go`, function `parseBERTRecordCounts`

`regionOffset` is a `uint64` parsed directly from the raw ACPI table. If the firmware supplies a value
larger than the in-memory table slice, the subsequent `data[regionOffset:]` slice panics with an
index-out-of-range. While the surrounding `if` block partially guards this, an explicit early return
with a descriptive error is cleaner and avoids any ambiguity about which branch is taken.

Change added (before the existing bounds-check block):
```go
if uint64(regionOffset) > uint64(len(data)) {
    return nil, fmt.Errorf("BERT: regionOffset %d exceeds table size %d", regionOffset, len(data))
}
```

## Fix 4: eBPF Makefile GPL license

**File:** `ebpf/Makefile`

The Makefile was tagged `Apache-2.0` but it builds BPF objects (`edac_trace.bpf.c`,
`workload_attr.bpf.c`) that use GPL-only kernel helpers (`bpf_ringbuf_reserve`,
`bpf_map_update_elem`, etc.) and themselves declare `char LICENSE[] SEC("license") = "GPL"`.
The kernel verifier rejects BPF programs that call GPL-only helpers unless the program license
is GPL. The build artifact is GPL-only; the Makefile that produces it should carry the same
identifier to avoid licence confusion in automated scanners.

Change: first line changed from `# SPDX-License-Identifier: Apache-2.0` to
`# SPDX-License-Identifier: GPL-2.0-only`.

## Fix 5: Wrong AER severity field name (eBPF)

**File:** `ebpf/edac_trace.bpf.c`, function `trace_aer_event`

The tracepoint context for `ras:aer_event` exposes the severity string via the BTF field
`error_severity`, not `severity`. Using the wrong field name causes `BPF_CORE_READ` to read
garbage (or zero) at load time with CO-RE relocation, resulting in all AER events being
classified as correctable regardless of their actual severity.

Change:
```c
// Before
bpf_probe_read_str(sev, sizeof(sev), BPF_CORE_READ(ctx, severity));
// After
bpf_probe_read_str(sev, sizeof(sev), BPF_CORE_READ(ctx, error_severity));
```
