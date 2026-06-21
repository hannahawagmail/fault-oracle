// SPDX-License-Identifier: GPL-2.0
// ebpf/workload_attr.bpf.c — Per-process CE attribution via page fault hooking.
//
// Hooks handle_mm_fault to capture which process is accessing memory at the
// time of a EDAC CE event. Correlates page fault activity on NUMA nodes with
// the EDAC correctable error counter to attribute faults to workloads.
//
// This does NOT directly read EDAC CE addresses (kernel only exposes the MC
// index, not physical address, in sysfs). Instead, it builds a per-(process,
// NUMA node) access density map, which correlates with MC CE distribution.
//
// Emits via ring buffer: {comm[16], pid, numa_node, timestamp_ns}
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

#define TASK_COMM_LEN 16

struct workload_event_t {
    char   comm[TASK_COMM_LEN];
    __u32  pid;
    __u32  tgid;
    __u32  numa_node;
    __u32  fault_type;    // 0=minor, 1=major
    __u64  timestamp_ns;
};

// Aggregation map: (comm, numa_node) → access count
struct agg_key {
    char comm[TASK_COMM_LEN];
    __u32 numa_node;
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1024);
    __type(key,   struct agg_key);
    __type(value, __u64);
} workload_access_count SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 128 * 1024);
} workload_events SEC(".maps");

// Sampling: only record 1-in-N faults to limit overhead.
// At 10k faults/sec, 1-in-100 = 100 events/sec → 1.6KB/s ring buffer usage.
#define SAMPLE_RATE 100

SEC("kprobe/handle_mm_fault")
int BPF_KPROBE(handle_mm_fault_entry, struct vm_area_struct *vma,
               unsigned long address, unsigned int flags,
               struct pt_regs *regs)
{
    // Sampling: use pid mod SAMPLE_RATE as a simple deterministic sampler
    __u32 pid = bpf_get_current_pid_tgid() >> 32;
    if (pid % SAMPLE_RATE != 0)
        return 0;

    // Get NUMA node for the faulting address
    // (simplified: use bpf_get_numa_node_id for current CPU as proxy)
    __u32 cpu       = bpf_get_smp_processor_id();
    // In a real deployment, cpu→numa mapping is pre-loaded into a BPF map
    // from /sys/devices/system/cpu/cpu<N>/node<M>. Here we use cpu as proxy.
    __u32 numa_node = cpu / 8;   // assume 8 CPUs per NUMA node (adjust per platform)

    // Update aggregation counter
    struct agg_key k = {};
    bpf_get_current_comm(&k.comm, sizeof(k.comm));
    k.numa_node = numa_node;
    __u64 *cnt = bpf_map_lookup_elem(&workload_access_count, &k);
    if (cnt) {
        __sync_fetch_and_add(cnt, 1);
    } else {
        __u64 one = 1;
        bpf_map_update_elem(&workload_access_count, &k, &one, BPF_NOEXIST);
    }

    // Emit sampled event to ring buffer
    struct workload_event_t *evt = bpf_ringbuf_reserve(&workload_events, sizeof(*evt), 0);
    if (!evt)
        return 0;

    bpf_get_current_comm(&evt->comm, sizeof(evt->comm));
    evt->pid          = pid;
    evt->tgid         = (__u32)(bpf_get_current_pid_tgid() & 0xffffffff);
    evt->numa_node    = numa_node;
    evt->fault_type   = (flags & 0x1) ? 1 : 0;
    evt->timestamp_ns = bpf_ktime_get_ns();

    bpf_ringbuf_submit(evt, 0);
    return 0;
}

char LICENSE[] SEC("license") = "GPL";
