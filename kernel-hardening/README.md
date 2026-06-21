# kernel-hardening — ARM Linux Fault Resilience Module

This module configures the Linux kernel for maximum resilience on production ARM embedded
and server systems. It covers three pillars: **panic policy**, **OOM killer tuning**, and
**eBPF-based anomaly detection**. Together they ensure that a faulting system either
recovers automatically or generates enough signal to diagnose the root cause.

---

## 1. Kernel Panic Configuration

### 1.1 Default behavior — why it is wrong for production

By default, a Linux kernel panic causes the system to print a stack trace and then **hang
forever**. On a server or embedded device with no keyboard attached, a hung system is
indistinguishable from a healthy one until a human notices the service is down. On a
headless ARM board this can mean hours of undetected downtime.

The parameters below change that behavior so that a panicking system either reboots
automatically or, if kdump is configured, captures a crash dump first and then reboots.

---

### 1.2 Key sysctl parameters

#### `kernel.panic = N` — reboot after N seconds

| Value | Meaning |
|-------|---------|
| `0`   | Halt forever (Linux default) |
| `-1`  | Reboot immediately, no delay |
| `10`  | Wait 10 s, then reboot — recommended for production |

A 10-second delay gives the system time to flush NAND/eMMC write caches and, if kdump is
configured, to capture a crash dump before rebooting. Do not set this below 5 on systems
running kdump.

**Where to set it:**

*Runtime (takes effect immediately, lost on reboot):*
```
sysctl -w kernel.panic=10
```

*Persistent via sysctl.d (survives reboot):*
```
# /etc/sysctl.d/99-fault-resilience.conf
kernel.panic = 10
```
Apply without rebooting:
```
sysctl --system
```

*In U-Boot bootargs (takes effect before userspace, overrides sysctl.d if the kernel
 parses it first — only useful for very early panics):*
```
setenv bootargs "${bootargs} panic=10"
saveenv
```
Note: the kernel command-line `panic=` value is read at boot and used to initialize
`kernel.panic`. A later `sysctl -w` call overrides it, so `/etc/sysctl.d/` wins.

---

#### `kernel.panic_on_oops = 1` — treat oops as panic

An **oops** is a non-fatal kernel error — the kernel detected something wrong but did not
consider it severe enough to halt. On a desktop this is tolerable; on a production
embedded system, an oops often means a driver is corrupting memory, and continuing to run
will make things worse.

Setting `panic_on_oops=1` promotes any oops to a full panic, which triggers the reboot
timer set by `kernel.panic`.

```
kernel.panic_on_oops = 1
```

**Trade-off:** On development boards you may want `panic_on_oops=0` so you can collect
the oops backtrace over serial without the board rebooting. Enable it for production
images only.

---

#### `kernel.panic_on_warn = 0` vs `1`

`WARN_ON()` is a kernel macro that prints a backtrace but keeps running. It is intended
for "this should not happen but is not immediately fatal" conditions.

| Value | Behavior | Recommended for |
|-------|----------|-----------------|
| `0`   | Log warning, continue | Production (stable kernels) |
| `1`   | Convert warning to panic → reboot | Kernel development, CI |

For most production ARM systems, leave this at `0`. Upstream kernels contain a non-trivial
number of `WARN_ON` paths that trigger under normal load, and setting `panic_on_warn=1`
can cause spurious reboots. Enable it only on staging systems to catch regressions.

---

#### `kernel.softlockup_panic = 1` — watchdog: soft lockup

The kernel's soft-lockup detector fires when a CPU task holds the CPU without sleeping for
longer than `kernel.watchdog_thresh` seconds (default 10 s). This usually indicates a
tight spin loop, a deadlock, or a misbehaving driver.

```
kernel.softlockup_panic = 1
```

Without this, the soft-lockup detector prints a backtrace but the system stays alive —
often in a degraded, unresponsive state. With it, a soft lockup triggers a panic and the
automatic reboot.

---

#### `kernel.hardlockup_panic = 1` — watchdog: hard lockup

A **hard lockup** means a CPU has not responded to any interrupt for several seconds.
This is more severe than a soft lockup — it usually means the CPU is stuck in a tight
loop with interrupts disabled, a hardware fault has stalled the CPU, or an NMI watchdog
is not firing. On ARM systems this requires a hardware PMU or an always-on timer.

```
kernel.hardlockup_panic = 1
```

---

#### Setting these permanently in sysctl.d

All parameters above are collected in `sysctl-hardening.conf` in this directory. Deploy:

```bash
cp sysctl-hardening.conf /etc/sysctl.d/99-fault-resilience.conf
sysctl --system
```

Or use `panic-config.sh --apply` which does this automatically and verifies the values.

#### Setting them in U-Boot bootargs

```
setenv bootargs "${bootargs} panic=10 panic_on_oops=1 oops=panic"
saveenv
```

The `oops=panic` kernel command-line option is an older alias for `panic_on_oops`; setting
both is harmless and ensures compatibility across kernel versions.

---

## 2. OOM Killer Configuration

### 2.1 How the Linux OOM killer works

When the kernel cannot satisfy a memory allocation request and all reclaim paths have been
exhausted, it invokes the **Out-Of-Memory (OOM) killer**. The OOM killer scans every
process, assigns each one an **oom_score** (0–2000), and kills the process with the
highest score. The score is roughly:

```
oom_score ≈ (process RSS + swap) / total_memory * 1000
           + (age penalty)
           + oom_score_adj
```

A process using 50% of RAM will have an oom_score around 500. Threads are counted toward
their parent's score. The kernel never kills init (PID 1).

After a victim is chosen, `SIGKILL` is sent, memory is freed, and the allocation that
triggered the OOM is retried. If the victim does not die quickly (e.g., it is stuck in
`D` state), the OOM killer may kill additional processes.

---

### 2.2 oom_score_adj — adjusting the score

`/proc/<pid>/oom_score_adj` accepts values from **-1000** to **+1000**:

| Value  | Meaning |
|--------|---------|
| `-1000` | Never kill this process (exempted) |
| `-900`  | Very unlikely to be killed |
| `0`     | No adjustment (default) |
| `+500`  | More likely to be killed — good for disposable workers |
| `+1000` | Kill this first |

Write the value directly:
```bash
echo -900 > /proc/$(pgrep sshd)/oom_score_adj
```

This is **not persistent** across process restarts. For persistence use systemd.

---

### 2.3 Which processes to protect

Set `oom_score_adj = -900` (not -1000, to preserve the kernel's ability to kill in
absolute extremes) for:

| Service | Why |
|---------|-----|
| `sshd` | Losing SSH access means losing remote management entirely |
| `NetworkManager` | Network loss breaks monitoring and remote access |
| `systemd-journald` | Losing the journal loses the diagnostic trail |
| `hw-fault-exporter` | Your custom metrics exporter — critical for observability |
| `containerd` / `dockerd` | Container runtime loss cascades to all containers |

---

### 2.4 Which processes to deprioritize

Set `oom_score_adj = +500` for:

- Worker processes handling user-submitted jobs
- Compilation or CI build processes
- Temporary data-processing pipelines
- Any process that can be restarted without data loss

---

### 2.5 Persistent configuration via systemd

Add an override drop-in for a service:
```bash
mkdir -p /etc/systemd/system/sshd.service.d/
cat > /etc/systemd/system/sshd.service.d/oom.conf <<EOF
[Service]
OOMScoreAdjust=-900
EOF
systemctl daemon-reload
```

`oom-tuning.sh --protect sshd` does this automatically.

---

### 2.6 cgroup v2 memory limits — a better alternative

Rather than adjusting OOM scores after the fact, **preventing processes from consuming
excessive memory in the first place** is more robust. With cgroup v2:

```bash
# Set a hard 512 MB limit on a service
mkdir -p /etc/systemd/system/my-worker.service.d/
cat > /etc/systemd/system/my-worker.service.d/memory.conf <<EOF
[Service]
MemoryMax=512M
MemorySwapMax=0
EOF
systemctl daemon-reload && systemctl restart my-worker
```

When the cgroup limit is hit, the OOM killer only considers processes **within that
cgroup**, protecting the rest of the system. This is far preferable to relying on global
oom_score_adj tuning.

`oom-tuning.sh --cgroup-limit <service> <size_mb>` writes this configuration for you.

---

### 2.7 /proc/meminfo fields to monitor

| Field | Meaning |
|-------|---------|
| `MemTotal` | Physical RAM installed |
| `MemAvailable` | Estimated reclaimable memory; drop below 100 MB is a warning |
| `SwapTotal` | Swap capacity (on flash: keep swap small or use zram) |
| `Committed_AS` | Total virtual memory promised across all processes; if > MemTotal + SwapTotal the system is over-committed |

---

## 3. eBPF for Anomaly Detection

### 3.1 Why eBPF

Traditional monitoring approaches — running daemons that poll `/proc` every few seconds —
have two problems on embedded ARM systems:

1. **They miss transient events.** A fork storm that lasts 200 ms and crashes a process
   will not appear in a 5-second polling interval.
2. **They add overhead proportional to polling frequency.** High-frequency polling is
   expensive.

**eBPF** (Extended Berkeley Packet Filter) solves both problems by running small programs
*inside the kernel*, triggered by kernel events, with near-zero overhead on the fast path.
An eBPF program attached to the `clone` syscall sees every fork as it happens, with no
missed events and no polling.

eBPF programs run in a sandboxed VM with a verifier that prevents unbounded loops and
bad memory accesses. They cannot crash the kernel. They share data with userspace through
**maps** (shared memory structures) and **ring buffers**.

---

### 3.2 Key probe types used in this module

| Type | What it instruments | Used for |
|------|---------------------|----------|
| `kprobe` | Entry/return of any kernel function | `out_of_memory`, `do_send_sig_info` |
| `tracepoint` | Stable, versioned kernel event hooks | `syscalls:sys_enter_clone`, `oom:mark_victim`, `block:block_rq_issue` |
| `perf_event` | Hardware performance counters | CPU stalls, cache misses (not used in this module) |
| `interval` | Timer-driven, not a kernel probe | Periodic metric emission |

**Tracepoints** are preferred over kprobes when available because their argument layout is
stable across kernel versions. Kprobes attach to raw function signatures, which can change
between minor kernel versions.

---

### 3.3 What this module monitors

| Script | Probe | Detects |
|--------|-------|---------|
| `detect-fork-storm.bt` | `tracepoint:syscalls:sys_enter_clone` | Runaway process creation — > 50 forks/sec sustained |
| `detect-oom-pressure.bt` | `kprobe:out_of_memory`, `tracepoint:oom:mark_victim` | OOM invocations, victim process identity |
| `detect-io-anomaly.bt` | `tracepoint:block:block_rq_issue` | Write storms > 1000 ops/sec; flash wear acceleration |

---

### 3.4 bpftrace vs libbpf vs BCC

| Tool | Language | Overhead | Best for |
|------|----------|----------|----------|
| **bpftrace** | High-level scripting language | Low | One-liners, readable scripts, ad-hoc tracing |
| **BCC** | Python + C | Medium (compiles at runtime) | Interactive tools, prototyping |
| **libbpf** | C with CO-RE | Lowest | Production daemons, portable binaries |

This module uses **bpftrace** for all detection scripts because:
- Scripts are self-contained `.bt` files, easy to read and audit
- No compilation step needed at deploy time
- bpftrace ships prebuilt on most ARM distributions (Debian, Ubuntu, Fedora)
- bpftrace output is plain text, easy to parse into Prometheus metrics

For a production deployment that must minimize startup latency or run on a minimal
root filesystem, the bpftrace scripts in this module serve as the specification; a
libbpf CO-RE rewrite would be the next step.

---

### 3.5 Integration with Prometheus

The `ebpf-to-prometheus.sh` script bridges bpftrace output to Prometheus:

```
bpftrace detect-fork-storm.bt
    │  (stdout: "ALERT: fork storm comm=bash rate=200/s")
    ▼
ebpf-to-prometheus.sh (parser)
    │  (writes Prometheus text format)
    ▼
/var/lib/prometheus/node-exporter/ebpf.prom
    │  (node_exporter textfile collector reads *.prom every scrape)
    ▼
Prometheus scrape → Grafana / Alertmanager
```

Prometheus text format example:
```
# HELP ebpf_fork_storm_alert Fork storm alert triggered by bpftrace
# TYPE ebpf_fork_storm_alert gauge
ebpf_fork_storm_alert{comm="bash"} 1
# HELP ebpf_oom_victim_total Cumulative OOM kill events
# TYPE ebpf_oom_victim_total counter
ebpf_oom_victim_total{comm="stress-ng"} 3
# HELP ebpf_io_write_rps Block write requests per second
# TYPE ebpf_io_write_rps gauge
ebpf_io_write_rps{device="mmcblk0",comm="dd"} 2048
```

The `.prom` file is written atomically (write to `.tmp`, then `rename`) so Prometheus
never reads a partial file.

---

### 3.6 Requirements

- Linux kernel ≥ 5.8 (for most tracepoints used; kernel ≥ 5.15 recommended for ARM)
- `bpftrace` ≥ 0.14 installed: `apt install bpftrace` or `dnf install bpftrace`
- Must run as root or with `CAP_BPF + CAP_PERFMON` capabilities
- `CONFIG_BPF_SYSCALL=y`, `CONFIG_TRACEPOINTS=y` in kernel config (standard on distro kernels)

On ARM64 systems with Secure Boot, BPF JIT may need to be explicitly enabled:
```
echo 1 > /proc/sys/net/core/bpf_jit_enable
```

---

## File Reference

| File | Purpose |
|------|---------|
| `panic-config.sh` | Configure and verify kernel panic sysctl parameters |
| `oom-tuning.sh` | Adjust OOM killer priorities for systemd services |
| `sysctl-hardening.conf` | Complete sysctl configuration file, deploy to /etc/sysctl.d/ |
| `ebpf/detect-fork-storm.bt` | bpftrace: detect process creation storms |
| `ebpf/detect-oom-pressure.bt` | bpftrace: detect OOM invocations and victims |
| `ebpf/detect-io-anomaly.bt` | bpftrace: detect abnormal block I/O patterns |
| `ebpf/ebpf-to-prometheus.sh` | Run bpftrace scripts and export metrics to Prometheus |
| `tests/test_kernel_hardening.py` | pytest suite for all scripts and configs |

## Quick Start

```bash
# 1. Deploy sysctl settings
bash panic-config.sh --apply --panic-on-oops --watchdog-panic

# 2. Protect critical services from OOM killer
bash oom-tuning.sh --apply-defaults

# 3. Start eBPF monitoring and export to Prometheus
sudo bash ebpf/ebpf-to-prometheus.sh --loop --outdir /var/lib/prometheus/node-exporter/

# 4. Run tests
cd tests && python3 -m pytest test_kernel_hardening.py -v
```
