# SPDX-License-Identifier: Apache-2.0
# selinux/Makefile — Build and install the hw_fault_exporter SELinux policy module.

MODULE  := hw_fault_exporter
VERSION := 1.0.0
PP      := $(MODULE).pp
MOD     := $(MODULE).mod

.PHONY: all build install uninstall check clean help

all: build

## build: Compile .te + .fc → .pp policy package
build: $(PP)

$(MOD): $(MODULE).te
	checkmodule -M -m -o $@ $<

$(PP): $(MOD) $(MODULE).fc
	semodule_package -o $@ -m $(MOD) -f $(MODULE).fc

## install: Load the compiled policy module into the running kernel
install: build
	semodule -i $(PP)
	restorecon -Rv /usr/local/bin/hw-fault-exporter \
	               /etc/hw-fault-exporter \
	               /var/log/hw-fault-exporter || true
	@echo "Policy module $(MODULE) v$(VERSION) installed"

## uninstall: Remove the policy module from the kernel
uninstall:
	semodule -r $(MODULE) || true

## check: Validate the .te file with checkmodule (no install)
check:
	checkmodule -M -m -o /dev/null $(MODULE).te && echo "Syntax OK"

## clean: Remove build artifacts
clean:
	rm -f $(MOD) $(PP)

## help: Show available targets
help:
	@grep -E '^## ' Makefile | sed 's/## /  /'
