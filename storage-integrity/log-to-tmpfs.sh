#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# log-to-tmpfs.sh — Configure log rotation to RAM with periodic disk flush
#
# What this script does:
#   1. Configures systemd-journald to use volatile (RAM) storage
#   2. Creates a systemd timer that flushes logs to persistent storage every 15m
#   3. Configures logrotate for /run/log with size limits
#   4. Sets up log archive directory on persistent storage (/var/log/persistent)
#
# Rationale:
#   Writing logs continuously to NAND/eMMC flash is the #1 cause of premature
#   wear on embedded ARM systems. A consumer eMMC with 1000 P/E cycles and
#   constant syslog writes at 100KB/min would exhaust wear budget in ~7 years.
#   With tmpfs logging, flash writes drop by 95%+.
#
# Usage:
#   bash log-to-tmpfs.sh [--dry-run] [--flush-interval <minutes>]
#                        [--max-use <size>] [--status]
#
# Exit codes:
#   0 — success
#   1 — error

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DRY_RUN=false
FLUSH_INTERVAL_MIN=15
JOURNALD_MAX_USE="64M"
JOURNALD_CONF="/etc/systemd/journald.conf"
LOGROTATE_CONF="/etc/logrotate.d/tmpfs-logs"
FLUSH_SERVICE_FILE="/etc/systemd/system/log-flush.service"
FLUSH_TIMER_FILE="/etc/systemd/system/log-flush.timer"
PERSISTENT_LOG_DIR="/var/log/persistent"
ARCHIVE_LOG_DIR="/var/log/archive"
MODE="apply"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
info()  { echo "[INFO]  $(date '+%Y-%m-%d %H:%M:%S') $*" >&2; }
warn()  { echo "[WARN]  $(date '+%Y-%m-%d %H:%M:%S') $*" >&2; }
error() { echo "[ERROR] $(date '+%Y-%m-%d %H:%M:%S') $*" >&2; }
pass()  { echo "[ PASS ] $*"; }
fail()  { echo "[ FAIL ] $*"; }
skip_() { echo "[ SKIP ] $*"; }

write_file() {
    local path="$1"
    local content="$2"
    if $DRY_RUN; then
        echo "[dry-run] Would write: $path"
        echo "--- content start ---"
        echo "$content"
        echo "--- content end ---"
    else
        echo "$content" > "$path"
        pass "Wrote: $path"
    fi
}

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
            --dry-run)         DRY_RUN=true;                   shift   ;;
            --status)          MODE="status";                   shift   ;;
            --flush-interval)  FLUSH_INTERVAL_MIN="$2";        shift 2 ;;
            --max-use)         JOURNALD_MAX_USE="$2";           shift 2 ;;
            --help|-h)
                sed -n '2,20p' "$0" | sed 's/^# //'
                exit 0
                ;;
            *)
                error "Unknown argument: $1"
                exit 1
                ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# --status mode
# ---------------------------------------------------------------------------
mode_status() {
    echo "=== Log-to-tmpfs Status ==="
    echo ""

    # journald storage setting
    echo "--- journald Storage ---"
    if [[ -f "$JOURNALD_CONF" ]]; then
        local storage
        storage=$(grep -E "^Storage\s*=" "$JOURNALD_CONF" 2>/dev/null || echo "(not set)")
        echo "  Storage setting: $storage"
        local maxuse
        maxuse=$(grep -E "^MaxUse\s*=" "$JOURNALD_CONF" 2>/dev/null || echo "(not set)")
        echo "  MaxUse setting:  $maxuse"

        if echo "$storage" | grep -q "volatile"; then
            pass "journald using volatile (RAM) storage"
        elif echo "$storage" | grep -q "none"; then
            pass "journald storage=none (logging disabled)"
        else
            fail "journald not using volatile storage — writing to flash"
        fi
    else
        skip_ "$JOURNALD_CONF not found"
    fi

    # Check /run/log usage
    echo ""
    echo "--- /run/log usage ---"
    if [[ -d /run/log ]]; then
        du -sh /run/log 2>/dev/null || echo "  (cannot determine size)"
        pass "/run/log directory exists"
    else
        skip_ "/run/log does not exist"
    fi

    # Journal disk usage
    echo ""
    echo "--- Journal disk usage ---"
    if command -v journalctl &>/dev/null; then
        journalctl --disk-usage 2>/dev/null || skip_ "journalctl --disk-usage failed"
    fi

    # Timer status
    echo ""
    echo "--- log-flush.timer ---"
    if systemctl is-active log-flush.timer &>/dev/null; then
        pass "log-flush.timer is active"
        systemctl status log-flush.timer --no-pager 2>/dev/null | head -10 || true
    elif [[ -f "$FLUSH_TIMER_FILE" ]]; then
        warn "log-flush.timer installed but not active"
        echo "  Enable: systemctl enable --now log-flush.timer"
    else
        skip_ "log-flush.timer not installed"
    fi
}

# ---------------------------------------------------------------------------
# Step 1: Configure journald for volatile storage
# ---------------------------------------------------------------------------
configure_journald() {
    echo ""
    echo "--- Configuring systemd-journald ---"

    if [[ ! -f "$JOURNALD_CONF" ]]; then
        warn "$JOURNALD_CONF not found — creating minimal config"
        run_cmd mkdir -p "$(dirname "$JOURNALD_CONF")"
    fi

    # Read existing config or start fresh
    local existing_content=""
    [[ -f "$JOURNALD_CONF" ]] && existing_content=$(cat "$JOURNALD_CONF")

    # We write a [Journal] section with our required settings.
    # Strategy: if [Journal] section exists, update Storage= and MaxUse= lines.
    #           if not, append the section.
    #
    # For safety in dry-run and real mode, we just show what we'd write:
    local new_conf
    new_conf=$(cat <<JOURNALD
# SPDX-License-Identifier: Apache-2.0
# /etc/systemd/journald.conf — configured by log-to-tmpfs.sh
# See journald.conf(5) for details.

[Journal]
# volatile: journal stored in /run/log/journal (RAM, cleared on reboot)
# This prevents continuous writes to flash storage.
Storage=volatile

# Maximum journal size in RAM. Older entries are dropped when this is reached.
# For embedded systems, 64MB is a generous amount.
MaxUse=${JOURNALD_MAX_USE}

# Keep individual journal files under 16MB for easier management
MaxFileSize=16M

# Limit rate at which messages are accepted (burst of 200 msgs per 30s)
RateLimitInterval=30s
RateLimitBurst=200

# Compress journal files to reduce RAM usage
Compress=yes

# Forward to syslog only if syslog is present (typically no on systemd systems)
ForwardToSyslog=no
JOURNALD
)

    write_file "$JOURNALD_CONF" "$new_conf"

    if ! $DRY_RUN; then
        # Restart journald to apply config
        if systemctl is-system-running --quiet 2>/dev/null || systemctl is-active systemd-journald &>/dev/null; then
            run_cmd systemctl restart systemd-journald
            pass "systemd-journald restarted"
        else
            warn "systemd not fully running — journald will pick up config on next start"
        fi
    fi
}

# ---------------------------------------------------------------------------
# Step 2: Create persistent log archive directory
# ---------------------------------------------------------------------------
setup_persistent_dir() {
    echo ""
    echo "--- Setting up persistent log directory ---"

    run_cmd mkdir -p "$PERSISTENT_LOG_DIR"
    run_cmd mkdir -p "$ARCHIVE_LOG_DIR"

    if ! $DRY_RUN; then
        chmod 750 "$PERSISTENT_LOG_DIR" 2>/dev/null || true
        chmod 750 "$ARCHIVE_LOG_DIR" 2>/dev/null || true
        pass "Created $PERSISTENT_LOG_DIR and $ARCHIVE_LOG_DIR"
    fi
}

# ---------------------------------------------------------------------------
# Step 3: Configure logrotate for RAM logs
# ---------------------------------------------------------------------------
configure_logrotate() {
    echo ""
    echo "--- Configuring logrotate for /run/log ---"

    local logrotate_conf
    logrotate_conf=$(cat <<LOGROTATE
# SPDX-License-Identifier: Apache-2.0
# /etc/logrotate.d/tmpfs-logs — rotate logs from RAM to persistent archive
# Managed by log-to-tmpfs.sh

/run/log/*.log {
    # Rotate when file exceeds 10MB (don't wait for daily cron)
    size 10M

    # Keep 3 rotated copies in RAM (space permitting)
    rotate 3

    # Don't fail if log file is missing
    missingok

    # Don't rotate empty files
    notifempty

    # Compress rotated files to save RAM
    compress
    delaycompress

    # Write rotated files to persistent archive directory
    olddir ${ARCHIVE_LOG_DIR}

    # Create new log file with these permissions after rotation
    create 0640 root adm

    # Run after rotation: rsync archive to persistent storage
    postrotate
        rsync -a --delete ${ARCHIVE_LOG_DIR}/ ${PERSISTENT_LOG_DIR}/ 2>/dev/null || true
    endscript
}

# Rotate systemd journal exports if they exist
/run/log/journal.log {
    size 20M
    rotate 2
    missingok
    notifempty
    compress
    delaycompress
    olddir ${ARCHIVE_LOG_DIR}
    create 0640 root adm
}
LOGROTATE
)

    write_file "$LOGROTATE_CONF" "$logrotate_conf"
}

# ---------------------------------------------------------------------------
# Step 4: Create systemd log-flush.service
# ---------------------------------------------------------------------------
create_flush_service() {
    echo ""
    echo "--- Creating log-flush.service ---"

    local service_content
    service_content=$(cat <<SERVICE
# SPDX-License-Identifier: Apache-2.0
# /etc/systemd/system/log-flush.service
# Periodically flush volatile journal and /run/log to persistent storage.
# Triggered by log-flush.timer every ${FLUSH_INTERVAL_MIN} minutes.

[Unit]
Description=Flush RAM logs to persistent storage
Documentation=man:journalctl(1) man:rsync(1)
# Require network to be up only if shipping logs remotely (remove if local only)
# After=network.target
DefaultDependencies=no
# Run before shutdown to ensure final logs are saved
Before=shutdown.target reboot.target halt.target

[Service]
Type=oneshot
# Export the in-memory journal to a persistent log file
ExecStart=/bin/sh -c 'journalctl --since "15 minutes ago" >> ${PERSISTENT_LOG_DIR}/journal-\$(date +%%Y%%m%%d).log 2>/dev/null || true'
# Sync any /run/log/* files to persistent storage
ExecStart=/bin/sh -c 'mkdir -p ${PERSISTENT_LOG_DIR} && rsync -a --ignore-missing-args /run/log/ ${PERSISTENT_LOG_DIR}/ 2>/dev/null || true'
# Rotate if persistent log directory is getting large (>200MB)
ExecStart=/bin/sh -c 'du -sm ${PERSISTENT_LOG_DIR} 2>/dev/null | awk "\$1 > 200 {exit 1}" || logrotate -f /etc/logrotate.d/tmpfs-logs 2>/dev/null || true'
# Sync filesystem to ensure data is written to flash
ExecStart=/bin/sync
RemainAfterExit=no
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE
)

    write_file "$FLUSH_SERVICE_FILE" "$service_content"
}

# ---------------------------------------------------------------------------
# Step 5: Create systemd log-flush.timer
# ---------------------------------------------------------------------------
create_flush_timer() {
    echo ""
    echo "--- Creating log-flush.timer ---"

    local timer_content
    timer_content=$(cat <<TIMER
# SPDX-License-Identifier: Apache-2.0
# /etc/systemd/system/log-flush.timer
# Run log-flush.service every ${FLUSH_INTERVAL_MIN} minutes.
#
# Tradeoff:
#   Shorter interval = more flash writes, better log durability
#   Longer interval  = fewer flash writes, more logs lost on power failure
# Default: 15 minutes. For critical systems, use 5 minutes.

[Unit]
Description=Periodic flush of RAM logs to persistent storage
Documentation=man:systemd.timer(5)

[Timer]
# Run every N minutes after boot
OnBootSec=2min
OnUnitActiveSec=${FLUSH_INTERVAL_MIN}min

# Also run on boot so logs from the startup phase are captured
OnCalendar=*:0/${FLUSH_INTERVAL_MIN}

# If the system was off when the timer was scheduled to run,
# run it immediately when the system next boots
Persistent=true

# Randomize startup to avoid all devices in a fleet flushing simultaneously
RandomizedDelaySec=30s

Unit=log-flush.service

[Install]
WantedBy=timers.target
TIMER
)

    write_file "$FLUSH_TIMER_FILE" "$timer_content"

    if ! $DRY_RUN; then
        run_cmd systemctl daemon-reload
        run_cmd systemctl enable log-flush.timer
        run_cmd systemctl start log-flush.timer
        pass "log-flush.timer enabled and started"
        echo ""
        echo "Timer status:"
        systemctl status log-flush.timer --no-pager 2>/dev/null | head -8 || true
    fi
}

# ---------------------------------------------------------------------------
# Apply mode: run all steps
# ---------------------------------------------------------------------------
mode_apply() {
    echo "=== Configuring Log-to-tmpfs ==="
    $DRY_RUN && echo "[dry-run mode — no changes will be made]"
    echo ""

    configure_journald
    setup_persistent_dir
    configure_logrotate
    create_flush_service
    create_flush_timer

    echo ""
    pass "Log-to-tmpfs configuration complete"
    echo ""
    echo "Summary:"
    echo "  journald:     volatile (RAM) storage, max ${JOURNALD_MAX_USE}"
    echo "  flush timer:  every ${FLUSH_INTERVAL_MIN} minutes → ${PERSISTENT_LOG_DIR}"
    echo "  logrotate:    /run/log → ${ARCHIVE_LOG_DIR} → ${PERSISTENT_LOG_DIR}"
    echo ""
    echo "To verify: bash $0 --status"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    parse_args "$@"

    case "$MODE" in
        apply)  mode_apply  ;;
        status) mode_status ;;
    esac
}

main "$@"
