#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# filesystem-check.sh — Audit filesystem choices for embedded ARM systems
#
# Checks each mounted filesystem for power-fail safety and flash wear
# characteristics. Reports PASS/WARN/FAIL per check.
#
# Usage:
#   bash filesystem-check.sh [--verbose] [--json]
#
# Exit codes:
#   0 — all checks pass
#   1 — one or more FAIL results

set -euo pipefail

VERBOSE=false
JSON_MODE=false
OVERALL_RESULT=0
FAIL_COUNT=0
WARN_COUNT=0
PASS_COUNT=0

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
pass_()  { echo "[ PASS ] $*";  (( PASS_COUNT++ )) || true; }
warn_()  { echo "[ WARN ] $*";  (( WARN_COUNT++ )) || true; }
fail_()  { echo "[ FAIL ] $*";  (( FAIL_COUNT++ )) || true; OVERALL_RESULT=1; }
skip_()  { echo "[ SKIP ] $*";  }
info()   { echo "[INFO]  $(date '+%Y-%m-%d %H:%M:%S') $*" >&2; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --verbose|-v) VERBOSE=true; shift ;;
            --json)       JSON_MODE=true; shift ;;
            --help|-h)
                sed -n '2,14p' "$0" | sed 's/^# //'
                exit 0
                ;;
            *) echo "Unknown argument: $1" >&2; exit 1 ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Parse /proc/mounts into associative arrays
# Returns: device, mountpoint, fstype, options
# ---------------------------------------------------------------------------
get_mount_options() {
    local mountpoint="$1"
    grep -E " ${mountpoint} " /proc/mounts 2>/dev/null | awk '{print $4}' | head -1 || true
}

get_mount_device() {
    local mountpoint="$1"
    grep -E " ${mountpoint} " /proc/mounts 2>/dev/null | awk '{print $1}' | head -1 || true
}

get_mount_fstype() {
    local mountpoint="$1"
    grep -E " ${mountpoint} " /proc/mounts 2>/dev/null | awk '{print $3}' | head -1 || true
}

# ---------------------------------------------------------------------------
# CHECK: Is rootfs mounted read-only?
# ---------------------------------------------------------------------------
check_rootfs_ro() {
    echo ""
    echo "--- Check: Root filesystem mount mode ---"

    local opts
    opts=$(get_mount_options "/")
    local fstype
    fstype=$(get_mount_fstype "/")
    local device
    device=$(get_mount_device "/")

    echo "  Device:  ${device:-unknown}"
    echo "  FStype:  ${fstype:-unknown}"
    echo "  Options: ${opts:-unknown}"

    if echo "$opts" | grep -qE "(^|,)ro(,|$)"; then
        pass_ "/ is mounted read-only ($fstype) — excellent for power-fail safety"
    elif [[ "$fstype" == "overlay" || "$fstype" == "overlayfs" ]]; then
        pass_ "/ is overlayfs — lower layer is read-only, writes go to upper"
    elif [[ "$fstype" == "tmpfs" ]]; then
        pass_ "/ is tmpfs — RAM-backed, no flash writes (diskless/initramfs system?)"
    elif [[ "$fstype" == "squashfs" || "$fstype" == "erofs" ]]; then
        pass_ "/ is $fstype — inherently read-only filesystem"
    else
        fail_ "/ is mounted read-write ($fstype) — power failure during write can corrupt rootfs"
        echo "       Recommendation: use EROFS or squashfs for root, or mount ro with overlayfs"
    fi

    # Check if ext4 with data=journal when writable
    if [[ "$fstype" == "ext4" ]] && ! echo "$opts" | grep -qE "(^|,)ro(,|$)"; then
        if echo "$opts" | grep -q "data=journal"; then
            pass_ "/ ext4 has data=journal — full journaling active"
        elif echo "$opts" | grep -q "data=ordered"; then
            warn_ "/ ext4 has data=ordered — metadata journaled, but data writes not fully safe"
        elif echo "$opts" | grep -q "data=writeback"; then
            fail_ "/ ext4 has data=writeback — unsafe for power-loss scenarios"
        else
            warn_ "/ ext4: journal mode not explicit in mount options (check /etc/fstab)"
        fi
    fi
}

# ---------------------------------------------------------------------------
# CHECK: Is /tmp on tmpfs?
# ---------------------------------------------------------------------------
check_tmp_tmpfs() {
    echo ""
    echo "--- Check: /tmp filesystem ---"

    local fstype
    fstype=$(get_mount_fstype "/tmp")
    local opts
    opts=$(get_mount_options "/tmp")

    if [[ -z "$fstype" ]]; then
        # /tmp may be on same mount as /
        local root_fstype
        root_fstype=$(get_mount_fstype "/")
        warn_ "/tmp has no dedicated mount (lives on $root_fstype rootfs)"
        echo "       Recommendation: mount tmpfs on /tmp to keep writes off flash"
        echo "       Add to /etc/fstab: tmpfs /tmp tmpfs defaults,mode=1777,size=64M 0 0"
        return
    fi

    if [[ "$fstype" == "tmpfs" ]]; then
        pass_ "/tmp is on tmpfs — no flash wear from temporary files"
        $VERBOSE && echo "  Options: $opts"
    else
        warn_ "/tmp is on $fstype (not tmpfs) — temporary file writes hit flash"
        echo "       Consider: mount -t tmpfs tmpfs /tmp -o size=64M,mode=1777"
    fi
}

# ---------------------------------------------------------------------------
# CHECK: All mounted ext4 filesystems for journal mode
# ---------------------------------------------------------------------------
check_ext4_journal_modes() {
    echo ""
    echo "--- Check: ext4 journal modes ---"

    local found_ext4=false
    while IFS=" " read -r device mountpoint fstype opts _rest; do
        [[ "$fstype" != "ext4" ]] && continue
        found_ext4=true

        local mode="unknown"
        if echo "$opts" | grep -q "data=journal";   then mode="journal";   fi
        if echo "$opts" | grep -q "data=ordered";   then mode="ordered";   fi
        if echo "$opts" | grep -q "data=writeback"; then mode="writeback"; fi

        local is_ro=false
        echo "$opts" | grep -qE "(^|,)ro(,|$)" && is_ro=true

        local has_noatime=false
        echo "$opts" | grep -qE "(^|,)no(a)?time(,|$)" && has_noatime=true

        echo "  Mount: $device → $mountpoint"
        echo "    fstype:  ext4"
        echo "    options: $opts"

        if $is_ro; then
            pass_ "$mountpoint ext4 ro — read-only, no write safety concern"
        else
            case "$mode" in
                journal)
                    pass_ "$mountpoint ext4 data=journal — fully journaled writes"
                    ;;
                ordered)
                    warn_ "$mountpoint ext4 data=ordered — metadata safe, data not fully journaled"
                    ;;
                writeback)
                    fail_ "$mountpoint ext4 data=writeback — data not journaled, unsafe for power loss"
                    ;;
                unknown)
                    warn_ "$mountpoint ext4 — journal mode not specified in mount options"
                    echo "       Check: tune2fs -l $device | grep 'Default mount options'"
                    ;;
            esac
        fi

        # Check noatime / nodiratime — important for reducing unnecessary writes on flash
        if $has_noatime; then
            pass_ "$mountpoint has noatime/nodiratime — access time writes disabled (good for flash)"
        else
            warn_ "$mountpoint missing noatime/nodiratime — every file read updates atime on flash"
            echo "       Add to fstab: noatime,nodiratime"
        fi

    done < /proc/mounts

    $found_ext4 || skip_ "No ext4 filesystems found in /proc/mounts"
}

# ---------------------------------------------------------------------------
# CHECK: FAT/vfat mounts (dangerous for writable Linux dirs)
# ---------------------------------------------------------------------------
check_vfat_mounts() {
    echo ""
    echo "--- Check: FAT/vfat filesystem mounts ---"

    local found_vfat=false
    while IFS=" " read -r device mountpoint fstype opts _rest; do
        [[ "$fstype" != "vfat" && "$fstype" != "fat" && "$fstype" != "msdos" ]] && continue
        found_vfat=true

        echo "  Mount: $device → $mountpoint ($fstype)"

        # /boot/efi is acceptable for FAT (EFI system partition)
        # /boot on some ARM boards uses FAT for U-Boot access — also acceptable
        if [[ "$mountpoint" == "/boot/efi" || "$mountpoint" == "/boot" ]]; then
            warn_ "$mountpoint is FAT — acceptable for EFI/boot partition but has no journaling"
            echo "       Limit writes here; don't use it for volatile data"
        elif [[ "$mountpoint" == "/" || "$mountpoint" == "/var" || "$mountpoint" == "/etc" ]]; then
            fail_ "$mountpoint is FAT ($fstype) — no journaling, unsafe for Linux system directories"
        else
            warn_ "$mountpoint is FAT ($fstype) — no journaling; avoid writing critical data here"
        fi

    done < /proc/mounts

    $found_vfat || skip_ "No FAT/vfat mounts found"
}

# ---------------------------------------------------------------------------
# CHECK: XFS on low-RAM systems
# ---------------------------------------------------------------------------
check_xfs_ram() {
    echo ""
    echo "--- Check: XFS on RAM-constrained systems ---"

    local found_xfs=false
    while IFS=" " read -r device mountpoint fstype opts _rest; do
        [[ "$fstype" != "xfs" ]] && continue
        found_xfs=true

        echo "  Mount: $device → $mountpoint (xfs)"

        # Get total RAM in MB
        local ram_mb
        ram_mb=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)

        if (( ram_mb < 512 )); then
            warn_ "$mountpoint XFS on system with ${ram_mb}MB RAM"
            echo "       XFS log buffers and metadata caches use significant RAM."
            echo "       Consider ext4 or F2FS for systems with <512MB RAM."
        elif (( ram_mb < 1024 )); then
            warn_ "$mountpoint XFS on system with ${ram_mb}MB RAM — monitor memory pressure"
        else
            pass_ "$mountpoint XFS with ${ram_md}MB RAM — adequate for XFS log buffers"
        fi

    done < /proc/mounts

    $found_xfs || skip_ "No XFS mounts found"
}

# ---------------------------------------------------------------------------
# CHECK: Flash wear indicators from sysfs
# ---------------------------------------------------------------------------
check_flash_wear() {
    echo ""
    echo "--- Check: Flash/eMMC wear indicators ---"

    local found_block=false

    # eMMC: check /sys/class/mmc_host/*/
    for mmc_host in /sys/class/mmc_host/*/; do
        for card_dir in "${mmc_host}"mmc[0-9]*/; do
            [[ -d "$card_dir" ]] || continue
            found_block=true

            local name
            name=$(cat "${card_dir}name" 2>/dev/null || echo "unknown")
            local type
            type=$(cat "${card_dir}type" 2>/dev/null || echo "unknown")

            echo "  eMMC/SD device: $card_dir (name=$name, type=$type)"

            # Life time estimate (eMMC ≥5.0 via /sys/.../life_time)
            for lt_file in "${card_dir}"life_time "${card_dir}"mmc/life_time; do
                if [[ -r "$lt_file" ]]; then
                    local lt
                    lt=$(cat "$lt_file" 2>/dev/null || echo "N/A")
                    echo "    Life time estimate: $lt"
                    # eMMC life_time: 0x01 = 0-10%, 0x09 = 80-90%, 0x0A = >90% worn
                    if echo "$lt" | grep -qE "0x0[89A-Fa-f]"; then
                        warn_ "eMMC life time estimate indicates >80% wear ($lt)"
                    elif echo "$lt" | grep -q "0x0[1-7]"; then
                        pass_ "eMMC life time estimate: $lt (not heavily worn)"
                    fi
                fi
            done

            # Pre-EOL information
            for eol_file in "${card_dir}"pre_eol_info "${card_dir}"mmc/pre_eol_info; do
                if [[ -r "$eol_file" ]]; then
                    local eol
                    eol=$(cat "$eol_file" 2>/dev/null || echo "N/A")
                    echo "    Pre-EOL info: $eol"
                    if echo "$eol" | grep -qE "0x02"; then
                        warn_ "eMMC pre-EOL: urgent (0x02) — replace device soon"
                    elif echo "$eol" | grep -qE "0x03"; then
                        fail_ "eMMC pre-EOL: EOL reached (0x03) — device at end of life"
                    fi
                fi
            done
        done
    done

    # Block device I/O stats from /sys/block/*/stat
    for block_dev in /sys/block/mmcblk[0-9] /sys/block/nvme[0-9]n[0-9]; do
        [[ -d "$block_dev" ]] || continue
        found_block=true
        local dev_name
        dev_name=$(basename "$block_dev")
        local stat_file="${block_dev}/stat"
        if [[ -r "$stat_file" ]]; then
            # /sys/block/X/stat fields: read_ios read_merges read_sectors read_ticks
            #   write_ios write_merges write_sectors write_ticks ...
            local write_ios write_sectors
            write_ios=$(awk '{print $5}' "$stat_file" 2>/dev/null || echo 0)
            write_sectors=$(awk '{print $7}' "$stat_file" 2>/dev/null || echo 0)
            local write_mb=$(( write_sectors / 2048 ))
            if $VERBOSE || (( write_mb > 1000 )); then
                echo "  $dev_name: write_ios=$write_ios, write_sectors=$write_sectors (~${write_mb}MB written this session)"
            fi
        fi
    done

    $found_block || skip_ "No block devices found in /sys/block or /sys/class/mmc_host"
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print_summary() {
    echo ""
    echo "========================================="
    echo " Filesystem Audit Summary"
    echo "========================================="
    echo "  PASS: $PASS_COUNT"
    echo "  WARN: $WARN_COUNT"
    echo "  FAIL: $FAIL_COUNT"
    if [[ $OVERALL_RESULT -eq 0 ]]; then
        echo " RESULT: PASS"
    else
        echo " RESULT: FAIL — address the FAIL items above"
    fi
    echo "========================================="
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    parse_args "$@"

    echo "=== Filesystem Power-Fail Safety Audit ==="
    echo "    $(date)"
    echo "    Kernel: $(uname -r)  Arch: $(uname -m)"
    echo ""

    echo "Full mount table:"
    column -t /proc/mounts 2>/dev/null | head -30 || cat /proc/mounts | head -30
    echo ""

    check_rootfs_ro
    check_tmp_tmpfs
    check_ext4_journal_modes
    check_vfat_mounts
    check_xfs_ram
    check_flash_wear
    print_summary

    exit $OVERALL_RESULT
}

main "$@"
