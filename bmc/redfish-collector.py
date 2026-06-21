#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
bmc/redfish-collector.py — Modern BMC Redfish API collector.

Supports iDRAC9, iLO5, and OpenBMC. Runs from a management-plane host
(not on the target node) and communicates with the BMC out-of-band.

Environment variables:
    REDFISH_HOST      — BMC IP/hostname (required; exits 0 with up=0 if not set)
    REDFISH_USER      — username (default: admin)
    REDFISH_PASSWORD  — password (read from env or /var/run/secrets/redfish-password)
    REDFISH_INSECURE  — skip TLS verify if "true" (default: "false")

Metrics emitted (Prometheus text format to stdout):
    redfish_system_health{component}           gauge  1=OK, 0=Warning, -1=Critical
    redfish_sel_entry_total{severity,category} counter
    redfish_temperature_celsius{sensor}        gauge
    redfish_fan_rpm{fan}                       gauge
    redfish_psu_input_watts{psu}               gauge
    redfish_psu_up{psu}                        gauge  (1 if State="Enabled")
    redfish_collector_up                       gauge  (0 if host not set or all fail)
    redfish_collector_last_run_timestamp       gauge
"""

import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_STATE_FILE = Path("/var/lib/hw-fault-bmc/redfish-state.json")

# Redfish health string → numeric gauge value
HEALTH_MAP = {
    "ok": 1,
    "warning": 0,
    "critical": -1,
}

# Redfish Severity → simplified label
SEL_SEVERITY_MAP = {
    "critical": "critical",
    "warning": "warning",
}

# Message category patterns (from MessageId / EventType prefix)
SEL_CATEGORY_PATTERNS = [
    ("cpu",     ["cpu", "processor"]),
    ("memory",  ["memory", "mem", "dram"]),
    ("power",   ["power", "psu", "voltage"]),
    ("thermal", ["thermal", "temp", "fan"]),
    ("storage", ["storage", "disk", "drive", "raid"]),
    ("network", ["network", "nic", "ethernet"]),
]


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def redfish_get(host: str, path: str, user: str, password: str,
                verify: bool = True) -> dict | None:
    """GET a Redfish endpoint, return parsed JSON or None on error."""
    url = f"https://{host}{path}"
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
        },
    )
    ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# State helpers (SEL deduplication)
# ---------------------------------------------------------------------------

def load_redfish_state(path: Path) -> dict:
    """Return state dict from file, or {} if absent/corrupt."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_redfish_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------

def sanitize_label(val: str) -> str:
    """Remove characters that are invalid in Prometheus label values."""
    return val.replace('"', "'").replace('\\', '/').replace('\n', ' ').strip() or "unknown"


def health_to_int(health_str: str) -> int:
    """Map Redfish health string to numeric gauge: OK→1, Warning→0, Critical→-1."""
    return HEALTH_MAP.get((health_str or "").lower(), 0)


def sel_category_for(message_id: str) -> str:
    """Map a Redfish MessageId to a category label."""
    lower = (message_id or "").lower()
    for category, keywords in SEL_CATEGORY_PATTERNS:
        for kw in keywords:
            if kw in lower:
                return category
    return "other"


# ---------------------------------------------------------------------------
# System endpoint — find the right path (iDRAC vs iLO/OpenBMC)
# ---------------------------------------------------------------------------

def get_system_path(host: str, user: str, password: str,
                    verify: bool = True) -> str | None:
    """
    Try iDRAC-style /redfish/v1/Systems/System.Embedded.1/ first,
    fall back to /redfish/v1/Systems/1/ for iLO/OpenBMC.
    Returns the path string if data was found, else None.
    """
    for path in [
        "/redfish/v1/Systems/System.Embedded.1/",
        "/redfish/v1/Systems/1/",
    ]:
        data = redfish_get(host, path, user, password, verify=verify)
        if data is not None:
            return path
    return None


# ---------------------------------------------------------------------------
# Collection functions
# ---------------------------------------------------------------------------

def collect_system_health(host: str, system_path: str, user: str,
                          password: str, verify: bool = True) -> list[dict]:
    """
    Collect system health metrics from the Systems endpoint.

    Returns list of dicts: {component, value}
    """
    data = redfish_get(host, system_path, user, password, verify=verify)
    if not data:
        return []

    results = []

    # MemorySummary.Status.Health
    try:
        health = data["MemorySummary"]["Status"]["Health"]
        results.append({"component": "memory", "value": health_to_int(health)})
    except (KeyError, TypeError):
        pass

    # ProcessorSummary.Status.Health
    try:
        health = data["ProcessorSummary"]["Status"]["Health"]
        results.append({"component": "processor", "value": health_to_int(health)})
    except (KeyError, TypeError):
        pass

    return results


def collect_sel_entries(host: str, system_path: str, user: str,
                        password: str, verify: bool, seen_ids: set) -> list[dict]:
    """
    Collect new SEL entries (Critical or Warning), deduplicating by Id.

    Returns list of dicts: {id, severity, category}
    Updates seen_ids in-place for entries that are new.
    """
    # Build the SEL log path relative to the system path.
    # Strip trailing slash then append log path.
    base = system_path.rstrip("/")
    sel_path = f"{base}/LogServices/Sel/Entries/"

    data = redfish_get(host, sel_path, user, password, verify=verify)
    if not data:
        return []

    entries = data.get("Members", [])
    new_entries = []
    for entry in entries:
        entry_id = str(entry.get("Id", ""))
        severity = entry.get("Severity", "")
        if severity not in ("Critical", "Warning"):
            continue
        if entry_id in seen_ids:
            continue
        seen_ids.add(entry_id)
        message_id = entry.get("MessageId", "")
        category = sel_category_for(message_id)
        new_entries.append({
            "id": entry_id,
            "severity": severity.lower(),
            "category": category,
        })
    return new_entries


def collect_thermal(host: str, user: str, password: str,
                    verify: bool = True) -> dict[str, list]:
    """
    Collect temperature and fan readings from Chassis Thermal endpoint.

    Returns {"temperatures": [...], "fans": [...]}
    """
    path = "/redfish/v1/Chassis/System.Embedded.1/Thermal"
    data = redfish_get(host, path, user, password, verify=verify)
    if not data:
        return {"temperatures": [], "fans": []}

    temperatures = []
    for t in data.get("Temperatures", []):
        reading = t.get("ReadingCelsius")
        name = t.get("Name", "unknown")
        if reading is not None:
            temperatures.append({"sensor": name, "value": reading})

    fans = []
    for f in data.get("Fans", []):
        reading = f.get("Reading")
        name = f.get("Name", "unknown")
        if reading is not None:
            fans.append({"fan": name, "value": reading})

    return {"temperatures": temperatures, "fans": fans}


def collect_power(host: str, user: str, password: str,
                  verify: bool = True) -> list[dict]:
    """
    Collect PSU readings from Chassis Power endpoint.

    Returns list of dicts: {psu, watts, up}
    """
    path = "/redfish/v1/Chassis/System.Embedded.1/Power"
    data = redfish_get(host, path, user, password, verify=verify)
    if not data:
        return []

    psus = []
    for psu in data.get("PowerSupplies", []):
        name = psu.get("Name", psu.get("MemberId", "unknown"))
        watts = psu.get("PowerInputWatts")
        try:
            state = psu["Status"]["State"]
        except (KeyError, TypeError):
            state = "Unknown"
        up = 1 if state == "Enabled" else 0
        if watts is not None:
            psus.append({"psu": name, "watts": watts, "up": up})
    return psus


# ---------------------------------------------------------------------------
# Prometheus output
# ---------------------------------------------------------------------------

def emit_metrics(
    host: str,
    health_data: list[dict],
    sel_entries: list[dict],
    thermal_data: dict[str, list],
    power_data: list[dict],
    collector_up: int,
) -> str:
    lines: list[str] = []
    now = time.time()

    # ---- redfish_system_health -----------------------------------------------
    lines.append("# HELP redfish_system_health Redfish system component health: 1=OK, 0=Warning, -1=Critical.")
    lines.append("# TYPE redfish_system_health gauge")
    for item in health_data:
        comp = sanitize_label(item["component"])
        lines.append(f'redfish_system_health{{component="{comp}"}} {item["value"]}')

    # ---- redfish_sel_entry_total ---------------------------------------------
    lines.append("# HELP redfish_sel_entry_total Total new Redfish SEL entries by severity and category.")
    lines.append("# TYPE redfish_sel_entry_total counter")
    for entry in sel_entries:
        sev = sanitize_label(entry["severity"])
        cat = sanitize_label(entry["category"])
        lines.append(f'redfish_sel_entry_total{{severity="{sev}",category="{cat}"}} 1')

    # ---- redfish_temperature_celsius -----------------------------------------
    lines.append("# HELP redfish_temperature_celsius Temperature reading in Celsius from Redfish Thermal.")
    lines.append("# TYPE redfish_temperature_celsius gauge")
    for t in thermal_data.get("temperatures", []):
        sensor = sanitize_label(t["sensor"])
        lines.append(f'redfish_temperature_celsius{{sensor="{sensor}"}} {t["value"]}')

    # ---- redfish_fan_rpm -----------------------------------------------------
    lines.append("# HELP redfish_fan_rpm Fan speed in RPM from Redfish Thermal.")
    lines.append("# TYPE redfish_fan_rpm gauge")
    for f in thermal_data.get("fans", []):
        fan = sanitize_label(f["fan"])
        lines.append(f'redfish_fan_rpm{{fan="{fan}"}} {f["value"]}')

    # ---- redfish_psu_input_watts / redfish_psu_up ----------------------------
    lines.append("# HELP redfish_psu_input_watts PSU input power in Watts from Redfish Power.")
    lines.append("# TYPE redfish_psu_input_watts gauge")
    for p in power_data:
        psu = sanitize_label(p["psu"])
        lines.append(f'redfish_psu_input_watts{{psu="{psu}"}} {p["watts"]}')

    lines.append("# HELP redfish_psu_up 1 if PSU State is Enabled, 0 otherwise.")
    lines.append("# TYPE redfish_psu_up gauge")
    for p in power_data:
        psu = sanitize_label(p["psu"])
        lines.append(f'redfish_psu_up{{psu="{psu}"}} {p["up"]}')

    # ---- redfish_collector_up ------------------------------------------------
    lines.append("# HELP redfish_collector_up 1 if Redfish collector ran successfully, 0 otherwise.")
    lines.append("# TYPE redfish_collector_up gauge")
    lines.append(f"redfish_collector_up {collector_up}")

    # ---- redfish_collector_last_run_timestamp --------------------------------
    lines.append("# HELP redfish_collector_last_run_timestamp Unix timestamp of last Redfish collector run.")
    lines.append("# TYPE redfish_collector_last_run_timestamp gauge")
    lines.append(f"redfish_collector_last_run_timestamp {now:.3f}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    host = os.environ.get("REDFISH_HOST", "").strip()
    if not host:
        # No host configured — emit up=0 and exit cleanly.
        output = emit_metrics("", [], [], {"temperatures": [], "fans": []}, [], 0)
        sys.stdout.write(output)
        return 0

    user = os.environ.get("REDFISH_USER", "admin")

    # Password: env var takes priority, then secret file.
    password = os.environ.get("REDFISH_PASSWORD", "")
    if not password:
        secret_path = Path("/var/run/secrets/redfish-password")
        try:
            password = secret_path.read_text().strip()
        except Exception:
            password = ""

    verify = os.environ.get("REDFISH_INSECURE", "false").lower() != "true"

    state_path = Path(os.environ.get("REDFISH_STATE_FILE",
                                     str(DEFAULT_STATE_FILE)))
    state = load_redfish_state(state_path)
    seen_ids: set = set(state.get("seen_sel_ids", []))

    # Discover system path.
    system_path = get_system_path(host, user, password, verify=verify)
    collector_up = 0

    health_data: list[dict] = []
    sel_entries: list[dict] = []
    thermal_data: dict[str, list] = {"temperatures": [], "fans": []}
    power_data: list[dict] = []

    if system_path:
        health_data  = collect_system_health(host, system_path, user, password, verify=verify)
        sel_entries  = collect_sel_entries(host, system_path, user, password, verify, seen_ids)
        thermal_data = collect_thermal(host, user, password, verify=verify)
        power_data   = collect_power(host, user, password, verify=verify)

        # Consider up if at least one endpoint returned data.
        if health_data or thermal_data["temperatures"] or thermal_data["fans"] or power_data:
            collector_up = 1

    output = emit_metrics(host, health_data, sel_entries, thermal_data, power_data, collector_up)
    sys.stdout.write(output)

    # Persist updated SEL state.
    state["seen_sel_ids"] = list(seen_ids)
    try:
        save_redfish_state(state_path, state)
    except OSError as exc:
        sys.stderr.write(f"redfish-collector: could not write state file: {exc}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
