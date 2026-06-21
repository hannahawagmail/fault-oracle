# SPDX-License-Identifier: Apache-2.0
"""
tools/anonymize_log.py — Strip identifying information from kernel logs.

Replaces hostnames, IP addresses, MAC addresses, PCI Bus:Device.Function
addresses, and UUIDs with deterministic placeholders before sharing logs as
bug report attachments.

Usage:
    python3 tools/anonymize_log.py kern.log > sanitised.log
    python3 tools/anonymize_log.py --inplace kern.log
    cat /var/log/kern.log | python3 tools/anonymize_log.py -
"""

import argparse
import hashlib
import re
import sys
from pathlib import Path

# ── Patterns ──────────────────────────────────────────────────────────────

# IPv4 address (not matching version strings like "6.6.20")
_RE_IPV4 = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
)

# IPv6 address (simplified — full form and compressed)
_RE_IPV6 = re.compile(
    r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b"
    r"|::(?:[0-9a-fA-F]{1,4}:){0,6}[0-9a-fA-F]{1,4}"
    r"|(?:[0-9a-fA-F]{1,4}:){1,7}:"
)

# MAC address (colon or hyphen separated)
_RE_MAC = re.compile(
    r"\b(?:[0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}\b"
)

# PCI Bus:Device.Function  e.g. 0000:01:00.0 or 0000:3b:00.0
_RE_BDF = re.compile(
    r"\b[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]\b"
)

# UUID (RFC 4122)
_RE_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
    re.IGNORECASE,
)

# Hostname in syslog prefix:  "Jan 15 08:00:01 HOSTNAME kernel:"
# Only replace the hostname field (word after timestamp, before "kernel:")
_RE_SYSLOG_HOST = re.compile(
    r"^(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(\S+)\s+(kernel:)",
    re.MULTILINE,
)


def _short_hash(value: str, length: int = 6) -> str:
    """Return a short deterministic hex string derived from value."""
    return hashlib.sha256(value.encode()).hexdigest()[:length]


def _make_replacer(prefix: str, memo: dict) -> callable:
    """Return a regex sub callback that maps each unique match to a placeholder."""
    counter = [0]

    def replacer(m: re.Match) -> str:
        key = m.group(0).lower()
        if key not in memo:
            counter[0] += 1
            memo[key] = f"{prefix}{counter[0]:03d}"
        return memo[key]

    return replacer


def anonymize(text: str) -> tuple[str, dict]:
    """
    Anonymize identifying information in text.

    Returns (anonymized_text, substitution_map) where substitution_map
    records what was replaced by what (for audit purposes).
    """
    subs: dict[str, dict] = {
        "hostname": {},
        "ipv4": {},
        "ipv6": {},
        "mac": {},
        "bdf": {},
        "uuid": {},
    }

    # Syslog hostname (first pass — before generic word replacement)
    def replace_syslog_host(m: re.Match) -> str:
        host = m.group(2)
        key = host.lower()
        if key not in subs["hostname"]:
            idx = len(subs["hostname"]) + 1
            subs["hostname"][key] = f"arm64-node-{idx:03d}"
        return f"{m.group(1)} {subs['hostname'][key]} {m.group(3)}"

    text = _RE_SYSLOG_HOST.sub(replace_syslog_host, text)

    # MAC addresses (before IPv6 so colons don't confuse IPv6 pattern)
    text = _RE_MAC.sub(_make_replacer("xx:xx:xx:xx:xx:", subs["mac"]), text)

    # PCI BDFs
    text = _RE_BDF.sub(_make_replacer("0000:aa:bb.", subs["bdf"]), text)

    # UUIDs
    text = _RE_UUID.sub(
        _make_replacer("00000000-0000-0000-0000-", subs["uuid"]), text
    )

    # IPv6
    text = _RE_IPV6.sub(_make_replacer("fd00::", subs["ipv6"]), text)

    # IPv4
    text = _RE_IPV4.sub(_make_replacer("192.0.2.", subs["ipv4"]), text)

    return text, subs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Strip identifying information from kernel log files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "input",
        nargs="?",
        default="-",
        help="Input log file (- or omit for stdin)",
    )
    p.add_argument(
        "--inplace",
        action="store_true",
        help="Edit the file in place (backup saved as <file>.orig)",
    )
    p.add_argument(
        "--summary",
        action="store_true",
        help="Print a summary of substitutions made to stderr",
    )
    args = p.parse_args(argv)

    if args.input == "-":
        text = sys.stdin.read()
    else:
        text = Path(args.input).read_text(errors="replace")

    cleaned, subs = anonymize(text)

    if args.inplace and args.input != "-":
        orig = Path(args.input)
        orig.rename(orig.with_suffix(orig.suffix + ".orig"))
        orig.write_text(cleaned)
        print(f"Anonymized: {orig}  (original → {orig.with_suffix(orig.suffix + '.orig')})",
              file=sys.stderr)
    else:
        sys.stdout.write(cleaned)

    if args.summary:
        total = sum(len(v) for v in subs.values())
        print(f"\n=== Anonymization summary ({total} substitutions) ===", file=sys.stderr)
        for category, mapping in subs.items():
            if mapping:
                print(f"  {category}: {len(mapping)} unique values replaced", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
