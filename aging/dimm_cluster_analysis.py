"""Detect DIMM failure clustering by vendor/lot/date-code."""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass


@dataclass
class DIMMRecord:
    serial: str
    vendor: str
    part_number: str
    date_code: str
    node: str
    slot: str
    ce_count: int
    ue_count: int
    failure_probability: float


@dataclass
class ClusterAlert:
    cluster_key: str
    cluster_type: str
    affected_count: int
    fleet_average_rate: float
    cluster_rate: float
    severity: str


THRESHOLD = 3.0


def _severity(ratio: float) -> str:
    if ratio >= 10:
        return "critical"
    if ratio >= 5:
        return "high"
    return "medium"


def _fleet_avg(dimms: list[DIMMRecord]) -> float:
    if not dimms:
        return 0.0
    return sum(d.failure_probability for d in dimms) / len(dimms)


def _check_group(groups: dict[str, list[DIMMRecord]], fleet_avg: float, cluster_type: str) -> list[ClusterAlert]:
    alerts: list[ClusterAlert] = []
    if fleet_avg == 0.0:
        return alerts
    for key, members in groups.items():
        rate = sum(d.failure_probability for d in members) / len(members)
        ratio = rate / fleet_avg
        if ratio >= THRESHOLD:
            alerts.append(ClusterAlert(
                cluster_key=key, cluster_type=cluster_type,
                affected_count=len(members), fleet_average_rate=fleet_avg,
                cluster_rate=rate, severity=_severity(ratio),
            ))
    return alerts


def analyze_clusters(dimms: list[DIMMRecord]) -> list[ClusterAlert]:
    if not dimms:
        return []
    fleet_avg = _fleet_avg(dimms)
    by_vendor: dict[str, list[DIMMRecord]] = defaultdict(list)
    by_date: dict[str, list[DIMMRecord]] = defaultdict(list)
    for d in dimms:
        by_vendor[f"{d.vendor}|{d.part_number}"].append(d)
        by_date[d.date_code].append(d)
    alerts = _check_group(by_vendor, fleet_avg, "vendor")
    alerts += _check_group(by_date, fleet_avg, "date_code")
    return alerts


def main() -> None:
    parser = argparse.ArgumentParser(description="DIMM cluster analysis")
    parser.add_argument("--input", type=str, default=None, help="JSON input file (default: stdin)")
    args = parser.parse_args()
    data = json.load(open(args.input)) if args.input else json.load(sys.stdin)
    dimms = [DIMMRecord(**r) for r in data]
    alerts = analyze_clusters(dimms)
    json.dump([asdict(a) for a in alerts], sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
