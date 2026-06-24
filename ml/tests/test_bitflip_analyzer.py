from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bitflip_analyzer import CEEvent, analyze_bitflips, Pattern


def _make_event(page=0x1000, offset=0, syndrome=0x1, timestamp=1.0, controller="mc0", csrow=0, channel=0):
    return CEEvent(page=page, offset=offset, syndrome=syndrome, timestamp=timestamp,
                   controller=controller, csrow=csrow, channel=channel)


def test_stuck_bit_detection():
    events = [_make_event(syndrome=0x4, timestamp=float(i)) for i in range(6)]
    results = analyze_bitflips(events)
    stuck = [r for r in results if r.pattern == Pattern.STUCK_BIT]
    assert len(stuck) == 1
    assert stuck[0].page == 0x1000
    assert 2 in stuck[0].bit_positions


def test_random_seu():
    events = [_make_event(syndrome=(1 << i), timestamp=float(i)) for i in range(5)]
    results = analyze_bitflips(events)
    seu = [r for r in results if r.pattern == Pattern.RANDOM_SEU]
    assert len(seu) == 1
    assert seu[0].confidence == 0.6


def test_multi_bit_detection():
    events = [_make_event(syndrome=0b1101)]
    results = analyze_bitflips(events)
    multi = [r for r in results if r.pattern == Pattern.MULTI_BIT]
    assert len(multi) == 1
    assert sorted(multi[0].bit_positions) == [0, 2, 3]


def test_empty_input():
    assert analyze_bitflips([]) == []
