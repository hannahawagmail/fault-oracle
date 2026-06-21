# rasdaemon Integration

## Overview

[rasdaemon](https://github.com/mchehab/rasdaemon) is a daemon that subscribes to kernel hardware error trace events and persists them to a SQLite database at `/var/lib/rasdaemon/ras-mc_event.db`. It complements the sysfs-polling approach of the main exporter by:

- Capturing **kernel timestamps** at the moment the MCE fires, not at the next scrape cycle
- Providing **structured fields** (top_layer, mid_layer, lower_layer, address) beyond what EDAC sysfs exposes
- Surviving **counter resets**: the DB is cumulative across reboots (unlike sysfs counters)

## Architecture

```
kernel MCE/EDAC trace events
         │
         ▼
     rasdaemon                       polls every 60s
   /var/lib/rasdaemon/    ──────────────────────────────▶  rasdaemon-exporter.py
     ras-mc_event.db                                              │
     ras-aer_event.db                                             ▼
                                           /var/lib/node_exporter/textfile_collector/
                                                    rasdaemon.prom
                                                         │
                                                         ▼
                                               node_exporter textfile collector
                                                         │
                                                         ▼
                                                    Prometheus
```

## Installation

```bash
# Install rasdaemon
sudo apt-get install rasdaemon        # Debian/Ubuntu
sudo dnf install rasdaemon            # RHEL/Fedora

# Install exporter
sudo install -m 0755 ras/rasdaemon-exporter.py /usr/local/lib/hw-fault-resilience/ras/
sudo install -m 0644 ras/rasdaemon-exporter.service /etc/systemd/system/
sudo install -m 0644 ras/rasdaemon-exporter.timer   /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now rasdaemon-exporter.timer
```

## Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `ras_mc_event_total` | Counter | `type`, `mc`, `top_layer`, `mid_layer` | Memory controller events from rasdaemon |
| `ras_aer_event_total` | Counter | `severity`, `dev_id`, `error_type` | PCIe AER events from rasdaemon |
| `ras_exporter_last_run_timestamp` | Gauge | — | Unix timestamp of last exporter run |
| `ras_exporter_db_events_total` | Gauge | — | Total rows in ras-mc_event.db |

### Relationship to EDAC sysfs metrics

| EDAC sysfs | rasdaemon |
|-----------|-----------|
| `edac_correctable_errors_total{mc,csrow,channel}` | `ras_mc_event_total{type="CE",mc,...}` |
| Polled every scrape (default 10s) | Captured at event time, exported every 60s |
| Counter resets on reboot | Cumulative across reboots |
| No address info | top_layer/mid_layer/lower_layer/address |

Both sources should be monitored. A spike visible in rasdaemon but not EDAC (or vice versa) indicates a timing or subsystem issue.

## Database Schema

```sql
-- ras-mc_event.db
CREATE TABLE mc_event (
    id          INTEGER PRIMARY KEY,
    timestamp   TEXT,      -- ISO8601, captured at MCE time
    err_count   INTEGER,
    error_type  TEXT,      -- CE / UE
    mc          INTEGER,
    top_layer   INTEGER,
    mid_layer   INTEGER,
    lower_layer INTEGER,
    address     INTEGER,
    grain       INTEGER,
    syndrome    INTEGER,
    driver_detail TEXT
);

-- ras-aer_event.db (PCIe AER)
CREATE TABLE aer_event (
    id        INTEGER PRIMARY KEY,
    timestamp TEXT,
    severity  TEXT,      -- correctable / non-fatal / fatal
    dev_id    TEXT,      -- PCI BDF e.g. 0000:01:00.0
    error_type TEXT
);
```

## CLI Reference

```bash
# Export all events to stdout
python3 ras/rasdaemon-exporter.py --output -

# Only events in the last 24 hours
python3 ras/rasdaemon-exporter.py --since 24 --output -

# Custom DB location
python3 ras/rasdaemon-exporter.py --db /mnt/shared/ras-mc_event.db
```

## Grafana

Import `deploy/grafana/rasdaemon-panels.json` into the existing EDAC dashboard. The first panel overlays rasdaemon CE rate on top of EDAC CE rate — divergence between them indicates a timing or reporting gap.
