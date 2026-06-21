# Go Comment Review Findings
Generated: 2026-06-21

## Files Reviewed

- `exporter/collectors/edac.go`
- `exporter/collectors/apei.go`
- `exporter/collectors/ebpf_edac.go`
- `exporter/main.go`

---

## exporter/collectors/edac.go

All exported symbols already had compliant doc comments beginning with the symbol name. No changes required.

**Verified present:**
- `// EDACCollector implements prometheus.Collector for the EDAC subsystem.`
- `// NewEDACCollector creates a new EDAC collector.`
- `// Describe implements prometheus.Collector.`
- `// Collect implements prometheus.Collector.`
- `// readUint64 reads a sysfs file and parses its content as a uint64.`
- `// emitSelf emits the exporter's own scrape health metrics.`

---

## exporter/collectors/apei.go

### Added

- **Package doc comment** — The file had a detailed file-level block comment but the `package collectors` declaration lacked the Go-convention package doc comment immediately above it. Added:
  ```go
  // Package collectors provides Prometheus collectors for ARM Linux hardware
  // error reporting subsystems (EDAC, PCIe AER, APEI BERT, MCE, eBPF tracepoints).
  package collectors
  ```

**Verified present (no change needed):**
- `// APEICollector reads ACPI BERT boot error records.`
- `// NewAPEICollector returns a new APEICollector.`
- `// Describe implements prometheus.Collector.`
- `// Collect implements prometheus.Collector.`
- `// severityName maps CPER severity field (uint32) to a string label.`
- `// parseBERTRecordCounts walks CPER records in the BERT region and returns…`
- All exported constants: `CPER_HEADER_SIZE` had an inline comment.

---

## exporter/collectors/ebpf_edac.go

### Added

- **`Describe` method** — Was missing a doc comment. Added:
  ```go
  // Describe implements prometheus.Collector.
  func (c *eBPFEDACCollector) Describe(ch chan<- *prometheus.Desc) {
  ```

- **`Collect` method** — Was missing a doc comment. Added:
  ```go
  // Collect implements prometheus.Collector. Emits collector_up=0 when eBPF is
  // unavailable (kernel too old, missing BTF, or EBPF_DISABLED=1) so dashboards
  // and alerts can distinguish "eBPF fell back to sysfs" from "exporter crashed".
  func (c *eBPFEDACCollector) Collect(ch chan<- prometheus.Metric) {
  ```

- **`handleEvent` method** — Was missing a doc comment. Added:
  ```go
  // handleEvent decodes a raw BPF ring buffer record and increments the
  // appropriate counter. It is called from drainRingBuffer() for every event.
  func (c *eBPFEDACCollector) handleEvent(raw []byte) {
  ```

- **`ebpfStartTime` var comment** — The comment `// EBPFCollectorUptime returns how long the eBPF collector has been running.` was incorrectly placed on the `var` declaration rather than on the `EBPFUptimeSeconds` function, creating a misleading association. Fixed by splitting into:
  ```go
  // ebpfStartTime records when the package was initialised, used by EBPFUptimeSeconds.
  var ebpfStartTime = time.Now()

  // EBPFUptimeSeconds returns how many seconds have elapsed since the eBPF
  // collector package was first loaded. Useful for dashboards and tests.
  func EBPFUptimeSeconds() float64 {
  ```

**Verified present (no change needed):**
- `// eBPFEDACCollector collects EDAC/MCE/AER events from the BPF ring buffer.`
- `// NewEBPFEDACCollector returns a new eBPF-backed collector.`
- `// start loads the BPF object and attaches tracepoints.`
- `// drainRingBuffer is the background goroutine that reads events from the…`
- `// checkKernelVersion returns nil if running kernel >= major.minor.`
- `// EBPFAvailable returns true if the current kernel and process capabilities…`
- All event type and severity constants: grouped comment present.
- `// mcEvent mirrors struct mc_event_t from the BPF C code.`
- `// mcKey` — unnamed struct, private, no doc comment required.

---

## exporter/main.go

### Added

- **`buildLogger` function** — Was missing a doc comment. Added:
  ```go
  // buildLogger creates a zap production logger configured for the given level string.
  // Accepts "debug", "info" (default), "warn", or "error". Unknown strings fall through
  // to info level. Uses ISO8601 timestamps so log lines are human-readable in journald.
  func buildLogger(level string) *zap.Logger {
  ```

- **`collectorLI` function** — Was missing a doc comment. Added:
  ```go
  // collectorLI renders an HTML list item for the exporter landing page.
  func collectorLI(name string, enabled bool, desc string) string {
  ```

**Verified present (no change needed):**
- File-level block comment documents all three collector subsystems and metric type rationale.
- `// zapErrorLogger adapts zap.Logger to the promhttp.Logger interface.`
- `// buildTLSConfig returns a *tls.Config that enables mTLS when caPath is set.`
- All constants (`defaultListenAddr`, etc.) have inline comments via the `flag.String` help strings.

---

## Summary

| File | Symbols added/fixed |
|------|---------------------|
| `edac.go` | 0 (all present) |
| `apei.go` | 1 (package doc) |
| `ebpf_edac.go` | 4 (`Describe`, `Collect`, `handleEvent`, `ebpfStartTime`/`EBPFUptimeSeconds` split) |
| `main.go` | 2 (`buildLogger`, `collectorLI`) |
