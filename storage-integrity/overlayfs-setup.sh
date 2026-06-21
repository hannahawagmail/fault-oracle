#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# overlayfs-setup.sh — Set up read-only rootfs with overlayfs
#
# Creates: lower (ro) + upper (tmpfs) + work + merged mount points
# Bind-mounts /etc, /var, /tmp over the overlay for writable access.
#
# Architecture:
#   /overlay/lower   → bind-mount of / (or explicit ro block device)
#   /overlay/tmpfs   → tmpfs holding upper/ and work/ directories
#   /overlay/merged  → merged view presented to applications
#
# Usage:
#   bash overlayfs-setup.sh [--lower /] [--upper-tmpfs-size 256M] [--apply]
#                           [--status] [--dry-run] [--teardown]
#
# Exit codes:
#   0 — success
#   1 — error
#   2 — overlayfs not supported by kernel

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
LOWER_DIR="/"
UPPER_TMPFS_SIZE="256M"
OVERLAY_BASE="/overlay"
MODE=""
DRY_RUN=false

# Directories that need to be writable at runtime
# /var/log: log files          /var/run: PID files, sockets
# /var/lib: app state          /tmp: temporary files
# /etc: runtime config writes  (resolv.conf, machine-id on some systems)
WRITABLE_DIRS=("/var" "/tmp" "/etc")

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
info()  { echo "[INFO]  $(date '+%Y-%m-%d %H:%M:%S') $*" >&2; }
warn()  { echo "[WARN]  $(date '+%Y-%m-%d %H:%M:%S') $*" >&2; }
error() { echo "[ERROR] $(date '+%Y-%m-%d %H:%M:%S') $*" >&2; }
pass()  { echo "[ PASS ] $*"; }
fail()  { echo "[ FAIL ] $*"; }
skip_() { echo "[ SKIP ] $*"; }

# Run or dry-run a command
run_cmd() {
    if $DRY_RUN; then
        echo "[dry-run] $*"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --lower)             LOWER_DIR="$2";          shift 2 ;;
            --upper-tmpfs-size)  UPPER_TMPFS_SIZE="$2";   shift 2 ;;
            --upper-size)        UPPER_TMPFS_SIZE="$2";   shift 2 ;;  # alias
            --apply)             MODE="apply";             shift   ;;
            --status)            MODE="status";            shift   ;;
            --teardown)          MODE="teardown";          shift   ;;
            --dry-run)           DRY_RUN=true;             shift   ;;
            --help|-h)
                sed -n '2,16p' "$0" | sed 's/^# //'
                exit 0
                ;;
            *)
                error "Unknown argument: $1"
                exit 1
                ;;
        esac
    done

    if [[ -z "$MODE" ]]; then
        error "No mode specified. Use --status, --apply, or --teardown."
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Check kernel overlayfs support
# ---------------------------------------------------------------------------
check_overlay_support() {
    if grep -q "overlay" /proc/filesystems 2>/dev/null; then
        pass "overlayfs is supported by the running kernel"
        return 0
    fi

    # Try loading the module
    info "overlay not in /proc/filesystems — attempting modprobe overlay"
    if modprobe overlay 2>/dev/null; then
        if grep -q "overlay" /proc/filesystems; then
            pass "overlayfs module loaded successfully"
            return 0
        fi
    fi

    fail "overlayfs not supported by this kernel"
    echo "  Enable with: CONFIG_OVERLAY_FS=y (or =m) in kernel config"
    return 1
}

# ---------------------------------------------------------------------------
# --status mode: show overlay-related mounts
# ---------------------------------------------------------------------------
mode_status() {
    echo "=== overlayfs Status ==="
    echo ""

    # Check kernel support first
    if grep -q "overlay" /proc/filesystems 2>/dev/null; then
        pass "overlayfs: kernel support present"
    else
        skip_ "overlayfs: not in /proc/filesystems (may need modprobe overlay)"
    fi

    echo ""
    echo "--- Active overlay mounts ---"
    local found_overlay=false
    while IFS= read -r line; do
        if echo "$line" | grep -q "overlay\|overlayfs"; then
            echo "  $line"
            found_overlay=true
        fi
    done < /proc/mounts
    $found_overlay || echo "  (none)"

    echo ""
    echo "--- tmpfs mounts on writable dirs ---"
    local found_tmpfs=false
    while IFS= read -r line; do
        local fstype mountpoint
        fstype=$(echo "$line" | awk '{print $3}')
        mountpoint=$(echo "$line" | awk '{print $2}')
        if [[ "$fstype" == "tmpfs" ]]; then
            echo "  $line"
            found_tmpfs=true
        fi
    done < /proc/mounts
    $found_tmpfs || echo "  (none)"

    echo ""
    echo "--- Rootfs mount options ---"
    grep " / " /proc/mounts | head -5 || echo "  (cannot determine)"

    echo ""
    echo "--- Overlay base directory ---"
    if [[ -d "$OVERLAY_BASE" ]]; then
        pass "$OVERLAY_BASE exists"
        ls -la "$OVERLAY_BASE/" 2>/dev/null || true
    else
        skip_ "$OVERLAY_BASE does not exist (overlayfs not yet applied)"
    fi

    echo ""
    echo "--- tmpfiles.d configuration ---"
    if [[ -f /etc/tmpfiles.d/overlayfs-dirs.conf ]]; then
        pass "/etc/tmpfiles.d/overlayfs-dirs.conf exists"
        cat /etc/tmpfiles.d/overlayfs-dirs.conf
    else
        skip_ "/etc/tmpfiles.d/overlayfs-dirs.conf not found"
    fi
}

# ---------------------------------------------------------------------------
# --apply mode: set up overlayfs
# ---------------------------------------------------------------------------
mode_apply() {
    echo "=== Applying overlayfs Setup ==="
    echo ""

    # In dry-run mode, skip all kernel checks and just describe what would happen
    if $DRY_RUN; then
        echo "[dry-run] Would configure overlayfs with the following layout:"
        echo "  lower:  ${LOWER_DIR}  →  ${OVERLAY_BASE}/lower (bind-mounted read-only)"
        echo "  upper:  tmpfs         →  ${OVERLAY_BASE}/tmpfs/upper"
        echo "  work:   tmpfs         →  ${OVERLAY_BASE}/tmpfs/work"
        echo "  merged: overlay mount →  ${OVERLAY_BASE}/merged"
        echo "[dry-run] No filesystem changes performed."
        return 0
    fi

    # Check kernel support
    check_overlay_support || exit 2

    # Verify we are root
    if [[ $EUID -ne 0 ]] && ! $DRY_RUN; then
        error "Must run as root to mount filesystems"
        exit 1
    fi

    # Create directory structure
    local lower="${OVERLAY_BASE}/lower"
    local tmpfs_dir="${OVERLAY_BASE}/tmpfs"
    local upper="${OVERLAY_BASE}/tmpfs/upper"
    local work="${OVERLAY_BASE}/tmpfs/work"
    local merged="${OVERLAY_BASE}/merged"

    info "Creating overlay directory structure under $OVERLAY_BASE"
    run_cmd mkdir -p "$lower" "$tmpfs_dir" "$merged"

    # Step 1: bind-mount the lower (read-only) directory
    info "Bind-mounting lower directory: $LOWER_DIR → $lower (read-only)"
    run_cmd mount --bind "$LOWER_DIR" "$lower"
    run_cmd mount -o remount,ro,bind "$lower"
    pass "Lower directory mounted read-only at $lower"

    # Step 2: mount tmpfs for upper + work
    info "Mounting tmpfs (size=$UPPER_TMPFS_SIZE) at $tmpfs_dir"
    run_cmd mount -t tmpfs -o "size=${UPPER_TMPFS_SIZE},mode=0755" tmpfs "$tmpfs_dir"
    run_cmd mkdir -p "$upper" "$work"
    pass "tmpfs upper layer mounted at $tmpfs_dir (size: $UPPER_TMPFS_SIZE)"

    # Pre-populate upper with directories that need to exist
    # so they appear in the merged view even before any writes
    for dir in "${WRITABLE_DIRS[@]}"; do
        local upper_subdir="${upper}${dir}"
        run_cmd mkdir -p "$upper_subdir"
        info "Pre-created upper subdir: $upper_subdir"
    done

    # Step 3: mount the overlay
    info "Mounting overlayfs: lower=$lower, upper=$upper, work=$work, merged=$merged"
    run_cmd mount -t overlay overlay \
        -o "lowerdir=${lower},upperdir=${upper},workdir=${work}" \
        "$merged"
    pass "overlayfs merged view mounted at $merged"

    # Step 4: bind-mount writable directories over the merged view
    # This allows specific directories to be writable while keeping / read-only.
    #
    # Why each directory needs write access:
    #   /var     — log files, PID files, app state databases (e.g., dpkg, rpm)
    #   /tmp     — POSIX temporary files; many programs fail without writable /tmp
    #   /etc     — resolv.conf (updated by dhclient/networkd), machine-id, localtime
    #
    echo ""
    info "Setting up writable bind-mounts over overlay directories..."
    for dir in "${WRITABLE_DIRS[@]}"; do
        local upper_subdir="${upper}${dir}"
        local merged_dir="${merged}${dir}"
        if [[ -d "$merged_dir" ]]; then
            run_cmd mount --bind "$upper_subdir" "$merged_dir"
            pass "Bind-mount: $upper_subdir → $merged_dir (writable)"
        else
            warn "Merged directory $merged_dir does not exist, skipping bind-mount"
        fi
    done

    # Step 5: generate fstab entries
    echo ""
    info "Generating /etc/fstab entries (append these to your fstab):"
    cat <<FSTAB

# overlayfs entries — generated by overlayfs-setup.sh
# Add to /etc/fstab or use in initramfs setup script:
#
overlay  ${merged}  overlay  lowerdir=${lower},upperdir=${upper},workdir=${work},ro  0  0
tmpfs    ${tmpfs_dir}  tmpfs  size=${UPPER_TMPFS_SIZE},mode=0755  0  0
FSTAB

    # Step 6: generate systemd tmpfiles.d snippet
    echo ""
    info "Generating /etc/tmpfiles.d/overlayfs-dirs.conf..."
    local tmpfiles_conf="/etc/tmpfiles.d/overlayfs-dirs.conf"
    if $DRY_RUN; then
        echo "[dry-run] Would write $tmpfiles_conf:"
        cat <<TMPFILES
# tmpfiles.d — persistent directories under overlayfs upper layer
# See tmpfiles.d(5)
d  ${upper}/var/log      0755 root root -
d  ${upper}/var/run      0755 root root -
d  ${upper}/var/lib      0755 root root -
d  ${upper}/tmp          1777 root root -
d  ${upper}/etc          0755 root root -
TMPFILES
    else
        mkdir -p "$(dirname "$tmpfiles_conf")"
        cat > "$tmpfiles_conf" <<TMPFILES
# SPDX-License-Identifier: Apache-2.0
# tmpfiles.d — persistent directories under overlayfs upper layer
# See tmpfiles.d(5)
# Generated by overlayfs-setup.sh
d  ${upper}/var/log      0755 root root -
d  ${upper}/var/run      0755 root root -
d  ${upper}/var/lib      0755 root root -
d  ${upper}/tmp          1777 root root -
d  ${upper}/etc          0755 root root -
TMPFILES
        pass "Wrote $tmpfiles_conf"
        info "Apply now: systemd-tmpfiles --create $tmpfiles_conf"
    fi

    echo ""
    pass "overlayfs setup complete. Merged view at: $merged"
    echo ""
    echo "NOTE: For production use, this setup should run from initramfs"
    echo "      before pivoting to the merged root. See your initramfs"
    echo "      configuration (initramfs-tools, dracut, or custom initrd)."
}

# ---------------------------------------------------------------------------
# --teardown mode: unmount in reverse order
# ---------------------------------------------------------------------------
mode_teardown() {
    echo "=== Tearing Down overlayfs ==="
    echo ""

    local lower="${OVERLAY_BASE}/lower"
    local tmpfs_dir="${OVERLAY_BASE}/tmpfs"
    local merged="${OVERLAY_BASE}/merged"

    # Unmount bind-mounts first (in reverse dependency order)
    for dir in $(echo "${WRITABLE_DIRS[@]}" | tr ' ' '\n' | tac); do
        local merged_dir="${merged}${dir}"
        if mountpoint -q "$merged_dir" 2>/dev/null; then
            info "Unmounting bind-mount: $merged_dir"
            run_cmd umount "$merged_dir" && pass "Unmounted $merged_dir" || warn "Failed to unmount $merged_dir"
        fi
    done

    # Unmount merged overlay
    if mountpoint -q "$merged" 2>/dev/null; then
        info "Unmounting overlay merged: $merged"
        run_cmd umount "$merged" && pass "Unmounted $merged" || warn "Failed to unmount $merged"
    else
        skip_ "$merged is not mounted"
    fi

    # Unmount tmpfs
    if mountpoint -q "$tmpfs_dir" 2>/dev/null; then
        info "Unmounting tmpfs: $tmpfs_dir"
        run_cmd umount "$tmpfs_dir" && pass "Unmounted $tmpfs_dir" || warn "Failed to unmount $tmpfs_dir"
    else
        skip_ "$tmpfs_dir is not mounted"
    fi

    # Unmount lower bind-mount
    if mountpoint -q "$lower" 2>/dev/null; then
        info "Unmounting lower: $lower"
        run_cmd umount "$lower" && pass "Unmounted $lower" || warn "Failed to unmount $lower"
    else
        skip_ "$lower is not mounted"
    fi

    pass "Teardown complete"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    parse_args "$@"

    case "$MODE" in
        status)   mode_status   ;;
        apply)    mode_apply    ;;
        teardown) mode_teardown ;;
    esac
}

main "$@"
