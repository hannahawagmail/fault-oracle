#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
ras/rasdaemon-exporter.py — Export rasdaemon SQLite events as Prometheus textfile metrics.

Reads /var/lib/rasdaemon/ras-mc_event.db (and ras-aer_event.db if present),
emits Prometheus textfile format to stdout or --output file.

Usage:
    python3 ras/rasdaemon-exporter.py [--db PATH] [--output PATH] [--since HOURS]

Writes to: /var/lib/node_exporter/textfile_collector/rasdaemon.prom (default)

Metrics:
    ras_mc_event_total{type, mc, top_layer, mid_layer}   — memory controller events
    ras_aer_event_total{severity, dev_id, error_type}     — PCIe AER events
    ras_exporter_last_run_timestamp                        — Unix timestamp of last run
    ras_exporter_db_events_total                           — total events in DB
"""
import argparse
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

log = logging.getLogger("rasdaemon-exporter")


def log_info(msg: str):
    log.info(msg)
    sys.stderr.write(msg + "\n")

DEFAULT_DB   = Path("/var/lib/rasdaemon/ras-mc_event.db")
DEFAULT_OUT  = Path("/var/lib/node_exporter/textfile_collector/rasdaemon.prom")
DEFAULT_AER_DB = Path("/var/lib/rasdaemon/ras-aer_event.db")


def sanitize_label(val: str) -> str:
    """Remove characters that are invalid in Prometheus label values."""
    return val.replace('"', "'").replace('\\', '/').replace('\n', ' ').strip() or "unknown"


def query_mc_events(conn: sqlite3.Connection, since_epoch: float) -> dict:
    """Return {(type, mc, top_layer, mid_layer): count}."""
    try:
        cursor = conn.execute("""
            SELECT error_type, mc, top_layer, mid_layer, COUNT(*) as cnt
            FROM mc_event
            WHERE timestamp >= ?
            GROUP BY error_type, mc, top_layer, mid_layer
        """, (since_epoch,))
        counts = {}
        for row in cursor:
            key = (
                sanitize_label(str(row[0])),
                sanitize_label(str(row[1])),
                sanitize_label(str(row[2])),
                sanitize_label(str(row[3])),
            )
            counts[key] = row[4]
        return counts
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
        log_info(f"SQLite error querying {conn}: {e}")
        return {}


def query_aer_events(conn: sqlite3.Connection, since_epoch: float) -> dict:
    """Return {(severity, dev_id, error_type): count}."""
    try:
        cursor = conn.execute("""
            SELECT severity, dev_id, error_type, COUNT(*) as cnt
            FROM aer_event
            WHERE timestamp >= ?
            GROUP BY severity, dev_id, error_type
        """, (since_epoch,))
        counts = {}
        for row in cursor:
            key = (
                sanitize_label(str(row[0])),
                sanitize_label(str(row[1])),
                sanitize_label(str(row[2])),
            )
            counts[key] = row[3]
        return counts
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
        log_info(f"SQLite error querying {conn}: {e}")
        return {}   # table may not exist


def emit_metrics(mc_counts: dict, aer_counts: dict, total_events: int) -> str:
    lines = []
    now = time.time()

    lines.append("# HELP ras_mc_event_total Total rasdaemon memory controller events.")
    lines.append("# TYPE ras_mc_event_total counter")
    for (etype, mc, top, mid), cnt in sorted(mc_counts.items()):
        lines.append(
            f'ras_mc_event_total{{type="{etype}",mc="{mc}",top_layer="{top}",mid_layer="{mid}"}} {cnt}'
        )

    lines.append("# HELP ras_aer_event_total Total rasdaemon PCIe AER events.")
    lines.append("# TYPE ras_aer_event_total counter")
    for (sev, dev_id, etype), cnt in sorted(aer_counts.items()):
        lines.append(
            f'ras_aer_event_total{{severity="{sev}",dev_id="{dev_id}",error_type="{etype}"}} {cnt}'
        )

    lines.append("# HELP ras_exporter_last_run_timestamp Unix timestamp of the last rasdaemon exporter run.")
    lines.append("# TYPE ras_exporter_last_run_timestamp gauge")
    lines.append(f"ras_exporter_last_run_timestamp {now:.3f}")

    lines.append("# HELP ras_exporter_db_events_total Total events in rasdaemon database.")
    lines.append("# TYPE ras_exporter_db_events_total gauge")
    lines.append(f"ras_exporter_db_events_total {total_events}")

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help="Path to ras-mc_event.db")
    parser.add_argument("--aer-db", type=Path, default=DEFAULT_AER_DB,
                        help="Path to ras-aer_event.db (optional)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT,
                        help="Output .prom file (- for stdout)")
    parser.add_argument("--since", type=float, default=0,
                        help="Only include events from the last N hours (0 = all)")
    args = parser.parse_args()

    if not args.db.exists():
        # Emit empty metrics so node_exporter doesn't error on missing file
        sys.stderr.write(f"rasdaemon-exporter: DB not found: {args.db}\n")
        output = emit_metrics({}, {}, 0)
    else:
        since_epoch = time.time() - args.since * 3600 if args.since > 0 else 0.0

        mc_conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        mc_counts = query_mc_events(mc_conn, since_epoch)
        total = mc_conn.execute("SELECT COUNT(*) FROM mc_event").fetchone()[0]
        mc_conn.close()

        aer_counts = {}
        if args.aer_db.exists():
            aer_conn = sqlite3.connect(f"file:{args.aer_db}?mode=ro", uri=True)
            aer_counts = query_aer_events(aer_conn, since_epoch)
            aer_conn.close()

        output = emit_metrics(mc_counts, aer_counts, total)

    if str(args.output) == "-":
        sys.stdout.write(output)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        sys.stderr.write(f"rasdaemon-exporter: wrote {len(output)} bytes to {args.output}\n")


if __name__ == "__main__":
    main()
