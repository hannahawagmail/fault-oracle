# SPDX-License-Identifier: Apache-2.0
# kernel-hardening/ebpf/Makefile — Build eBPF programs for EDAC tracing.

ARCH       ?= $(shell uname -m | sed 's/x86_64/x86/' | sed 's/aarch64/arm64/')
CLANG      ?= clang
LIBBPF     ?= $(shell pkg-config --libs libbpf 2>/dev/null || echo "-lbpf")
LIBBPF_INC ?= $(shell pkg-config --cflags libbpf 2>/dev/null)
KERNEL_INC ?= /usr/include/$(shell uname -m)-linux-gnu

BPF_CFLAGS := -O2 -g -target bpf \
              -D__TARGET_ARCH_$(ARCH) \
              -I$(KERNEL_INC) \
              $(LIBBPF_INC)

USER_CFLAGS := -O2 -Wall

.PHONY: all clean help

all: edac_trace.bpf.o edac_trace_user  ## Build eBPF object and userspace reader

edac_trace.bpf.o: edac_trace.bpf.c
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@
	@echo "Built: $@ (load with: sudo bpftool prog load $@ /sys/fs/bpf/edac_trace)"

edac_trace_user: edac_trace_user.c
	$(CC) $(USER_CFLAGS) $< -o $@ $(LIBBPF) -lelf -lz
	@echo "Built: $@ (run with: sudo ./edac_trace_user)"

clean:  ## Remove build artifacts
	rm -f edac_trace.bpf.o edac_trace_user

help:  ## Show this help
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ { printf "  %-20s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
