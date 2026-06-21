#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
annotations/annotation-pusher.py — Push Alertmanager alerts as Grafana annotations.

Polls the Alertmanager API for firing alerts and pushes annotations to Grafana
so they appear as vertical lines on dashboards. Designed to run as a systemd
timer every 60s.

Environment variables:
    GRAFANA_URL      — e.g. http://grafana.monitoring.svc:3000
    GRAFANA_API_KEY  — service account token with annotations:write
    ALERTMANAGER_URL — e.g. http://alertmanager.monitoring.svc:9093

Usage:
    python3 annotations/annotation-pusher.py [--dry-run] [--once]
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request
import urllib.parse

GRAFANA_URL       = os.environ.get("GRAFANA_URL",       "http://grafana.monitoring.svc:3000")
GRAFANA_API_KEY   = os.environ.get("GRAFANA_API_KEY",   "")
ALERTMANAGER_URL  = os.environ.get("ALERTMANAGER_URL",  "http://alertmanager.monitoring.svc:9093")

# Annotation tag prefix for hw-fault events
TAG_PREFIX = "hw-fault"

# Severity → Grafana panel color tag
SEVERITY_TAGS = {
    "critical": "critical",
    "warning":  "warning",
    "info":     "info",
}


def get_firing_alerts(alertmanager_url: str) -> list:
    """Fetch all currently firing alerts from Alertmanager."""
    url = f"{alertmanager_url}/api/v2/alerts?active=true&silenced=false&inhibited=false"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def alert_fingerprint(alert: dict) -> str:
    """Stable fingerprint for deduplication."""
    labels = json.dumps(alert.get("labels", {}), sort_keys=True)
    return hashlib.sha256(labels.encode()).hexdigest()[:16]


def build_annotation(alert: dict) -> dict:
    labels      = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    severity    = labels.get("severity", "info")
    name        = labels.get("alertname", "UnknownAlert")
    instance    = labels.get("instance", "")
    summary     = annotations.get("summary", name)

    tags = [TAG_PREFIX, name, severity]
    if instance:
        tags.append(f"instance:{instance}")

    return {
        "time":  int(time.time() * 1000),
        "tags":  tags,
        "text":  f"<b>{name}</b> ({severity})<br>{summary}",
        "panelId": None,
        "dashboardUID": None,
    }


def push_annotation(grafana_url: str, api_key: str, annotation: dict,
                    dry_run: bool = False) -> bool:
    if dry_run:
        sys.stderr.write(f"[dry-run] annotation: {annotation['text']}\n")
        return True

    payload = json.dumps(annotation).encode()
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    req = urllib.request.Request(
        f"{grafana_url}/api/annotations",
        data=payload, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return "id" in result
    except Exception as exc:
        sys.stderr.write(f"annotation-pusher: failed to push: {exc}\n")
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--grafana",      default=GRAFANA_URL)
    parser.add_argument("--api-key",      default=GRAFANA_API_KEY)
    parser.add_argument("--alertmanager", default=ALERTMANAGER_URL)
    parser.add_argument("--dry-run",      action="store_true")
    parser.add_argument("--once",         action="store_true",
                        help="Run once and exit (default for systemd timer)")
    args = parser.parse_args()

    seen: set = set()

    def run_once():
        try:
            alerts = get_firing_alerts(args.alertmanager)
        except Exception as exc:
            sys.stderr.write(f"annotation-pusher: alertmanager error: {exc}\n")
            return

        pushed = 0
        for alert in alerts:
            fp = alert_fingerprint(alert)
            if fp in seen:
                continue
            seen.add(fp)
            annotation = build_annotation(alert)
            if push_annotation(args.grafana, args.api_key, annotation, args.dry_run):
                pushed += 1

        sys.stderr.write(f"annotation-pusher: pushed {pushed}/{len(alerts)} annotations\n")

    if args.once:
        run_once()
    else:
        while True:
            run_once()
            time.sleep(60)


if __name__ == "__main__":
    main()
