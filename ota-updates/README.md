# OTA Updates: Atomic, Safe, Rollback-Capable Firmware Updates for ARM Linux

## Why `apt-get` Is Wrong for Edge / Embedded Systems

Traditional package managers like `apt-get`/`dpkg` were designed for server and desktop
environments where:

- Power is stable and reliable
- Storage is abundant and fast
- A human can intervene if something goes wrong
- Downtime is acceptable

On an embedded ARM board — a gateway, industrial controller, or IoT device — none of
those assumptions hold. Using `apt-get` for OTA updates on such devices is a reliability
anti-pattern for several reasons:

### Power Loss Mid-Update Corrupts Package State

`dpkg` applies packages by unpacking a tarball onto the live filesystem and running
`preinst`/`postinst` scripts. If power is cut at any point during that process:

- The filesystem may contain a mix of old and new files
- Shared libraries may be partially overwritten (`libc.so.6` is a famous victim)
- The package database (`/var/lib/dpkg/status`) may be half-written, causing every
  subsequent `dpkg` call to fail with "database is locked" or "inconsistent state"
- The system may refuse to boot because the init binary or kernel modules were updated
  but not fully written

Recovery from this state requires either a recovery partition (which you probably did not
set up) or physical access to rewrite the eMMC — exactly the situation you were trying to avoid.

### No Atomic Rollback

`apt-get` does not support atomic rollback. `apt-get install --reinstall` can re-install
a package, but only if:

1. The package database is intact
2. The network is available (to re-download)
3. The system is still bootable

If the update broke the bootloader, kernel, or init system, none of those conditions hold.
You cannot `apt-get` your way out of a bricked device.

### Half-Written Binaries Can Survive

Unless every write path calls `fsync()` before `rename()`, a power loss after `write()`
but before `fsync()` leaves data only in the page cache. The OS may report success; the
data is lost when power dies. `dpkg` does not guarantee `fsync` on every file it writes.
Even if the file appears complete in size, its contents may be zeros or a mix of old and
new data.

---

## Atomic OTA Principles

### Image-Based vs. Package-Based Updates

| Approach | Atomicity | Rollback | Bandwidth | Complexity |
|---|---|---|---|---|
| Package-based (apt, rpm) | None | Manual | Low (delta pkgs) | Low |
| Image-based (full rootfs) | Full (A/B) | Automatic | High | Medium |
| Image-based (delta image) | Full (A/B) | Automatic | Medium | High |

**Image-based updates** replace the entire root filesystem at once. The update is applied
to an inactive partition while the live system continues running. A bootloader flag is
then toggled to select the new partition on next boot. If the new system fails to boot,
the bootloader automatically reverts to the previous partition.

### A/B Partition as the Rollback Mechanism

A typical A/B partition layout on eMMC:

```
mmcblk0p1  /boot      vfat   64 MiB   (U-Boot, kernel, DTB — shared)
mmcblk0p2  rootfs-A   ext4   1024 MiB (slot A, currently running)
mmcblk0p3  rootfs-B   ext4   1024 MiB (slot B, update target)
mmcblk0p4  /data      ext4   remaining (persistent data, logs, config)
```

The bootloader (U-Boot in our case) reads environment variables to decide which slot to
boot:

```
BOOT_ORDER="A B"      # try A first, then B
bootcount=0           # incremented each boot
bootlimit=3           # if bootcount >= bootlimit, try next slot
```

If the new firmware crashes before confirming a successful boot, `bootcount` reaches
`bootlimit` and U-Boot falls back to the other slot automatically.

### Update Agent Responsibilities

A correct update agent must:

1. **Verify the cryptographic signature** of the update bundle before writing any data.
   Writing first, verifying later creates a TOCTOU window where a corrupt bundle can
   brick the device.
2. **Write to the inactive partition** only. The live partition must never be touched.
3. **Set the bootloader flag** (via `fw_setenv`) to attempt the new slot on next boot,
   but with `bootcount=0` and `bootlimit=3` as a safety net.
4. **Trigger a controlled reboot** (not a hard reset) to give services time to flush
   state to `/data`.
5. **After reboot, verify boot success** (check that the expected kernel version and
   services are running).
6. **Confirm success to the update server** (or to RAUC's status file) so the bootcount
   watchdog is not triggered on the next reboot.

---

## RAUC: Robust Auto-Update Controller

RAUC is an open-source update framework built for exactly this use case. It is widely
used in automotive (Yocto/AGL), industrial, and embedded Linux projects.

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Update Server / CI pipeline                            │
│  rauc bundle --cert=cert.pem --key=key.pem ./dir out.raucb │
└────────────────────┬────────────────────────────────────┘
                     │ HTTPS / SCP / USB
                     ▼
┌─────────────────────────────────────────────────────────┐
│  ARM board running Linux                                │
│                                                         │
│  rauc service (DBus daemon)                             │
│    ├── reads /etc/rauc/system.conf (slot map)           │
│    ├── verifies bundle signature vs /etc/rauc/keyring.pem│
│    ├── calls pre-install hook                           │
│    ├── writes image to inactive slot                    │
│    ├── calls post-install hook (sets fw_setenv)         │
│    └── triggers reboot                                  │
└─────────────────────────────────────────────────────────┘
```

### RAUC Update Bundle (.raucb)

A `.raucb` file is a squashfs archive containing:

```
bundle.raucb (squashfs)
├── manifest.raucm      ← describes contents, compatible string, version
├── rootfs.ext4.img     ← raw ext4 image for rootfs slot
├── boot.vfat.img       ← raw vfat image for boot slot (optional)
└── signature           ← CMS signature over manifest + image checksums
```

The signature is a CMS (Cryptographic Message Syntax) structure, the same format used
by PKCS#7. RAUC verifies it against the certificate in `/etc/rauc/keyring.pem` before
mounting the squashfs or reading any image data.

### Key Files

**`/etc/rauc/system.conf`** — maps slot names to block devices, defines bootloader
integration. See `rauc-system.conf` in this directory.

**`/etc/rauc/keyring.pem`** — the CA certificate (or chain) used to verify update
bundle signatures. The corresponding private key lives on your build/signing server and
should never be on the device.

**`/data/rauc-status`** — RAUC's persistent status database. Because it lives on the
`/data` partition (not the rootfs), it survives rootfs updates and tracks which slot was
last confirmed good.

### U-Boot Integration

RAUC calls `fw_setenv` (from `u-boot-tools` package) to set bootloader environment
variables. After writing the new rootfs to slot B:

```bash
fw_setenv BOOT_ORDER "B A"   # try B first
fw_setenv bootcount 0        # reset boot attempt counter
fw_setenv bootlimit 3        # allow 3 attempts before fallback
```

U-Boot reads these on every boot. If `bootcount >= bootlimit`, it reverses `BOOT_ORDER`
and resets `bootcount`, booting the previously-working slot.

After the new system boots successfully, the first-boot service must call:

```bash
rauc status mark-good        # confirms boot success, resets bootcount watchdog
```

### Bundle Creation

On your build server:

```bash
# Generate signing keypair (do once, store key offline)
openssl genrsa -out update-key.pem 4096
openssl req -new -x509 -key update-key.pem -out update-cert.pem -days 3650 \
    -subj "/CN=OTA Update Authority/O=Example Corp"

# Deploy cert to all devices
cp update-cert.pem /etc/rauc/keyring.pem

# Build bundle directory
mkdir -p bundle-dir
cp rootfs.ext4.img bundle-dir/
cp boot.vfat.img   bundle-dir/
cp rauc-manifest.raucm bundle-dir/manifest.raucm

# Sign and create bundle
rauc bundle \
    --cert=update-cert.pem \
    --key=update-key.pem \
    bundle-dir/ \
    firmware-v2.1.0.raucb
```

### Installing an Update

```bash
# Via command line
rauc install firmware-v2.1.0.raucb

# Via DBus (from application code)
gdbus call --system \
    --dest de.pengutronix.rauc \
    --object-path / \
    --method de.pengutronix.rauc.Installer.Install \
    "/path/to/firmware-v2.1.0.raucb"

# Check status
rauc status
rauc status --output-format=json
```

---

## Mender: Cloud-Managed OTA Alternative

Mender takes a client-server approach. The Mender client runs on the device and polls
a Mender server (self-hosted or Mender.io SaaS) for updates.

### Differences from RAUC

| Feature | RAUC | Mender |
|---|---|---|
| Architecture | Local daemon + DBus | Client polls cloud server |
| Bundle format | `.raucb` (squashfs+CMS) | `.mender` (tar + JSON metadata) |
| Fleet management | None (DIY) | Built-in dashboard |
| Signature | CMS/PKCS#7 | RSA or EC JWT |
| A/B rollback | Yes (via bootloader) | Yes (via bootloader) |
| Air-gapped support | Yes (USB/SCP install) | Requires connectivity to server |
| Self-hosted | Yes (fully) | Yes (Mender Server OSS) |
| Commercial tier | No | Yes (Mender Enterprise) |

### When to Choose RAUC vs. Mender

**Choose RAUC when:**
- Devices are air-gapped or on a private network with no cloud access
- You want full control over the update pipeline
- You are already using Yocto/Buildroot and want tight integration
- You need to integrate with existing CI/CD (Jenkins, GitLab CI) via simple shell scripts

**Choose Mender when:**
- You manage a large fleet and need a GUI dashboard for deployment campaigns
- You want phased rollouts (deploy to 5% of fleet, check health, then 100%)
- You need remote device management features beyond just updates
- Your team prefers a managed service over operating your own infrastructure

---

## systemd Watchdog Integration (sd_notify)

### The Problem with `Restart=always`

`Restart=always` in a systemd unit will restart a service when it exits. But it does
**not** help when a service is alive but hung — stuck in an infinite loop, blocked on a
network call, deadlocked on a mutex, or waiting on a broken hardware device.

A process that is hung does not exit. From systemd's perspective, it is healthy.
The watchdog mechanism solves this.

### How systemd Watchdog Works

Add `WatchdogSec=30s` to your `[Service]` section. systemd then:

1. Sets the `WATCHDOG_USEC` environment variable to `30000000` (microseconds)
2. Expects the process to send `WATCHDOG=1` to the `NOTIFY_SOCKET` at least once
   every 30 seconds
3. If 30 seconds elapse without a ping, systemd kills the process with `SIGABRT`
   and restarts it (per `Restart=` policy)

### Application Requirements

The application must:

1. Read `WATCHDOG_USEC` from the environment to know the expected ping interval.
   Best practice: ping at `WATCHDOG_USEC / 2` to give headroom.
2. Call `sd_notify(0, "WATCHDOG=1")` periodically. Without `libsystemd`, this means
   writing `"WATCHDOG=1"` to the datagram socket at `$NOTIFY_SOCKET`.
3. Send `sd_notify(0, "READY=1")` after initialization is complete. Until this is
   sent (with `Type=notify`), systemd holds the service in "activating" state and
   blocks dependent units from starting.
4. Send `sd_notify(0, "STOPPING=1")` before exiting on `SIGTERM` to let systemd know
   the shutdown is intentional.

### NOTIFY_SOCKET Without libsystemd

```c
int sd_notify_manual(const char *msg) {
    const char *sock_path = getenv("NOTIFY_SOCKET");
    if (!sock_path) return 0;  // not running under systemd, ignore

    int fd = socket(AF_UNIX, SOCK_DGRAM, 0);
    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    strncpy(addr.sun_path, sock_path, sizeof(addr.sun_path) - 1);
    // Handle abstract sockets (path starts with '@')
    if (sock_path[0] == '@') addr.sun_path[0] = '\0';

    sendto(fd, msg, strlen(msg), MSG_NOSIGNAL,
           (struct sockaddr*)&addr, sizeof(addr));
    close(fd);
    return 1;
}
```

See `systemd-watchdog-example/hw-fault-monitor.c` for a complete implementation.

### STATUS= Messages

The `STATUS=` notification lets you set a human-readable status message visible in
`systemctl status`:

```c
sd_notify(0, "STATUS=Monitoring EDAC mc0, CE count: 42");
```

This appears in the `Status:` line of `systemctl status hw-fault-monitor`.

---

## Quick-Start

### Prerequisites

```bash
# On build host (Debian/Ubuntu)
apt-get install rauc u-boot-tools openssl

# On target device
apt-get install rauc u-boot-tools
```

### Deploy Configuration

```bash
# Copy system config
install -m 644 rauc-system.conf /etc/rauc/system.conf

# Install verification certificate
install -m 644 update-cert.pem /etc/rauc/keyring.pem

# Install hooks
install -m 755 rauc-pre-install.sh  /usr/lib/rauc/pre-install
install -m 755 rauc-post-install.sh /usr/lib/rauc/post-install

# Install watchdog monitor
gcc -O2 -o /usr/local/bin/hw-fault-monitor \
    systemd-watchdog-example/hw-fault-monitor.c
install -m 644 systemd-watchdog-example/hw-fault-monitor.service \
    /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hw-fault-monitor
```

### Run Tests

```bash
cd ota-updates/
python3 -m pytest tests/test_ota.py -v
```
