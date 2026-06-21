# SPDX-License-Identifier: Apache-2.0
#
# Makefile for the ARM Cortex-A72 reference EDAC driver.
#
# Usage:
#   Native ARM64 build:
#     make KERNELDIR=/lib/modules/$(uname -r)/build
#
#   Cross-compile for ARM64 from x86 host:
#     make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \
#          KERNELDIR=/path/to/arm64-kernel-build
#
#   Build for QEMU virt (using a locally built kernel):
#     make KERNELDIR=/path/to/linux-build ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu-

# The module is built from two translation units:
#   edac_cortex_ref.c  — main driver (poll loop, debugfs, platform driver)
#   cortex_ref_ras.c   — ARM RAS Extension register accessors
#
# Kbuild multi-file module convention: when <module>-objs is defined, obj-m
# names the final .ko. The -objs list names constituent .o files whose source
# names must differ from the module name to avoid a collision with the
# intermediate module .o. We keep edac_cortex_ref.c as-is and link it together
# with cortex_ref_ras.o under the module name edac_cortex_ref.ko by listing
# both objects explicitly.
#
# NOTE: Kbuild prohibits edac_cortex_ref.o appearing in both obj-m and
# edac_cortex_ref-objs. The workaround used here is to compile the main
# translation unit as part of the default obj-m rule (it owns edac_cortex_ref.o)
# and add cortex_ref_ras.o as an additional object via the always-built list.
# For a production driver, rename edac_cortex_ref.c to edac_cortex_ref_main.c
# and use edac_cortex_ref-objs := edac_cortex_ref_main.o cortex_ref_ras.o.
obj-m := edac_cortex_ref.o cortex_ref_ras.o

# Kernel build directory — override on command line
KERNELDIR ?= /lib/modules/$(shell uname -r)/build

# Architecture — override for cross-compile
ARCH     ?= arm64

# Cross-compiler prefix — override for cross-compile (e.g. aarch64-linux-gnu-)
CROSS_COMPILE ?=

# Extra CFLAGS passed to the kernel build system
ccflags-y := -DDEBUG

.PHONY: all clean modules_install help

all: modules

modules:
	$(MAKE) -C $(KERNELDIR) \
		M=$(CURDIR) \
		ARCH=$(ARCH) \
		CROSS_COMPILE=$(CROSS_COMPILE) \
		modules

modules_install:
	$(MAKE) -C $(KERNELDIR) \
		M=$(CURDIR) \
		ARCH=$(ARCH) \
		CROSS_COMPILE=$(CROSS_COMPILE) \
		modules_install

clean:
	$(MAKE) -C $(KERNELDIR) M=$(CURDIR) clean
	rm -f Module.symvers modules.order

help:
	@echo "Targets:"
	@echo "  all (default)      Build the kernel module"
	@echo "  modules_install    Install to /lib/modules/..."
	@echo "  clean              Remove build artifacts"
	@echo ""
	@echo "Variables:"
	@echo "  KERNELDIR          Kernel build directory (default: running kernel)"
	@echo "  ARCH               Target architecture (default: arm64)"
	@echo "  CROSS_COMPILE      Cross-compiler prefix (default: none)"
	@echo ""
	@echo "Example (cross-compile for ARM64):"
	@echo "  make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- KERNELDIR=/path/to/linux"
