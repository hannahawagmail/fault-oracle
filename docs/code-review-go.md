# Code Review: Go Exporter and Collectors

## Summary

The codebase is well-structured and shows solid engineering discipline: graceful degradation is consistent, binary parsing is mostly correct, and the HTTP server is production-ready (mTLS, readiness probes, timeout guards). However, there are two blockers — a startup panic caused by duplicate Prometheus descriptor registration and a thread-safety gap where map reads in `Collect()` race against background goroutine writes — that must be fixed before the exporter can be shipped.

---

## Findings

### [BLOCKER] Duplicate `fault_resilience_collector_up` descriptor causes MustRegister panic

File: `exporter/collectors/apei.go` Line: 53  
File: `exporter/collectors/edac.go` Line: 121  
File: `exporter/collectors/ebpf_edac.go` Line: 111

Issue: All three collectors independently call `prometheus.NewDesc("fault_resilience_collector_up", ..., []string{"collector"}, nil)`. Prometheus computes a descriptor fingerprint from the fully-qualified metric name plus the sorted label names. Three independent `NewDesc` calls with the same FQName and same label set produce three *distinct* `*prometheus.Desc` objects with the same fingerprint. When the second collector is passed to `registry.MustRegister`, Prometheus detects the collision and panics with `duplicate metrics collector registration attempted`. The exporter will never start once all three collectors are active.

Fix: Define a single shared descriptor in one place (e.g., a `shared.go` file in the `collectors` package) and pass it to each collector via `Options` or a package-level variable. Alternatively, use `prometheus.NewRegistry()` per-collector in tests and share a `collectorUpDesc` singleton:

```go
// collectors/shared.go
var CollectorUpDesc = prometheus.NewDesc(
    "fault_resilience_collector_up",
    "1 if the named collector subsystem is accessible, 0 otherwise.",
    []string{"collector"}, nil,
)
```

The same pattern applies to `hw_fault_exporter_scrape_duration_seconds` and `hw_fault_exporter_scrape_errors_total` defined in `edac.go` — if `SelfCollector` (not reviewed) registers those same names, `MustRegister(selfCollector)` will also panic.

---

### [BLOCKER] Race between `handleEvent` map writes and `Collect` map reads on AER/MCE atomics vs mutex scope

File: `exporter/collectors/ebpf_edac.go` Lines: 161–193, 204–237

Issue: `handleEvent` increments `c.aerCE`, `c.aerUE`, `c.aerFatal`, and `c.mceTotal` without holding `c.mu`. `Collect()` acquires `c.mu` for the `mcCE`/`mcUE` map iteration (lines 215–232) but then reads the atomic counters *after* `defer c.mu.Unlock()` is already scheduled — and those atomic reads at lines 234–237 happen concurrently with potential `handleEvent` increments. This is technically safe for the `atomic.Uint64` values themselves, but the *snapshot consistency* is broken: a single scrape can observe mcCE/mcUE as of lock-acquisition time while observing AER counts from milliseconds later. For hardware error counters presented as monotonic Prometheus Counters this creates counter resets on re-scrape if the process restarts, but more seriously it means a single gather can report inconsistent state.

More critically: `c.available` (line 206) is written in `NewEBPFEDACCollector` (the constructor, lines 121–125) from one goroutine and read in `Collect()` (called from an http handler goroutine) without any synchronization. Go's memory model does not guarantee visibility without a synchronization primitive or atomic load/store.

Fix: Protect `c.available` with an `atomic.Bool` (Go 1.19+). Extend the mutex to cover the AER/MCE atomic snapshots, or snapshot all counters inside a single `c.mu.Lock()` block and emit them after releasing the lock:

```go
c.mu.Lock()
mcCE := cloneMap(c.mcCE)
mcUE := cloneMap(c.mcUE)
aerCE := c.aerCE.Load()
aerUE := c.aerUE.Load()
aerFatal := c.aerFatal.Load()
mceTotal := c.mceTotal.Load()
c.mu.Unlock()
// emit from snapshots
```

---

### [MAJOR] BERT `regionOffset` integer conversion allows silent wrap on malformed input

File: `exporter/collectors/apei.go` Lines: 122–126

Issue: `regionOffset` is `uint64` and `regionLength` is `uint32`. The bounds check is:

```go
if regionOffset > 0 && int(regionOffset)+int(regionLength) <= len(data) {
    region = data[regionOffset : regionOffset+uint64(regionLength)]
}
```

On a 64-bit host, `int` is 64-bit, so wrapping cannot occur. However, if `regionOffset` is a very large value (e.g., `0xFFFFFFFFFFFFFFFF` from a corrupted table), `int(regionOffset)` becomes a large negative number, the bounds check passes (negative + small positive is negative, which is `<= len(data)`), and the slice expression `data[regionOffset:]` panics with an out-of-bounds index because the slice operator uses the raw `uint64` value.

Fix: Add an explicit upper-bound guard before the slice:

```go
if regionOffset > 0 &&
    regionOffset < uint64(len(data)) &&
    uint64(regionLength) <= uint64(len(data))-regionOffset {
    region = data[regionOffset : regionOffset+uint64(regionLength)]
}
```

---

### [MAJOR] `edac.go` emits `collectorUp=1` even when all per-controller reads fail

File: `exporter/collectors/edac.go` Lines: 241–242

Issue: `collector_up=1` is emitted unconditionally at the bottom of `Collect()` as long as `os.ReadDir(mcRoot)` succeeded. If `mcRoot` exists but every `ce_count`/`ue_count` read inside it fails (e.g., permission denied after startup), all metric families will be empty while `collector_up=1` signals health. Alerting rules that fire on `collector_up == 0` will not trigger, and the scrape will appear healthy while emitting no hardware error data.

Fix: Track a `subsystemHealthy` boolean. Set it to `false` if `errCount` exceeds the number of discovered controllers, and emit `collectorUp` accordingly — or at minimum emit the `errCount` via `scrapeErrors` loudly and document that non-zero `scrape_errors_total` means partial data.

---

### [MAJOR] `Collect()` acquires mutex but `handleEvent` increments AER/MCE atomics outside it — mixed synchronization model is confusing and fragile

File: `exporter/collectors/ebpf_edac.go` Lines: 69–79, 181–193

Issue: The struct uses *two* synchronization mechanisms for different fields: a `sync.Mutex` for `mcCE`/`mcUE` maps and `atomic.Uint64` for `aerCE`/`aerUE`/`aerFatal`/`mceTotal`. This is not incorrect per se, but the mixed model means future contributors may add new counter types and choose the wrong mechanism. The comment block labels `mu sync.Mutex` as protecting "Atomic counters drained from the ring buffer", which incorrectly implies the atomics are also mutex-protected, when they are not.

Fix: Either unify on mutex-protected `uint64` fields for all counters (simplest), or switch fully to atomics for all counter types. Update the comment to accurately describe which mechanism protects which fields.

---

### [MINOR] `arch.go` missing `arm` (32-bit ARM) GOARCH mapping

File: `exporter/collectors/arch.go` Lines: 25–34

Issue: `runtime.GOARCH == "arm"` (32-bit ARM, e.g., ARMv7) falls through to `ArchUnknown`. On a 32-bit ARM host, `CollectorSupported("apei")` returns `true` (because `ArchUnknown` is neither `ArchARM64` nor `ArchX86_64`, so the APEI case returns `false` — actually `ArchUnknown == ArchARM64` is false, so `return arch == ArchARM64 || arch == ArchX86_64` returns `false`). The PMU case similarly returns `false`. However, `default: return true` means EDAC, MCE, and AER are marked as supported on an `ArchUnknown` host, which may or may not be correct. If the binary is ever cross-compiled for `arm` (Raspberry Pi fleet monitoring), the arch gate gives no protection.

Fix: Add explicit `case "arm": return "armv7"` (or `"arm"`) mapping and document intentional behaviour for that arch, or add it to the `default` branch with a comment.

---

### [MINOR] `ebpf_edac.go`: `eBPFEDACCollector` type and `NewEBPFEDACCollector` are unexported/exported inconsistently

File: `exporter/collectors/ebpf_edac.go` Lines: 59, 85

Issue: The struct type `eBPFEDACCollector` is unexported (lowercase `e`), but the constructor `NewEBPFEDACCollector` is exported. This is a valid Go pattern (return an interface), but the function's return type is `*eBPFEDACCollector` (a concrete unexported type), not `prometheus.Collector`. Callers in external packages cannot refer to the returned type by name, which prevents storing it in a typed variable for testing or method access beyond the `prometheus.Collector` interface.

Fix: Either export the type (`EBPFEDACCollector`) for consistency with `EDACCollector` and `APEICollector`, or change the return type to `prometheus.Collector`:

```go
func NewEBPFEDACCollector(opts Options) prometheus.Collector {
```

---

### [MINOR] `main.go` builds logger with `cfg.Build()` and discards the error

File: `exporter/main.go` Line: 283

Issue: `logger, _ := cfg.Build()` silently discards the `error` return from `zap.Config.Build()`. If `zap` fails to initialize (e.g., invalid `OutputPaths` after config modification), `logger` will be `nil` and any subsequent `logger.Info(...)` call will panic.

Fix:
```go
logger, err := cfg.Build()
if err != nil {
    fmt.Fprintf(os.Stderr, "failed to build logger: %v\n", err)
    os.Exit(1)
}
```

---

### [MINOR] `apei.go`: no godoc on `Describe` and `Collect` methods

File: `exporter/collectors/apei.go` Lines: 61, 66

Issue: `APEICollector.Describe` and `APEICollector.Collect` have no godoc comments. While they implement a well-known interface, the project's other collectors (`EDACCollector`) document their interface implementations with `// Describe implements prometheus.Collector.` for consistency and `go doc` output.

Fix: Add `// Describe implements prometheus.Collector.` and `// Collect implements prometheus.Collector.` to both methods.

---

### [MINOR] `ebpf_edac.go`: `EBPFCollectorUptime` godoc comment is on wrong symbol

File: `exporter/collectors/ebpf_edac.go` Lines: 274–278

Issue: The comment `// EBPFCollectorUptime returns how long the eBPF collector has been running.` is attached to the *variable* `ebpfStartTime`, not to the *function* `EBPFUptimeSeconds`. In Go, a godoc comment must immediately precede the declaration it documents. The function `EBPFUptimeSeconds` has no godoc comment.

Fix:
```go
var ebpfStartTime = time.Now()

// EBPFUptimeSeconds returns how many seconds the eBPF collector has been running.
func EBPFUptimeSeconds() float64 {
    return time.Since(ebpfStartTime).Seconds()
}
```

---

### [NIT] `apei.go` constant comment mis-states BERT body layout

File: `exporter/collectors/apei.go` Line: 27

Issue: The comment says `BERT body: BootErrorRegionLength (4B) + BootErrorRegionOffset (8B)`. The UEFI 2.9 spec (Table 18-387) calls the second field `BootErrorRegionPhysicalAddress`, not `BootErrorRegionOffset`. "Offset" implies a relative position within the file, but this is a physical memory address. The code reads and uses it as an absolute address into the file buffer, which is correct only when the file is the raw BERT table; the name mismatch will mislead future readers.

Fix: Rename the constant comment label to `BootErrorRegionPhysicalAddress` to match the UEFI specification terminology.

---

### [NIT] `edac.go`: `readUint64` scanner reads only first line but does not drain the scanner

File: `exporter/collectors/edac.go` Lines: 257–261

Issue: After calling `scanner.Scan()` once and returning, the underlying file descriptor is closed by `defer f.Close()` before the scanner's buffer is drained. This is benign for sysfs files (which are always single-line), but leaves a latent risk: if any sysfs path ever returns multiple lines (e.g., a future kernel version adding debug info), only the first line is parsed without any warning. This is low risk in practice but worth noting.

Fix: No immediate action needed for sysfs, but a comment like `// sysfs guarantees single-line integer output` would make the assumption explicit.

---

### [NIT] `main.go`: `collectorLI` HTML output is not escaped

File: `exporter/main.go` Lines: 318–323

Issue: `collectorLI` embeds `name` and `desc` strings directly into HTML with `fmt.Sprintf`. If either string ever contains `<`, `>`, or `&`, the HTML will be malformed. Currently the strings are string literals, so this is not exploitable, but the pattern is fragile.

Fix: Use `html/template` or call `html.EscapeString(name)` and `html.EscapeString(desc)` before interpolation.

---

### [NIT] `arch.go`: `CollectorSupported` does not log unsupported collectors

File: `exporter/collectors/arch.go` Lines: 49–63

Issue: `CollectorSupported` returns a `bool` with no logging. When it returns `false` and a collector is silently skipped in `main.go`, there is no log entry to explain why the APEI or PMU collector is absent. Debugging missing metrics in production requires re-reading source rather than checking logs.

Fix: Either accept a `*zap.Logger` parameter and log a `debug` message, or have `main.go` log the result of each `CollectorSupported` call before skipping registration.

---

## Metrics

- Files reviewed: 5
- Blockers: 2
- Majors: 3
- Minors: 4
- Nits: 4
