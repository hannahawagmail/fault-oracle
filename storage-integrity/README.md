<!-- SPDX-License-Identifier: Apache-2.0 -->
# storage-integrity — ARM Linux Storage Integrity and Wear Protection

This module provides scripts and documentation for protecting filesystem
integrity on ARM embedded systems, especially in the face of unexpected power
loss and flash memory wear. Filesystem corruption is the leading cause of
unrecoverable failures in deployed ARM devices.

---

## Table of Contents

1. [Why Read-Only Rootfs](#why-read-only-rootfs)
2. [overlayfs Architecture](#overlayfs-architecture)
3. [Power-Fail-Safe Filesystem Comparison](#power-fail-safe-filesystem-comparison)
4. [Write Rate Limiting and Flash Wear](#write-rate-limiting-and-flash-wear)
5. [Quick Start](#quick-start)
6. [File Reference](#file-reference)

---

## Why Read-Only Rootfs

### The Problem: Writes During Power Failure

Every write to a storage device — whether NOR flash, NAND flash, eMMC, or
UFS — has a window of vulnerability. If power is cut during a write operation:

1. **Journal/metadata inconsistency**: Filesystem metadata (inodes, directory
   entries, block allocation bitmaps) may be partially updated. On next mount,
   `fsck` may not be able to repair the damage automatically.

2. **Partial block write**: NAND flash writes entire pages (4KB–16KB). If
   power fails mid-page-write, the page may be in an indeterminate state.
   Some NAND controllers handle this via ECC, but the data is lost.

3. **Torn write on eMMC**: The eMMC controller handles internal write
   transactions, but the host-visible "write" may be split across multiple
   internal operations. The eMMC spec provides no atomicity guarantee for
   writes larger than one sector without explicit RPMBv2 features.

### The Solution: Immutable Rootfs

Making the root filesystem read-only (mounted `ro`) eliminates the entire
category of rootfs corruption:

- `/` is never written to after boot; power loss cannot corrupt it
- The rootfs image can be verified at any time (checksum, signature)
- OTA updates write to the *other* A/B slot, never the running rootfs
- EROFS (read-only filesystem) provides even better density: no journal
  overhead, no block allocation metadata to corrupt

### What Still Needs Write Access

A read-only rootfs means the following directories need special handling:

| Directory | Requirement | Solution |
|---|---|---|
| `/tmp` | Temporary files, must be writable | tmpfs (in RAM, volatile) |
| `/var/run`, `/run` | PID files, sockets, runtime state | tmpfs (standard on systemd) |
| `/var/log` | Log files | tmpfs + periodic flush to persistent partition |
| `/var/lib` | Application state, databases | overlayfs upper layer or bind-mount to /data |
| `/etc` | May need runtime writes (resolv.conf, machine-id) | overlayfs upper layer |
| `/home` | User data | Bind-mount from persistent data partition |

---

## overlayfs Architecture

overlayfs is a union filesystem built into the Linux kernel since v3.18. It
presents a merged view of a read-only *lower* directory and a writable *upper*
directory. Reads come from lower (fast, on-flash), writes go to upper (RAM or
persistent partition).

### Layer Structure

```
  ┌──────────────────────────────────────────────────────┐
  │  MERGED (presented to applications as /)             │
  │  Reads from lower if not in upper                    │
  │  Writes go to upper (copy-on-write)                  │
  └───────────────────┬──────────────────────────────────┘
                      │
          ┌───────────┴───────────┐
          │                       │
  ┌───────▼───────┐     ┌────────▼────────┐
  │  LOWER        │     │  UPPER + WORK   │
  │  (read-only)  │     │  (writable)     │
  │               │     │                 │
  │  squashfs or  │     │  tmpfs (RAM)    │
  │  ext4 ro      │     │  or ext4 on     │
  │  EROFS        │     │  data partition │
  └───────────────┘     └─────────────────┘
```

### How Copy-on-Write Works

1. Application reads `/etc/hostname` → file exists in lower → served directly
2. Application writes to `/etc/hostname` → overlayfs copies the file from
   lower to upper (copy-up), then writes the new content to upper
3. Subsequent reads of `/etc/hostname` → served from upper (modified version)
4. Lower directory is never modified — the rootfs stays intact
5. If upper is tmpfs → changes vanish at reboot (good for logs, resolv.conf)
6. If upper is on persistent partition → changes survive reboot (good for /etc)

### Mount Sequence

```bash
# 1. Mount the read-only lower (rootfs)
mount -o ro /dev/mmcblk0p2 /overlay/lower

# 2. Mount tmpfs for upper + work
mount -t tmpfs -o size=256M tmpfs /overlay/tmpfs
mkdir -p /overlay/tmpfs/upper /overlay/tmpfs/work

# 3. Mount overlayfs merged view
mount -t overlay overlay \
    -o lowerdir=/overlay/lower,upperdir=/overlay/tmpfs/upper,workdir=/overlay/tmpfs/work \
    /overlay/merged

# 4. Bind-mount writable directories over specific paths
mount --bind /overlay/tmpfs/upper/var /overlay/merged/var
mount --bind /run /overlay/merged/run
```

### Persistent Upper Layer

For changes that must survive reboot (e.g., /etc changes from user config):

```bash
# Use the data partition for upper instead of tmpfs
mount /dev/mmcblk0p4 /data
mkdir -p /data/overlay-upper /data/overlay-work
mount -t overlay overlay \
    -o lowerdir=/overlay/lower,upperdir=/data/overlay-upper,workdir=/data/overlay-work \
    /
```

This gives durability while still protecting the rootfs from modification.

---

## Power-Fail-Safe Filesystem Comparison

| Filesystem | Type | Journaling | Flash-optimized | Power-fail safety | Notes |
|---|---|---|---|---|---|
| **ext4** (data=journal) | Block | Full data+metadata | No | High | Full data journaling prevents torn writes; significant write amplification |
| **ext4** (data=ordered) | Block | Metadata only | No | Medium | Default mode; metadata consistent, but data writes not journaled |
| **ext4** (data=writeback) | Block | Metadata only | No | Low | Fastest but file data may contain stale content after crash |
| **Btrfs** | Block | CoW (no journal) | No | High | Copy-on-write means no partial overwrites; tree structure survives power loss |
| **F2FS** | Block | Log-structured | Yes | High | Designed for flash; segment-based writes reduce random I/O; good for eMMC |
| **UBIFS** | MTD/raw NAND | Log-structured | Yes (MTD) | Very High | Designed for raw NAND via UBI; handles bad blocks, wear leveling |
| **EROFS** | Block | None (read-only) | Yes | Perfect (read-only) | Cannot be corrupted by writes; ideal for rootfs-A/B slots |
| **squashfs** | Block | None (read-only) | Somewhat | Perfect (read-only) | Compressed read-only; lower density than EROFS but more tooling |
| **FAT32/vfat** | Block | None | No | Very Low | Never use for writable Linux dirs; acceptable for /boot/efi or SD card boot |
| **tmpfs** | RAM | None (volatile) | N/A | Perfect (no persistence) | Ideal for /tmp, /run, /var/run; disappears on power loss by design |
| **XFS** | Block | Metadata only | No | Medium | Excellent performance; requires significant RAM for log buffers; avoid on <512MB RAM |

### Recommendation for ARM Embedded Systems

```
/          → EROFS (read-only, A/B slot)
/boot      → ext4 data=journal or FAT32 (small, infrequently written)
/run       → tmpfs (standard systemd)
/tmp       → tmpfs
/var/log   → tmpfs + periodic rsync to /data
/data      → ext4 data=journal or F2FS (persistent user data)
```

### ext4 Journal Modes Explained

```
data=writeback: Only metadata is journaled.
    - Fastest performance.
    - After crash, file data may be stale or corrupted.
    - Metadata (directory entries, file sizes) will be consistent.
    - UNSAFE for databases or critical files.

data=ordered (DEFAULT):
    - Metadata journaled; data written to disk before metadata journal commit.
    - File data won't contain stale blocks after crash.
    - File may be shorter than expected (truncated) but not corrupted.
    - Good for most workloads.

data=journal:
    - Both data AND metadata written to journal before being applied.
    - Safest: any interrupted write is either fully committed or rolled back.
    - 2x write amplification (write to journal, then to data area).
    - Best for systems with frequent small writes under power-loss risk.
```

---

## Write Rate Limiting and Flash Wear

### Why Flash Wears Out

NAND flash cells (SLC, MLC, TLC, QLC) have a limited number of program/erase
(P/E) cycles before the cell can no longer reliably hold charge:

| Flash Type | P/E Cycles | Use Case |
|---|---|---|
| SLC NAND | 100,000+ | Industrial, automotive |
| eMLC / pSLC | 20,000–30,000 | Industrial eMMC |
| MLC NAND | 3,000–10,000 | Consumer eMMC, SD cards |
| TLC NAND | 300–1,000 | Consumer SSDs |
| QLC NAND | 100–300 | High-density consumer SSDs |

A consumer SD card at 1,000 P/E cycles and a 4GB cell size, written at
4MB/s continuously, would wear out in: `(1000 × 4GB) / 4MB/s = ~1,000,000s ≈
11 days`. This is why continuous log writing to SD card is catastrophic.

### Write Absorption with tmpfs

tmpfs intercepts writes in RAM, dramatically reducing flash writes:

- Systemd writes `/run/log/journal/` to RAM → zero flash wear during operation
- On shutdown (clean), sync to flash if needed
- On power loss: last ~15 minutes of logs lost (acceptable for most systems)
- Net result: flash wear reduced by 95%+ for typical log-heavy workloads

### The Periodic Flush Pattern

For systems that need log durability without constant flash writes:

```
/run/log (tmpfs, RAM)
    │
    │  rsync every 15 minutes (log-flush.timer)
    │
    ▼
/var/log/persistent (ext4 data=journal on data partition)
```

On clean shutdown, systemd's `log-flush.service` also runs, ensuring
the final state is captured. On power loss, at most 15 minutes of logs
are lost — a tunable tradeoff.

---

## Quick Start

### 1. Set Up Read-Only Rootfs with overlayfs

```bash
# Check current overlay status
bash overlayfs-setup.sh --status

# Preview what apply would do
bash overlayfs-setup.sh --apply --dry-run

# Apply overlayfs (requires root, best done from initramfs or early boot)
sudo bash overlayfs-setup.sh --apply --upper-tmpfs-size 256M
```

### 2. Audit Filesystem Choices

```bash
# Check all mounted filesystems for power-fail safety
bash filesystem-check.sh

# Example output:
# [ PASS ] /     ext4 ro         — read-only rootfs, excellent
# [ WARN ] /var  ext4 rw         — writable ext4 without data=journal
# [ PASS ] /tmp  tmpfs           — RAM-backed, wear-free
# [ FAIL ] /boot vfat            — FAT32 on writable mount, no journaling
```

### 3. Configure Log-to-tmpfs

```bash
# Preview changes
bash log-to-tmpfs.sh --dry-run

# Apply (configures journald, creates flush timer)
sudo bash log-to-tmpfs.sh

# Verify
systemctl status log-flush.timer
journalctl --disk-usage
```

### 4. Run Tests

```bash
cd tests/
python3 -m pytest test_storage_integrity.py -v
```

---

## File Reference

| File | Purpose | Lines |
|---|---|---|
| `overlayfs-setup.sh` | Set up read-only rootfs with overlayfs | ~200 |
| `filesystem-check.sh` | Audit filesystem choices for embedded ARM | ~180 |
| `log-to-tmpfs.sh` | Configure journald + logrotate to RAM | ~150 |
| `tests/test_storage_integrity.py` | Python tests (no hardware required) | ~180 |

---

## References

- overlayfs kernel documentation: `Documentation/filesystems/overlayfs.rst`
- ext4 data modes: `Documentation/filesystems/ext4/journal.rst`
- F2FS design: `Documentation/filesystems/f2fs.rst`
- UBIFS documentation: `Documentation/filesystems/ubifs.rst`
- EROFS: `Documentation/filesystems/erofs.rst`
- Linux tmpfs: `Documentation/filesystems/tmpfs.rst`
- systemd-journald: `man journald.conf(5)`
- Flash wear and eMMC: JEDEC JESD84-B51 (eMMC specification)
- MTD subsystem: `Documentation/driver-api/mtd/`
