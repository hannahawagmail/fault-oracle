# Authors and Upstream Contributions

## Hanna Hawa

Principal Software Engineer — ARM embedded systems, firmware, and Linux kernel.
~13 years of experience spanning bare-metal bring-up, U-Boot, Linux kernel drivers,
and datacenter-scale ARM Linux deployments.

### Mainline Linux Kernel Contributions

#### Marvell ARMADA pinctrl drivers

Two pinctrl drivers merged into the mainline Linux kernel for the Marvell ARMADA
system-on-chip family, used in network and storage appliances:

- **`drivers/pinctrl/mvebu/pinctrl-armada-ap806.c`**
  Pin control driver for the ARMADA AP806 Application Processor.
  Merged into mainline; visible in the kernel source tree at that path.

- **`drivers/pinctrl/mvebu/pinctrl-armada-cp110.c`**
  Pin control driver for the ARMADA CP110 Control Processor.
  Merged into mainline alongside the AP806 driver.

These drivers implement the standard Linux pinctrl framework interfaces
(`pinctrl_ops`, `pinmux_ops`, `pinconf_ops`) and handle GPIO multiplexing
for a production SoC used in deployed hardware.

#### Softirq regression diagnosis — commit 3c53776e

In January 2018, Hanna Hawa diagnosed a softirq accounting regression in the
mainline Linux kernel. Linus Torvalds credited the diagnosis directly in the
merge commit:

```
commit 3c53776e8b9c0d1d1e06e48ead40fc57efc4a47a
Author: Linus Torvalds <torvalds@linux-foundation.org>

    Merge branch 'akpm' of git://git.kernel.org/pub/scm/linux/kernel/git/akpm/mm

    ...
    Reported-and-tested-by: Hanna Hawa <hhhawa@gmail.com>
```

**Why this credential is relevant to this repository:**

The softirq regression caused interrupt-handling latency to silently degrade
under load — a category of "rare event that becomes routine at scale" failure.
Diagnosing it required reading kernel accounting paths, correlating `/proc`
counters with scheduler behavior, and bisecting across kernel versions to
identify the commit that introduced the regression. That methodology —
instrument, correlate, bisect, report — is the same methodology this
repository formalizes for hardware fault detection and recovery.

The softirq fix was itself a resilience contribution: it restored correct
interrupt processing guarantees that kernel subsystems depend on for
forward-progress safety.

### Contact

- GitHub: [hanna-hawa](https://github.com/hanna-hawa)
- Email: hhhawa@gmail.com
