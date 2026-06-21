#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
correlation/event-correlator.py — Correlate dmesg events with Prometheus metrics.

Reads newline-delimited JSON from stdin (from dmesg-watcher.py), correlates
events that occur close together in time (within CORRELATION_WINDOW_S), and
pushes Grafana annotations for correlated bursts.

Correlation rules:
  1. If edac_ce + mce within 10s on same node → "memory_multi_source" burst
  2. If pcie_aer_fatal + edac_ue within 30s → "pcie_memory_cascade"
  3. Three or more events of same type within 60s → "event_storm"

Usage:
    python3 correlation/dmesg-watcher.py --follow \
        | python3 correlation/event-correlator.py [--grafana URL] [--dry-run]
"""
import argparse
import json
import sys
import time
import urllib.request

CORRELATION_WINDOW_S = 60


class EventBuffer:
    """Sliding window event buffer that automatically expires old events.

    Holds dmesg-watcher JSON events for a configurable time window and provides
    typed access methods used by detect_correlations() to apply correlation rules.
    Expiry is lazy — triggered on every write and read — so no background thread
    is required.
    """

    def __init__(self, window_s: float = CORRELATION_WINDOW_S):
        """Initialise the buffer with the given sliding window size in seconds."""
        self.window_s = window_s
        self._events: list = []

    def add(self, event: dict):
        """Append an event to the buffer and expire events older than window_s."""
        self._events.append(event)
        self._expire()

    def _expire(self):
        """Remove events whose timestamp falls outside the current sliding window."""
        cutoff = time.time() - self.window_s
        self._events = [e for e in self._events if e.get("timestamp", 0) >= cutoff]

    def events_of_type(self, event_type: str) -> list:
        """Return all unexpired events matching the given event_type string."""
        self._expire()
        return [e for e in self._events if e["event_type"] == event_type]

    def recent(self) -> list:
        """Return a snapshot list of all unexpired events in insertion order."""
        self._expire()
        return list(self._events)

    def count_by_type(self) -> dict:
        """Return {event_type: count} for all unexpired events in the window."""
        self._expire()
        counter: dict = {}
        for e in self._events:
            counter[e["event_type"]] = counter.get(e["event_type"], 0) + 1
        return counter


def detect_correlations(buf: EventBuffer) -> list:
    """Return list of detected correlation dicts."""
    correlations = []
    by_type = buf.count_by_type()

    # Rule 1: memory multi-source (EDAC CE + MCE)
    if by_type.get("edac_ce", 0) > 0 and by_type.get("mce", 0) > 0:
        correlations.append({
            "type": "memory_multi_source",
            "severity": "critical",
            "description": f"EDAC CE ({by_type['edac_ce']}) + MCE ({by_type['mce']}) within {CORRELATION_WINDOW_S}s",
        })

    # Rule 2: PCIe + memory cascade
    if by_type.get("pcie_aer_fatal", 0) > 0 and by_type.get("edac_ue", 0) > 0:
        correlations.append({
            "type": "pcie_memory_cascade",
            "severity": "critical",
            "description": "PCIe AER fatal + EDAC UE — possible interconnect failure",
        })

    # Rule 3: event storm (≥3 of same type)
    for etype, cnt in by_type.items():
        if cnt >= 3:
            correlations.append({
                "type": "event_storm",
                "severity": "warning",
                "description": f"{etype} storm: {cnt} events in {CORRELATION_WINDOW_S}s",
                "event_type": etype,
                "count": cnt,
            })

    return correlations


def push_grafana_annotation(grafana_url: str, api_key: str, tag: str,
                             text: str, dry_run: bool = False):
    """Push an annotation to Grafana."""
    payload = json.dumps({
        "time":     int(time.time() * 1000),
        "tags":     ["hw-fault", tag],
        "text":     text,
    }).encode()
    url = f"{grafana_url}/api/annotations"
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if dry_run:
        sys.stderr.write(f"[dry-run] annotation: {text}\n")
        return
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as exc:
        sys.stderr.write(f"correlator: annotation failed: {exc}\n")


def main():
    """Entry point: read newline-delimited JSON events from stdin and correlate them.

    Reads dmesg-watcher output from stdin (pipe or redirect) and pushes
    Grafana annotations for any detected correlation patterns. The seen_correlations
    set prevents duplicate annotations for the same correlation type within a
    window; it is cleared when it exceeds 100 entries so long-running instances
    do not accumulate memory indefinitely.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--grafana",    default="http://grafana.monitoring.svc:3000")
    parser.add_argument("--api-key",    default="")
    parser.add_argument("--dry-run",    action="store_true")
    parser.add_argument("--window",     type=float, default=CORRELATION_WINDOW_S)
    args = parser.parse_args()

    buf = EventBuffer(window_s=args.window)
    seen_correlations: set = set()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if "event_type" not in event:
            continue

        buf.add(event)
        sys.stderr.write(f"[event] {event['event_type']} @ {event.get('kernel_ts', '?')}\n")

        correlations = detect_correlations(buf)
        for corr in correlations:
            key = (corr["type"], corr.get("event_type", ""))
            if key not in seen_correlations:
                seen_correlations.add(key)
                sys.stderr.write(f"[correlation] {corr['type']}: {corr['description']}\n")
                push_grafana_annotation(
                    args.grafana, args.api_key,
                    corr["type"], corr["description"],
                    dry_run=args.dry_run
                )

        # Clear seen correlations periodically so recurring storms re-fire
        if len(seen_correlations) > 100:
            seen_correlations.clear()


if __name__ == "__main__":
    main()
