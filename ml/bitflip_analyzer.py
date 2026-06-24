from __future__ import annotations
import argparse, json
from dataclasses import dataclass, field
from collections import defaultdict
from enum import Enum


class Pattern(str, Enum):
    STUCK_BIT = "stuck_bit"
    ROW_HAMMER = "row_hammer"
    RANDOM_SEU = "random_seu"
    MULTI_BIT = "multi_bit"


@dataclass
class CEEvent:
    page: int
    offset: int
    syndrome: int
    timestamp: float
    controller: str
    csrow: int
    channel: int


@dataclass
class BitflipDiagnosis:
    page: int
    pattern: str
    bit_positions: list[int]
    confidence: float
    recommendation: str


def _bit_positions(syndrome: int) -> list[int]:
    return [i for i in range(64) if syndrome & (1 << i)]


def _check_row_hammer(events: list[CEEvent]) -> list[BitflipDiagnosis]:
    results = []
    by_controller = defaultdict(list)
    for e in events:
        by_controller[(e.controller, e.channel)].append(e)
    for key, evts in by_controller.items():
        evts_sorted = sorted(evts, key=lambda e: e.timestamp)
        for i in range(len(evts_sorted)):
            for j in range(i + 1, len(evts_sorted)):
                a, b = evts_sorted[i], evts_sorted[j]
                if b.timestamp - a.timestamp > 0.064:
                    break
                if abs(a.csrow - b.csrow) == 1 and a.page != b.page:
                    bits = list(set(_bit_positions(a.syndrome) + _bit_positions(b.syndrome)))
                    results.append(BitflipDiagnosis(
                        page=a.page, pattern=Pattern.ROW_HAMMER, bit_positions=sorted(bits),
                        confidence=0.75, recommendation="Remap adjacent rows; schedule DIMM replacement"))
    return results


def analyze_bitflips(events: list[CEEvent]) -> list[BitflipDiagnosis]:
    if not events:
        return []
    results = []
    page_bits: dict[int, list[int]] = defaultdict(list)
    for e in events:
        bits = _bit_positions(e.syndrome)
        if len(bits) > 1:
            results.append(BitflipDiagnosis(
                page=e.page, pattern=Pattern.MULTI_BIT, bit_positions=bits,
                confidence=0.95, recommendation="Offline DIMM immediately; imminent UE risk"))
        page_bits[e.page].extend(bits)

    for page, bits in page_bits.items():
        bit_counts = defaultdict(int)
        for b in bits:
            bit_counts[b] += 1
        stuck = [b for b, c in bit_counts.items() if c > 5]
        if stuck:
            results.append(BitflipDiagnosis(
                page=page, pattern=Pattern.STUCK_BIT, bit_positions=sorted(stuck),
                confidence=0.9, recommendation="Page offline; permanent cell damage"))

    rh = _check_row_hammer(events)
    results.extend(rh)

    diagnosed_pages = {d.page for d in results}
    for page, bits in page_bits.items():
        if page not in diagnosed_pages:
            results.append(BitflipDiagnosis(
                page=page, pattern=Pattern.RANDOM_SEU, bit_positions=sorted(set(bits)),
                confidence=0.6, recommendation="No action; cosmic ray / alpha particle"))
    return results


def main():
    parser = argparse.ArgumentParser(description="Analyze CE syndrome bitflip patterns")
    parser.add_argument("--input", required=True, help="JSON file with CE events")
    args = parser.parse_args()
    with open(args.input) as f:
        raw = json.load(f)
    events = [CEEvent(**e) for e in raw]
    diagnoses = analyze_bitflips(events)
    for d in diagnoses:
        print(f"page=0x{d.page:x} pattern={d.pattern} bits={d.bit_positions} "
              f"confidence={d.confidence} rec=\"{d.recommendation}\"")


if __name__ == "__main__":
    main()
