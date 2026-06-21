// SPDX-License-Identifier: GPL-2.0
// ebpf/edac_trace.bpf.c — BPF program attaching to kernel RAS tracepoints.
//
// Hooks:
//   ras:mc_event    — EDAC memory controller correctable/uncorrectable events
//   ras:aer_event   — PCIe AER correctable/uncorrectable events
//   ras:mce_record  — Machine check exception records
//
// All events are pushed via BPF ring buffer to the Go userspace collector
// (exporter/collectors/ebpf_edac.go) which converts them to Prometheus counters.
//
// Requirements: Linux 5.8+ (ring buffer), BTF+CO-RE enabled kernel.
// ARM64 (Graviton3 = 6.1): supported.
// Compile: clang -O2 -g -target bpf -D__TARGET_ARCH_arm64 \
//            -I/usr/include/bpf -c edac_trace.bpf.c -o edac_trace.bpf.o
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

// ─── Event structures sent to userspace ──────────────────────────────────────

#define EVENT_TYPE_MC   1
#define EVENT_TYPE_AER  2
#define EVENT_TYPE_MCE  3

#define SEVERITY_CE     0
#define SEVERITY_UE     1
#define SEVERITY_FATAL  2

struct mc_event_t {
    __u32 type;          // EVENT_TYPE_MC
    __u32 severity;      // SEVERITY_CE or SEVERITY_UE
    __u32 mc;
    __u32 top_layer;
    __u32 mid_layer;
    __u32 lower_layer;
    __u64 address;
    __u64 timestamp_ns;
    char  error_type[16];
};

struct aer_event_t {
    __u32 type;          // EVENT_TYPE_AER
    __u32 severity;
    char  dev_name[16];
    char  error_type[32];
    __u64 timestamp_ns;
};

struct mce_event_t {
    __u32 type;          // EVENT_TYPE_MCE
    __u32 severity;
    __u64 status;
    __u64 addr;
    __u64 timestamp_ns;
};

// ─── Ring buffer map ─────────────────────────────────────────────────────────

struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 256 * 1024);   // 256 KB ring buffer
} events SEC(".maps");

// ─── Counters map (kernel-side aggregation, optional) ────────────────────────

struct counter_key {
    __u32 type;
    __u32 severity;
    __u32 mc;
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 256);
    __type(key,   struct counter_key);
    __type(value, __u64);
} counters SEC(".maps");

static __always_inline void increment_counter(__u32 type, __u32 severity, __u32 mc) {
    struct counter_key k = {.type = type, .severity = severity, .mc = mc};
    __u64 *val = bpf_map_lookup_elem(&counters, &k);
    if (val) {
        __sync_fetch_and_add(val, 1);
    } else {
        __u64 one = 1;
        bpf_map_update_elem(&counters, &k, &one, BPF_NOEXIST);
    }
}

// ─── ras:mc_event tracepoint ─────────────────────────────────────────────────
//
// Kernel signature (include/trace/events/ras.h):
//   TP_PROTO(unsigned int err_type, const char *msg, const char *label,
//            long grain, unsigned long syndrome, unsigned int mc,
//            unsigned int top_layer, unsigned int mid_layer,
//            unsigned int lower_layer, unsigned long long address,
//            u8 type)

SEC("tracepoint/ras/mc_event")
int trace_mc_event(struct trace_event_raw_ras_mc_event *ctx) {
    struct mc_event_t *evt = bpf_ringbuf_reserve(&events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    evt->type       = EVENT_TYPE_MC;
    evt->mc         = BPF_CORE_READ(ctx, mc_index);
    evt->top_layer  = BPF_CORE_READ(ctx, top_layer);
    evt->mid_layer  = BPF_CORE_READ(ctx, mid_layer);
    evt->lower_layer = BPF_CORE_READ(ctx, lower_layer);
    evt->address    = BPF_CORE_READ(ctx, address);
    evt->timestamp_ns = bpf_ktime_get_ns();

    // err_type: 1=CE, 2=UE
    __u32 err_type = BPF_CORE_READ(ctx, err_type);
    evt->severity = (err_type == 2) ? SEVERITY_UE : SEVERITY_CE;

    bpf_probe_read_str(evt->error_type, sizeof(evt->error_type),
                       BPF_CORE_READ(ctx, msg));

    increment_counter(EVENT_TYPE_MC, evt->severity, evt->mc);
    bpf_ringbuf_submit(evt, 0);
    return 0;
}

// ─── ras:aer_event tracepoint ─────────────────────────────────────────────────

SEC("tracepoint/ras/aer_event")
int trace_aer_event(struct trace_event_raw_ras_aer_event *ctx) {
    struct aer_event_t *evt = bpf_ringbuf_reserve(&events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    evt->type         = EVENT_TYPE_AER;
    evt->timestamp_ns = bpf_ktime_get_ns();

    bpf_probe_read_str(evt->dev_name,  sizeof(evt->dev_name),
                       BPF_CORE_READ(ctx, dev_name));
    bpf_probe_read_str(evt->error_type, sizeof(evt->error_type),
                       BPF_CORE_READ(ctx, error_type));

    // severity string: "Correctable" / "Fatal" / "Uncorrected"
    char sev[16];
    bpf_probe_read_str(sev, sizeof(sev), BPF_CORE_READ(ctx, error_severity));
    if (sev[0] == 'C')       evt->severity = SEVERITY_CE;
    else if (sev[0] == 'F')  evt->severity = SEVERITY_FATAL;
    else                     evt->severity = SEVERITY_UE;

    increment_counter(EVENT_TYPE_AER, evt->severity, 0);
    bpf_ringbuf_submit(evt, 0);
    return 0;
}

// ─── ras:mce_record tracepoint ───────────────────────────────────────────────

SEC("tracepoint/ras/mce_record")
int trace_mce_record(struct trace_event_raw_mce_record *ctx) {
    struct mce_event_t *evt = bpf_ringbuf_reserve(&events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    evt->type         = EVENT_TYPE_MCE;
    evt->status       = BPF_CORE_READ(ctx, status);
    evt->addr         = BPF_CORE_READ(ctx, addr);
    evt->timestamp_ns = bpf_ktime_get_ns();
    // Severity derived from status MSB in userspace (too complex for BPF)
    evt->severity     = SEVERITY_UE;

    increment_counter(EVENT_TYPE_MCE, SEVERITY_UE, 0);
    bpf_ringbuf_submit(evt, 0);
    return 0;
}

char LICENSE[] SEC("license") = "GPL";
