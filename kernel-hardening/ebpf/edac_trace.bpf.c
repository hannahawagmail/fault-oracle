// SPDX-License-Identifier: GPL-2.0
/*
 * kernel-hardening/ebpf/edac_trace.bpf.c
 *
 * eBPF program: trace EDAC correctable and uncorrectable errors via a kprobe
 * on edac_mc_handle_error(). Records {mc_idx, type, count} to a ring buffer
 * map so userspace can display events in real time.
 *
 * Build with: clang -O2 -g -target bpf -D__TARGET_ARCH_arm64 \
 *               -I/usr/include/$(uname -m)-linux-gnu \
 *               -c edac_trace.bpf.c -o edac_trace.bpf.o
 *
 * Load with:  bpftool prog load edac_trace.bpf.o /sys/fs/bpf/edac_trace
 *         or: use the companion userspace reader edac_trace_user.c
 *
 * Kernel requirement: >= 5.8 (ring buffer map type BPF_MAP_TYPE_RINGBUF)
 *
 * Kprobe target: edac_mc_handle_error
 *   Prototype (linux/edac.h):
 *     void edac_mc_handle_error(
 *         const enum hw_event_mc_err_type type,  // HW_EVENT_ERR_CORRECTED = 0,
 *                                                //  HW_EVENT_ERR_UNCORRECTED = 1
 *         struct mem_ctl_info *mci,              // memory controller info
 *         const u16 error_count,
 *         const unsigned long page_frame_number,
 *         const unsigned long offset_in_page,
 *         const unsigned long syndrome,
 *         const int top_layer,
 *         const int mid_layer,
 *         const int low_layer,
 *         const char *msg,
 *         const char *other_detail
 *     );
 */

#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

/*
 * edac_event — data record written to the ring buffer for each EDAC event.
 * Kept small to minimise ring buffer pressure.
 */
struct edac_event {
	__u64 timestamp_ns;   /* bpf_ktime_get_ns() — monotonic nanoseconds */
	__u32 mc_idx;         /* memory controller index */
	__u32 error_count;    /* number of errors in this event */
	__u32 error_type;     /* 0 = CE, 1 = UE */
	__s32 top_layer;      /* csrow equivalent */
	__s32 mid_layer;      /* channel equivalent */
	__s32 low_layer;      /* chip-select layer */
};

/*
 * Ring buffer map: userspace reads events from here using
 * ring_buffer__consume() (libbpf) or bpf_map_lookup_elem().
 */
struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 256 * 1024);  /* 256 KB */
} edac_events SEC(".maps");

/*
 * Stats counters — cumulative totals for CE and UE events seen by this probe.
 */
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 2);           /* index 0 = CE, index 1 = UE */
	__type(key, __u32);
	__type(value, __u64);
} edac_totals SEC(".maps");

/*
 * mem_ctl_info partial layout — only the fields we need.
 * CO-RE (Compile Once, Run Everywhere) handles kernel version differences.
 */
struct mem_ctl_info {
	unsigned int mc_idx;
} __attribute__((preserve_access_index));

/*
 * kprobe on edac_mc_handle_error — fires on every EDAC error report.
 *
 * Arguments follow the Linux calling convention for arm64 (AArch64):
 *   x0 = type (hw_event_mc_err_type enum)
 *   x1 = mci  (struct mem_ctl_info *)
 *   x2 = error_count
 *   x3 = page_frame_number
 *   x4 = offset_in_page
 *   x5 = syndrome
 *   x6 = top_layer
 *   x7 = mid_layer
 *   stack = low_layer, msg, other_detail
 */
SEC("kprobe/edac_mc_handle_error")
int BPF_KPROBE(trace_edac_mc_handle_error,
               int type,
               struct mem_ctl_info *mci,
               unsigned short error_count,
               unsigned long page_frame_number,
               unsigned long offset_in_page,
               unsigned long syndrome,
               int top_layer,
               int mid_layer,
               int low_layer)
{
	struct edac_event *ev;
	__u32 mc_idx = 0;
	__u32 type_key;
	__u64 *counter;

	/* Read mc_idx safely from kernel memory using CO-RE */
	mc_idx = BPF_CORE_READ(mci, mc_idx);

	/* Reserve space in the ring buffer */
	ev = bpf_ringbuf_reserve(&edac_events, sizeof(*ev), 0);
	if (!ev)
		return 0;  /* ring buffer full — drop event, don't crash */

	ev->timestamp_ns = bpf_ktime_get_ns();
	ev->mc_idx        = mc_idx;
	ev->error_count   = error_count;
	ev->error_type    = (type == 0) ? 0 : 1;  /* 0=CE, 1=UE */
	ev->top_layer     = top_layer;
	ev->mid_layer     = mid_layer;
	ev->low_layer     = low_layer;

	bpf_ringbuf_submit(ev, 0);

	/* Increment cumulative counter */
	type_key = ev->error_type;
	counter = bpf_map_lookup_elem(&edac_totals, &type_key);
	if (counter)
		__sync_fetch_and_add(counter, 1);

	return 0;
}

char LICENSE[] SEC("license") = "GPL";
