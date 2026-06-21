# Code Review: eBPF C Programs

Reviewed by: independent code review agent
Date: 2026-06-20
Scope: `ebpf/edac_trace.bpf.c`, `ebpf/workload_attr.bpf.c`, `ebpf/Makefile`

---

## Summary

Three files were reviewed covering two BPF programs and their build system. The
programs are structurally sound and use CO-RE patterns correctly. However, six
issues were identified ranging from a build-breaking license mismatch (BLOCKER)
through a systematic sampling bias (MAJOR) to several minor correctness and
robustness concerns. No unbounded loops or obviously unbounded pointer
dereferences were found, but the `bpf_probe_read_str` call on a derived pointer
(line 120 of `edac_trace.bpf.c`) needs closer attention because the verifier may
reject it on older kernels that cannot prove the intermediate pointer is safe.

---

## Findings

### [BLOCKER] Makefile license is Apache-2.0 while BPF objects declare GPL

File: `ebpf/Makefile` Line: 1
File: `ebpf/edac_trace.bpf.c` Line: 175
File: `ebpf/workload_attr.bpf.c` Line: 96

Issue: The Makefile carries `SPDX-License-Identifier: Apache-2.0` while both BPF
programs embed `char LICENSE[] SEC("license") = "GPL"`. This is a build-artifact
licensing contradiction. More critically, `bpf_ktime_get_ns()`, `bpf_ringbuf_*`,
and `__sync_fetch_and_add` on BPF map values are all GPL-only kernel helpers.
The kernel loader checks the embedded `LICENSE` section against each helper's
`gpl_only` flag at load time. If the license section ever gets changed to match
the Makefile's Apache-2.0 declaration, every `bpf_ringbuf_reserve` and
`bpf_ktime_get_ns` call will cause a `EPERM` rejection at `bpf(BPF_PROG_LOAD)`.
The Makefile also controls CI pipelines that scan SPDX headers; a mismatch here
will surface as a compliance failure.

Fix: Change the Makefile first line to `# SPDX-License-Identifier: GPL-2.0` (or
`GPL-2.0-only`) so all three files are consistent. The BPF object license
sections are correct and must not be changed.

---

### [BLOCKER] `bpf_probe_read_str` called with a pointer obtained from `BPF_CORE_READ` — verifier may reject on kernels < 5.17

File: `ebpf/edac_trace.bpf.c` Lines: 119-120, 138-141, 145

Issue: The pattern used is:

```c
bpf_probe_read_str(evt->error_type, sizeof(evt->error_type),
                   BPF_CORE_READ(ctx, msg));
```

`BPF_CORE_READ(ctx, msg)` expands to a `bpf_core_read`-aware dereference that
yields a `char *` read from kernel memory. The resulting pointer is an untrusted
scalar as far as the verifier is concerned. On kernels before 5.17 (where
`PTR_TO_MEM` tracking for helper arguments was improved), passing such a pointer
as the third argument of `bpf_probe_read_str` can cause the verifier to reject
the program with `R2 type=scalar expected=fp, pkt, pkt_end, map_value,
mem, ...`. The comment header claims Linux 5.8+ is supported; that claim is
broken by this pattern.

Additionally, `aer_event` reads `dev_name` and `error_type` with the same idiom
(lines 138-141), and `severity` on line 145 is also read this way.

Fix: Replace `BPF_CORE_READ(ctx, msg)` with an explicit intermediate variable
and use `bpf_probe_read_kernel` for the pointer step:

```c
const char *msg_ptr;
bpf_core_read(&msg_ptr, sizeof(msg_ptr), &ctx->msg);
bpf_probe_read_kernel_str(evt->error_type, sizeof(evt->error_type), msg_ptr);
```

Prefer `bpf_probe_read_kernel_str` over `bpf_probe_read_str` (the latter is the
legacy alias and deprecated since 5.10). Apply the same fix to all three string
reads in the `aer_event` handler.

---

### [MAJOR] Sampling by `pid % SAMPLE_RATE` introduces systematic bias

File: `ebpf/workload_attr.bpf.c` Lines: 56-59

Issue: The current sampler is:

```c
__u32 pid = bpf_get_current_pid_tgid() >> 32;
if (pid % SAMPLE_RATE != 0)
    return 0;
```

PIDs are assigned monotonically by the kernel's PID allocator. Only PIDs that are
exact multiples of 100 (0, 100, 200, …) are ever sampled. This has two
consequences:

1. Short-lived kernel threads and daemons that happen to get odd-multiple PIDs are
   completely invisible to the correlator regardless of how much memory they
   access.
2. Long-running processes with PID % 100 != 0 — which is 99% of all processes —
   are permanently excluded. The aggregation map (`workload_access_count`) will
   therefore be dominated by whichever long-running process happened to get PID
   100, 200, etc. On a system where PID 200 is a daemon with low memory activity,
   the correlator will under-count dramatically.

This is not a true statistical sample; it is a structural filter based on PID
assignment order.

Fix: Replace the PID modulo with a per-invocation counter stored in a
`BPF_MAP_TYPE_PERCPU_ARRAY` (avoids atomic contention). A simpler approach that
requires no extra map is to use the lower bits of `bpf_ktime_get_ns()`, which are
quasi-random at the nanosecond scale:

```c
__u64 ts = bpf_ktime_get_ns();
if ((ts >> 0) % SAMPLE_RATE != 0)   // lower bits vary every call
    return 0;
```

The most robust approach is a per-CPU call counter in a `PERCPU_ARRAY`:

```c
struct { __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
         __uint(max_entries, 1); __type(key, __u32); __type(value, __u64); }
    sample_ctr SEC(".maps");

__u32 idx = 0;
__u64 *ctr = bpf_map_lookup_elem(&sample_ctr, &idx);
if (!ctr) return 0;
(*ctr)++;
if (*ctr % SAMPLE_RATE != 0) return 0;
```

This samples exactly 1-in-N invocations without any PID bias.

---

### [MAJOR] Ring buffer (256 KB) undersized for CE storm bursts

File: `ebpf/edac_trace.bpf.c` Line: 63

Issue: The comment in `workload_attr.bpf.c` estimates 100 events/sec at 1-in-100
sampling. For `edac_trace.bpf.c` there is no sampling at all — every CE event is
forwarded. A correctable error storm on a Graviton3 instance (e.g., during a DRAM
refresh calibration sweep or a high-temperature throttle event) can produce
hundreds to low-thousands of `ras:mc_event` entries per second. Each
`mc_event_t` is 56 bytes. At 1000 events/sec the ring buffer fills in
approximately 4.5 seconds if the userspace reader stalls for any reason (GC
pause, context switch, slow Prometheus scrape). When the ring buffer is full,
`bpf_ringbuf_reserve` returns NULL and events are silently dropped; the counters
map continues to increment but the ring buffer consumer sees gaps.

The 128 KB `workload_events` buffer is less critical because sampling reduces the
rate, but a sampling rate of 1-in-100 at 500k page faults/sec (not unusual under
heavy memory pressure) still yields 5000 events/sec × 48 bytes = 240 KB/sec,
which overflows a 128 KB buffer in under a second.

Fix:
- Increase `edac_trace` ring buffer to at least 1 MB (`1024 * 1024`), ideally
  4 MB for CE storm headroom.
- Increase `workload_events` to 512 KB (`512 * 1024`) or add an adaptive sampling
  floor (e.g., increase SAMPLE_RATE dynamically via a global variable when drop
  rate exceeds a threshold).
- Add a drop counter map so userspace can detect and alert on buffer saturation
  rather than silently losing events.

---

### [MAJOR] `ras:aer_event` field name `severity` does not exist in kernel BTF — should be `error_severity`

File: `ebpf/edac_trace.bpf.c` Lines: 145

Issue: The program reads:

```c
bpf_probe_read_str(sev, sizeof(sev), BPF_CORE_READ(ctx, severity));
```

In the actual kernel BTF for `trace_event_raw_ras_aer_event` (defined in
`include/trace/events/ras.h`, present since 4.11 and unchanged through 6.6), the
field containing the severity string is named `error_severity`, not `severity`.
Because CO-RE is in use, this will cause a relocations-not-found failure at BPF
load time: the `bpf_core_field_offset` relocation for `severity` will not match
any BTF record and the loader will reject the program with `libbpf: field
[severity]: no matching BTF field found`. This is a load-time blocker on any real
kernel.

The `ras:mc_event` and `ras:mce_record` struct accesses appear to match their
respective BTF definitions (`mc_index`, `top_layer`, `mid_layer`, `lower_layer`,
`address`, `err_type` for mc_event; `status`, `addr` for mce_record) and are not
flagged here.

Fix: Change line 145 to use `error_severity`:

```c
bpf_probe_read_str(sev, sizeof(sev), BPF_CORE_READ(ctx, error_severity));
```

And update the struct typedef comment on line 44 of `aer_event_t` accordingly.
Verify all field names against `bpftool btf dump file /sys/kernel/btf/vmlinux`
on the target kernel before shipping.

---

### [MINOR] `fault_type` flag mask is incorrect for `handle_mm_fault` flags

File: `ebpf/workload_attr.bpf.c` Line: 89

Issue:

```c
evt->fault_type = (flags & 0x1) ? 1 : 0;
```

The `flags` argument to `handle_mm_fault` uses `FAULT_FLAG_*` constants defined
in `include/linux/mm_types.h`. `FAULT_FLAG_WRITE` is bit 0 (0x1), not the
minor/major distinction. The major/minor distinction comes from the return value
of `handle_mm_fault`, not its input flags. There is no input flag that directly
encodes minor vs. major. Bit 0 being set means the fault is a write fault, not a
major page fault.

Fix: Either rename `fault_type` to `fault_is_write` and document accordingly, or
hook `mm_page_fault` or use a fentry/fexit on `handle_mm_fault` to capture the
return value (`VM_FAULT_MAJOR` bit) if the minor/major distinction is genuinely
needed.

---

### [MINOR] `increment_counter` race on initial insert — lost update under high concurrency

File: `ebpf/edac_trace.bpf.c` Lines: 81-90
File: `ebpf/workload_attr.bpf.c` Lines: 72-78

Issue: Both files use the same pattern:

```c
__u64 *val = bpf_map_lookup_elem(&counters, &k);
if (val) {
    __sync_fetch_and_add(val, 1);
} else {
    __u64 one = 1;
    bpf_map_update_elem(&counters, &k, &one, BPF_NOEXIST);
}
```

There is a TOCTOU window between the failed lookup and the `BPF_NOEXIST` insert.
If two CPUs simultaneously see a missing key, both attempt `BPF_NOEXIST` inserts;
one succeeds and one silently fails (returns -EEXIST which is unchecked), dropping
the second increment. The net counter after two concurrent first-hits is 1, not 2.
On a multi-core ARM64 machine with simultaneous EDAC events, this loss is
non-zero, which is a correctness concern for a fault counter.

Fix: Use `BPF_MAP_TYPE_PERCPU_HASH` for the counters map. Per-CPU maps eliminate
the race entirely by giving each CPU its own counter slot. Userspace sums across
CPUs when reading. Alternatively, pre-populate all expected counter keys at
program load time from Go, so `bpf_map_lookup_elem` always succeeds and
`__sync_fetch_and_add` is the only operation needed.

---

### [MINOR] `verify` target swallows errors with `|| true`

File: `ebpf/Makefile` Line: 43

Issue:

```sh
bpftool prog load $$obj /sys/fs/bpf/test_$$obj 2>&1 || true;
```

The `|| true` means a verifier rejection (non-zero exit) is silently ignored. CI
will report `verify` as passing even when the program fails to load. This defeats
the purpose of the target.

Fix: Remove `|| true`. If the intent is to continue checking remaining objects
even after one fails, collect exit codes and report at the end:

```makefile
verify: $(BPF_OBJS)
	@rc=0; for obj in $(BPF_OBJS); do \
	    echo "Verifying $$obj..."; \
	    bpftool prog load $$obj /sys/fs/bpf/test_$$obj 2>&1; \
	    r=$$?; rm -f /sys/fs/bpf/test_$$obj; [ $$r -eq 0 ] || rc=$$r; \
	done; exit $$rc
```

---

### [MINOR] NUMA node derived from CPU ID using a hardcoded divisor

File: `ebpf/workload_attr.bpf.c` Lines: 63-66

Issue:

```c
__u32 cpu       = bpf_get_smp_processor_id();
__u32 numa_node = cpu / 8;   // assume 8 CPUs per NUMA node (adjust per platform)
```

The comment acknowledges this is an approximation, but the value 8 is wrong for
Graviton3. AWS Graviton3 instances present a single NUMA node across all vCPUs
(at least up to 64 vCPU instance sizes); `cpu / 8` will produce incorrect NUMA
node IDs (0–7 for a 64-vCPU instance) that do not correspond to real NUMA nodes.
This makes the `numa_node` field in emitted events meaningless and corrupts the
`workload_access_count` aggregation key.

Fix: As the comment mentions, pre-load a `cpu_to_numa` `BPF_MAP_TYPE_ARRAY`
from userspace using `/sys/devices/system/cpu/cpu<N>/node<M>` or
`/sys/bus/node/devices/node<N>/cpumap`. The BPF program looks up `cpu` in this
array to get the real NUMA node. A reasonable default when the map is empty is 0
(single-node system).

---

### [NIT] `bpf_probe_read_str` deprecated alias used throughout

File: `ebpf/edac_trace.bpf.c` Lines: 119, 138, 140, 145

Issue: `bpf_probe_read_str` is a legacy alias for `bpf_probe_read_kernel_str`.
Since Linux 5.10 the preferred name is `bpf_probe_read_kernel_str`, which makes
the memory-safety intent explicit (kernel vs. user memory). Using the legacy alias
generates warnings with newer libbpf and clang.

Fix: Replace all `bpf_probe_read_str` calls with `bpf_probe_read_kernel_str`.

---

### [NIT] Makefile `clean` does not remove the `verify` pin directory entries

File: `ebpf/Makefile` Line: 47

Issue: If `make verify` is interrupted after `bpftool prog load` succeeds but
before `rm -f /sys/fs/bpf/test_$$obj` runs, pinned objects are left in the BPF
filesystem. `make clean` does not remove them.

Fix: Add `rm -f /sys/fs/bpf/test_*.bpf.o` to the `clean` target.

---

### [NIT] `generate` target does not declare which Go files it produces

File: `ebpf/Makefile` Lines: 34-37

Issue: The `generate` target runs `go generate ./collectors/` but neither checks
whether the `bpf2go` tool is installed nor declares the generated output files.
If `bpf2go` is absent, the step silently produces no output and the build appears
to succeed.

Fix: Add a guard at the top of the Makefile:

```makefile
$(if $(shell command -v bpf2go 2>/dev/null),,$(error bpf2go not found; run: go install github.com/cilium/ebpf/cmd/bpf2go@latest))
```

---

## Metrics

- Files reviewed: 3
- Blockers: 3 (license mismatch, `bpf_probe_read_str` on CO-RE pointer, `aer_event.error_severity` wrong field name)
- Majors: 3 (sampling bias, ring buffer undersizing, `fault_type` flag misinterpretation)
- Minors: 3 (counter TOCTOU race, `verify` swallows errors, hardcoded CPU-to-NUMA divisor)
- Nits: 3 (deprecated helper alias, stale BPF pins not cleaned, missing `bpf2go` guard)
