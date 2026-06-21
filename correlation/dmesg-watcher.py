#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
correlation/dmesg-watcher.py — Stream dmesg and emit structured JSON events.

Watches dmesg for hardware error patterns (EDAC, MCE, AER, CXL) and writes
newline-delimited JSON to stdout so event-correlator.py can process them.

Usage:
    python3 correlation/dmesg-watcher.py [--follow] [--since "10 minutes ago"]
    # Pipe into correlator:
    python3 correlation/dmesg-watcher.py --follow | python3 correlation/event-correlator.py
"""
import argparse
import json
import re
import subprocess
import time

# Pattern registry: (regex, event_type, severity)
PATTERNS = [
    (re.compile(r"EDAC MC(\d+): (\d+) CE"), "edac_ce", "warning"),
    (re.compile(r"EDAC MC(\d+): (\d+) UE"), "edac_ue", "critical"),
    (re.compile(r"mce: \[Hardware Error\]: Machine check events logged"), "mce", "critical"),
    (re.compile(r"pcieport.*AER.*Corrected error"), "pcie_aer_correctable", "info"),
    (re.compile(r"pcieport.*AER.*Uncorrected .Non-Fatal. error"), "pcie_aer_nonfatal", "warning"),
    (re.compile(r"pcieport.*AER.*Uncorrected .Fatal. error"), "pcie_aer_fatal", "critical"),
    (re.compile(r"cxl.*correctable error"), "cxl_ce", "warning"),
    (re.compile(r"cxl.*uncorrectable error"), "cxl_ue", "critical"),
    (re.compile(r"thermal thermal_zone(\d+): critical temperature reached"), "thermal_critical", "critical"),
    (re.compile(r"Under-voltage detected"), "undervoltage", "warning"),
]


def parse_dmesg_line(line: str) -> dict:
    """Parse a dmesg line into a structured event dict (or None if not matched)."""
    # dmesg format: [  123.456789] message
    ts_match = re.match(r"^\[\s*(\d+\.\d+)\]\s*(.*)", line)
    if not ts_match:
        return None

    kernel_ts = float(ts_match.group(1))
    message   = ts_match.group(2).strip()

    for pattern, event_type, severity in PATTERNS:
        m = pattern.search(message)
        if m:
            return {
                "timestamp":   time.time(),
                "kernel_ts":   kernel_ts,
                "event_type":  event_type,
                "severity":    severity,
                "message":     message,
                "groups":      list(m.groups()),
            }
    return None


def stream_dmesg(follow: bool, since: str = None):
    cmd = ["dmesg", "--time-format=reltime"]
    if follow:
        cmd.append("--follow")
    if since:
        cmd += ["--since", since]
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True) as proc:
        for line in proc.stdout:
            yield line.rstrip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--follow", action="store_true", help="Follow dmesg (continuous)")
    parser.add_argument("--since", default=None, help="Start time (e.g. '10 minutes ago')")
    parser.add_argument("--all-lines", action="store_true", help="Emit all lines, not just matches")
    args = parser.parse_args()

    for line in stream_dmesg(args.follow, args.since):
        event = parse_dmesg_line(line)
        if event:
            print(json.dumps(event), flush=True)
        elif args.all_lines and line:
            print(json.dumps({"raw": line, "timestamp": time.time()}), flush=True)


if __name__ == "__main__":
    main()
