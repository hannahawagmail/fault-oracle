// SPDX-License-Identifier: Apache-2.0
/*
 * kernel-hardening/ebpf/edac_trace_user.c
 *
 * Userspace reader for edac_trace.bpf.o. Loads the eBPF object, attaches
 * the kprobe, and prints EDAC events to stdout as they arrive.
 *
 * Build:
 *   clang -O2 edac_trace_user.c -o edac_trace_user \
 *         -lbpf -lelf -lz
 *
 * Run (root required):
 *   sudo ./edac_trace_user [--duration-sec 30] [--verbose]
 *
 * Or load the eBPF object manually and read the ring buffer:
 *   sudo bpftool prog load edac_trace.bpf.o /sys/fs/bpf/edac_trace
 *   sudo bpftool map dump name edac_events
 *
 * Requirements:
 *   libbpf >= 0.8   (apt-get install libbpf-dev)
 *   linux-headers   (for kprobe support)
 *   kernel >= 5.8   (BPF_MAP_TYPE_RINGBUF)
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <unistd.h>
#include <time.h>
#include <errno.h>
#include <bpf/libbpf.h>
#include <bpf/bpf.h>

/* Must match struct edac_event in edac_trace.bpf.c */
struct edac_event {
	unsigned long long timestamp_ns;
	unsigned int mc_idx;
	unsigned int error_count;
	unsigned int error_type;   /* 0 = CE, 1 = UE */
	int top_layer;
	int mid_layer;
	int low_layer;
};

static volatile int running = 1;
static unsigned long long total_ce = 0, total_ue = 0;

static void sig_handler(int sig) {
	(void)sig;
	running = 0;
}

static int handle_event(void *ctx, void *data, size_t size) {
	(void)ctx;
	if (size < sizeof(struct edac_event))
		return 0;

	const struct edac_event *ev = data;
	const char *type_str = (ev->error_type == 0) ? "CE" : "UE";
	double ts_s = (double)ev->timestamp_ns / 1e9;

	printf("[%14.6f] EDAC mc%u %s count=%u csrow=%d ch=%d chip=%d\n",
	       ts_s, ev->mc_idx, type_str, ev->error_count,
	       ev->top_layer, ev->mid_layer, ev->low_layer);

	if (ev->error_type == 0)
		total_ce += ev->error_count;
	else
		total_ue += ev->error_count;

	return 0;
}

int main(int argc, char **argv) {
	int duration_sec = 0;  /* 0 = run until Ctrl-C */
	int verbose = 0;

	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--duration-sec") && i + 1 < argc)
			duration_sec = atoi(argv[++i]);
		else if (!strcmp(argv[i], "--verbose"))
			verbose = 1;
		else if (!strcmp(argv[i], "--help")) {
			printf("Usage: edac_trace_user [--duration-sec N] [--verbose]\n");
			return 0;
		}
	}

	printf("edac_trace_user: loading edac_trace.bpf.o ...\n");

	struct bpf_object *obj = bpf_object__open("edac_trace.bpf.o");
	if (libbpf_get_error(obj)) {
		fprintf(stderr, "Error: cannot open edac_trace.bpf.o: %s\n",
		        strerror(errno));
		fprintf(stderr, "Build with: clang -O2 -g -target bpf "
		        "-c edac_trace.bpf.c -o edac_trace.bpf.o\n");
		return 1;
	}

	if (bpf_object__load(obj)) {
		fprintf(stderr, "Error: cannot load eBPF object: %s\n", strerror(errno));
		bpf_object__close(obj);
		return 1;
	}

	/* Find and attach the kprobe program */
	struct bpf_program *prog = bpf_object__find_program_by_name(
		obj, "trace_edac_mc_handle_error");
	if (!prog) {
		fprintf(stderr, "Error: program trace_edac_mc_handle_error not found\n");
		bpf_object__close(obj);
		return 1;
	}

	struct bpf_link *link = bpf_program__attach(prog);
	if (libbpf_get_error(link)) {
		fprintf(stderr, "Error: cannot attach kprobe: %s\n"
		        "Is edac_mc_handle_error present in the kernel?\n"
		        "Check: grep edac_mc_handle_error /proc/kallsyms\n",
		        strerror(errno));
		bpf_object__close(obj);
		return 1;
	}

	/* Find the ring buffer map */
	struct bpf_map *rb_map = bpf_object__find_map_by_name(obj, "edac_events");
	if (!rb_map) {
		fprintf(stderr, "Error: ring buffer map 'edac_events' not found\n");
		bpf_link__destroy(link);
		bpf_object__close(obj);
		return 1;
	}

	struct ring_buffer *rb = ring_buffer__new(
		bpf_map__fd(rb_map), handle_event, NULL, NULL);
	if (!rb) {
		fprintf(stderr, "Error: cannot create ring buffer consumer\n");
		bpf_link__destroy(link);
		bpf_object__close(obj);
		return 1;
	}

	signal(SIGINT, sig_handler);
	signal(SIGTERM, sig_handler);

	printf("Tracing EDAC errors (kprobe:edac_mc_handle_error). "
	       "Press Ctrl-C to stop.\n");
	if (verbose)
		printf("  timestamp(s)      mc   type  count  csrow  ch  chip\n");

	time_t start = time(NULL);
	while (running) {
		int err = ring_buffer__poll(rb, 100 /* ms timeout */);
		if (err < 0 && errno != EINTR) {
			fprintf(stderr, "Error: ring_buffer__poll: %s\n", strerror(errno));
			break;
		}
		if (duration_sec > 0 && (time(NULL) - start) >= duration_sec)
			break;
	}

	printf("\n--- Summary ---\n");
	printf("Total CE events: %llu\n", total_ce);
	printf("Total UE events: %llu\n", total_ue);

	ring_buffer__free(rb);
	bpf_link__destroy(link);
	bpf_object__close(obj);
	return 0;
}
