// SPDX-License-Identifier: Apache-2.0
/*
 * hw-fault-monitor.c — Example daemon with systemd watchdog integration
 *
 * Demonstrates:
 *   1. READY=1 notification after initialization
 *   2. Periodic WATCHDOG=1 keepalive ping
 *   3. STATUS= messages for systemd status
 *   4. Graceful SIGTERM handling
 *
 * This implementation uses the NOTIFY_SOCKET environment variable directly
 * (a datagram Unix socket), without depending on libsystemd. This keeps the
 * binary small and portable to minimal embedded rootfs images.
 *
 * Compile: gcc -O2 -Wall -Wextra -o hw-fault-monitor hw-fault-monitor.c
 * Syntax check only: gcc -fsyntax-only hw-fault-monitor.c
 *
 * Behavior:
 *   - Reads /sys/devices/system/edac/mc0/ce_count every 10 seconds
 *   - If ce_count delta since last check > 100: sends STATUS=WARNING message
 *   - If ce_count delta since last check > 1000: does NOT send WATCHDOG=1
 *     (intentionally lets systemd's watchdog fire → restart)
 *   - Sends WATCHDOG=1 every WATCHDOG_USEC/2 microseconds (recommended rate)
 *   - On SIGTERM: sends STOPPING=1, exits 0
 */

#include <errno.h>
#include <stddef.h>
#include <fcntl.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

/* -------------------------------------------------------------------------
 * Configuration constants
 * ------------------------------------------------------------------------- */
#define EDAC_CE_COUNT_PATH   "/sys/devices/system/edac/mc0/ce_count"
#define POLL_INTERVAL_SEC    10      /* How often to check EDAC, in seconds */
#define CE_WARN_THRESHOLD    100     /* CE delta triggering STATUS= warning */
#define CE_CRITICAL_THRESHOLD 1000  /* CE delta that suppresses watchdog ping
                                     * → intentionally lets WDT fire → restart */
#define STATUS_MSG_MAXLEN    256

/* -------------------------------------------------------------------------
 * Global state
 * ------------------------------------------------------------------------- */
static volatile sig_atomic_t g_terminate = 0;
static long long g_watchdog_usec = 0;   /* From WATCHDOG_USEC env variable */

/* -------------------------------------------------------------------------
 * Signal handler — set flag, do nothing else (async-signal-safe)
 * ------------------------------------------------------------------------- */
static void handle_sigterm(int sig) {
    (void)sig;
    g_terminate = 1;
}

/* -------------------------------------------------------------------------
 * sd_notify_manual() — Send a notification message to systemd's NOTIFY_SOCKET
 *
 * systemd sets NOTIFY_SOCKET to a path (or abstract socket path prefixed with
 * '@') for the service's notify socket. We open a datagram socket, connect
 * to that path, and send the message string.
 *
 * Returns 1 on success, 0 if NOTIFY_SOCKET is unset (not running under
 * systemd — this is not an error), -1 on send failure.
 *
 * Reference: sd_notify(3) man page, systemd source notify.c
 * ------------------------------------------------------------------------- */
static int sd_notify_manual(const char *msg) {
    const char *sock_path;
    int fd;
    struct sockaddr_un addr;
    ssize_t sent;
    size_t msg_len;
    size_t addr_len;

    sock_path = getenv("NOTIFY_SOCKET");
    if (!sock_path || sock_path[0] == '\0') {
        /* Not running under systemd — silently ignore */
        return 0;
    }

    fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        perror("sd_notify_manual: socket");
        return -1;
    }

    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;

    if (sock_path[0] == '@') {
        /* Abstract namespace socket: replace '@' with NUL byte */
        strncpy(addr.sun_path + 1, sock_path + 1, sizeof(addr.sun_path) - 2);
        /* Length includes the leading NUL byte for abstract sockets */
        addr_len = offsetof(struct sockaddr_un, sun_path) + 1 + strlen(sock_path + 1);
    } else {
        strncpy(addr.sun_path, sock_path, sizeof(addr.sun_path) - 1);
        addr_len = sizeof(addr);
    }

    msg_len = strlen(msg);
    sent = sendto(fd, msg, msg_len, MSG_NOSIGNAL,
                  (struct sockaddr *)&addr, (socklen_t)addr_len);
    close(fd);

    if (sent < 0) {
        perror("sd_notify_manual: sendto");
        return -1;
    }
    return 1;
}

/* -------------------------------------------------------------------------
 * sd_notify_status() — Convenience wrapper for STATUS= messages
 *
 * Formats "STATUS=<message>" and sends it. The status string appears in
 * `systemctl status hw-fault-monitor` under the "Status:" line.
 * ------------------------------------------------------------------------- */
static void sd_notify_status(const char *fmt, ...) {
    char buf[STATUS_MSG_MAXLEN];
    char msg[STATUS_MSG_MAXLEN + 8]; /* "STATUS=" prefix + message */
    va_list ap;

    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);

    snprintf(msg, sizeof(msg), "STATUS=%s", buf);
    sd_notify_manual(msg);
}

/* -------------------------------------------------------------------------
 * read_edac_ce_count() — Read correctable error count from EDAC sysfs
 *
 * Returns the current cumulative CE count, or -1 if the file cannot be read
 * (e.g., no ECC memory controller on this platform).
 * ------------------------------------------------------------------------- */
static long long read_edac_ce_count(void) {
    FILE *f;
    long long count = -1;

    f = fopen(EDAC_CE_COUNT_PATH, "r");
    if (!f) {
        /* Not an error on non-ECC boards; caller decides what to do */
        return -1;
    }

    if (fscanf(f, "%lld", &count) != 1) {
        count = -1;
    }
    fclose(f);
    return count;
}

/* -------------------------------------------------------------------------
 * monotonic_usec() — Return monotonic clock time in microseconds
 *
 * Uses CLOCK_MONOTONIC so the result is not affected by system clock changes
 * (NTP steps, daylight saving, etc.).
 * ------------------------------------------------------------------------- */
static long long monotonic_usec(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        /* Fallback: should never happen on Linux */
        return 0;
    }
    return (long long)ts.tv_sec * 1000000LL + (long long)ts.tv_nsec / 1000LL;
}

/* -------------------------------------------------------------------------
 * main()
 * ------------------------------------------------------------------------- */
int main(void) {
    struct sigaction sa;
    long long last_watchdog_ping_usec;
    long long last_poll_usec;
    long long now_usec;
    long long watchdog_interval_usec;
    long long prev_ce_count;
    long long curr_ce_count;
    long long ce_delta;
    int suppress_watchdog;
    const char *wdt_env;
    char status_buf[STATUS_MSG_MAXLEN];

    /* ---- Signal handling ---- */
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = handle_sigterm;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT,  &sa, NULL);

    /* ---- Read WATCHDOG_USEC from systemd ---- */
    wdt_env = getenv("WATCHDOG_USEC");
    if (wdt_env && wdt_env[0] != '\0') {
        g_watchdog_usec = atoll(wdt_env);
    }

    /*
     * Best practice: ping at half the watchdog interval so that even if one
     * ping is delayed (e.g., by a slow sysfs read), the next ping still
     * arrives before the deadline.
     */
    if (g_watchdog_usec > 0) {
        watchdog_interval_usec = g_watchdog_usec / 2;
    } else {
        /* Not running under systemd watchdog; use a default so the loop works */
        watchdog_interval_usec = 15000000LL; /* 15 seconds */
    }

    /* ---- Initialization ---- */
    fprintf(stderr, "hw-fault-monitor: starting (watchdog_usec=%lld)\n",
            g_watchdog_usec);

    /*
     * Perform any slow initialization here (open devices, allocate memory,
     * establish connections). For this example, we just check EDAC availability.
     */
    prev_ce_count = read_edac_ce_count();
    if (prev_ce_count < 0) {
        fprintf(stderr, "hw-fault-monitor: EDAC mc0 not available; CE monitoring disabled\n");
        sd_notify_status("Running (EDAC not available on this platform)");
    } else {
        fprintf(stderr, "hw-fault-monitor: EDAC mc0 initial CE count: %lld\n", prev_ce_count);
        sd_notify_status("Running, EDAC ce_count=%lld", prev_ce_count);
    }

    /*
     * READY=1: tells systemd that initialization is complete.
     * With Type=notify in the service unit, systemd waits for this before
     * considering the service "active" and starting dependent units.
     * Sending READY=1 too early (before init is done) defeats the purpose.
     */
    sd_notify_manual("READY=1");

    now_usec = monotonic_usec();
    last_watchdog_ping_usec = now_usec;
    last_poll_usec = now_usec;

    /* ---- Main monitoring loop ---- */
    while (!g_terminate) {
        now_usec = monotonic_usec();
        suppress_watchdog = 0;

        /* Poll EDAC every POLL_INTERVAL_SEC seconds */
        if ((now_usec - last_poll_usec) >= (long long)POLL_INTERVAL_SEC * 1000000LL) {
            last_poll_usec = now_usec;

            curr_ce_count = read_edac_ce_count();
            if (curr_ce_count >= 0 && prev_ce_count >= 0) {
                ce_delta = curr_ce_count - prev_ce_count;
                prev_ce_count = curr_ce_count;

                if (ce_delta > CE_CRITICAL_THRESHOLD) {
                    /*
                     * Extremely high CE rate — memory may be failing.
                     * Do NOT send WATCHDOG=1 this iteration. systemd will
                     * kill and restart us if this persists past WatchdogSec.
                     * The restart gives the init system a chance to log a
                     * kernel message and reset hardware counters.
                     */
                    fprintf(stderr,
                            "hw-fault-monitor: CRITICAL CE delta %lld > %d; "
                            "suppressing watchdog ping to trigger restart\n",
                            ce_delta, CE_CRITICAL_THRESHOLD);
                    snprintf(status_buf, sizeof(status_buf),
                             "CRITICAL: CE delta=%lld; allowing watchdog restart",
                             ce_delta);
                    sd_notify_status("%s", status_buf);
                    suppress_watchdog = 1;

                } else if (ce_delta > CE_WARN_THRESHOLD) {
                    /*
                     * Elevated CE rate — concerning but not yet critical.
                     * Continue operation, emit STATUS= warning.
                     */
                    fprintf(stderr,
                            "hw-fault-monitor: WARNING CE delta %lld > %d\n",
                            ce_delta, CE_WARN_THRESHOLD);
                    snprintf(status_buf, sizeof(status_buf),
                             "WARNING: high CE rate detected, delta=%lld", ce_delta);
                    sd_notify_status("%s", status_buf);

                } else {
                    /* Normal operation */
                    sd_notify_status("OK, EDAC ce_count=%lld (delta=%lld per %ds)",
                                     curr_ce_count, ce_delta, POLL_INTERVAL_SEC);
                }
            }
        }

        /* Send WATCHDOG=1 ping at half the watchdog interval */
        if (!suppress_watchdog &&
            (now_usec - last_watchdog_ping_usec) >= watchdog_interval_usec) {
            last_watchdog_ping_usec = now_usec;
            sd_notify_manual("WATCHDOG=1");
        }

        /* Sleep 1 second between loop iterations (avoids busy-waiting) */
        sleep(1);
    }

    /* ---- Graceful shutdown ---- */
    fprintf(stderr, "hw-fault-monitor: received termination signal, shutting down\n");

    /*
     * STOPPING=1: tells systemd we are intentionally exiting. systemd will
     * not count this as an unexpected exit for Restart= purposes.
     * Always send this before exiting on SIGTERM.
     */
    sd_notify_manual("STOPPING=1");

    /* Perform cleanup (close file handles, flush state, etc.) */
    fprintf(stderr, "hw-fault-monitor: shutdown complete\n");
    return 0;
}
