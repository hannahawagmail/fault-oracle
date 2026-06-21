# SPDX-License-Identifier: Apache-2.0
"""
tools/generate_edac_log.py — Synthetic EDAC CE storm log generator.

Generates realistic syslog-format EDAC correctable error log lines suitable
for use as input to parse_edac_trace.py, replay benchmarks, and CI test fixtures.

Usage:
    python3 tools/generate_edac_log.py --events 500 --rate 2.5 --controllers 2
    python3 tools/generate_edac_log.py --burst-pattern --output sample_burst.log
    python3 tools/generate_edac_log.py --help
"""

import argparse
import datetime
import random
import sys


# Correctable error message templates matching parse_edac_trace.py RE_EDAC_CE
_CE_TEMPLATE = (
    "{ts} {host} kernel: EDAC MC{mc}: {count} CE {detail}"
    " on MC{mc} csrow{csrow} channel{ch} (mc#{mc} page 0x{page:x} offset 0x{off:x} grain {grain})"
)

_CE_DETAIL_TEMPLATES = [
    "memory read error",
    "ECC scrub corrected error",
    "CE on rank {rank}",
    "single-bit ECC error on DIMM {dimm}",
]

_UE_TEMPLATE = (
    "{ts} {host} kernel: EDAC MC{mc}: {count} UE {detail}"
    " on MC{mc} csrow{csrow} (mc#{mc} page 0x{page:x} offset 0x{off:x} grain {grain})"
)


def _syslog_ts(dt: datetime.datetime) -> str:
    return dt.strftime("%b %d %H:%M:%S").replace(" 0", "  ")


def _detail(template_list: list, **kwargs) -> str:
    t = random.choice(template_list)
    return t.format(**kwargs)


def generate_events(
    n_events: int,
    start_time: datetime.datetime,
    rate_per_second: float,
    n_controllers: int,
    n_csrows: int,
    n_channels: int,
    burst: bool,
    ue_rate: float,
    hostname: str,
    seed: int | None,
) -> list[str]:
    """Return a list of syslog lines representing EDAC error events."""
    rng = random.Random(seed)
    lines = []
    t = start_time

    for i in range(n_events):
        # Advance time: Poisson inter-arrival if not burst, else bimodal
        if burst:
            # 80% of events arrive in tight 10-event bursts
            if i % 10 == 0:
                gap = rng.expovariate(rate_per_second * 20)
            else:
                gap = rng.expovariate(rate_per_second * 5)
        else:
            gap = rng.expovariate(rate_per_second)
        t += datetime.timedelta(seconds=gap)

        mc = rng.randint(0, n_controllers - 1)
        csrow = rng.randint(0, n_csrows - 1)
        ch = rng.randint(0, n_channels - 1)
        page = rng.randint(0x10000, 0xFFFFFF)
        off = rng.randint(0, 0x3FF) & ~0x3F
        grain = rng.choice([8, 64, 128])
        count = rng.randint(1, 3)

        is_ue = rng.random() < ue_rate
        if is_ue:
            detail = _detail(
                ["multi-bit ECC error", "double-bit error detected on DIMM 0"],
                rank=csrow,
                dimm=f"A{csrow}",
            )
            line = _UE_TEMPLATE.format(
                ts=_syslog_ts(t),
                host=hostname,
                mc=mc,
                count=count,
                detail=detail,
                csrow=csrow,
                page=page,
                off=off,
                grain=grain,
            )
        else:
            detail = _detail(
                _CE_DETAIL_TEMPLATES,
                rank=csrow,
                dimm=f"A{csrow}",
            )
            line = _CE_TEMPLATE.format(
                ts=_syslog_ts(t),
                host=hostname,
                mc=mc,
                count=count,
                detail=detail,
                csrow=csrow,
                ch=ch,
                page=page,
                off=off,
                grain=grain,
            )
        lines.append(line)

    return lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Generate synthetic EDAC CE storm syslog for testing.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--events", type=int, default=200, help="Total number of error events")
    p.add_argument("--rate", type=float, default=1.0, help="Mean events per second")
    p.add_argument("--controllers", type=int, default=2, help="Number of memory controllers (mc0..mcN-1)")
    p.add_argument("--csrows", type=int, default=4, help="Number of csrows per controller")
    p.add_argument("--channels", type=int, default=2, help="Number of channels per csrow")
    p.add_argument("--ue-rate", type=float, default=0.02, help="Fraction of events that are UEs (0.0–1.0)")
    p.add_argument("--burst-pattern", action="store_true", help="Generate bursty arrival pattern")
    p.add_argument("--hostname", default="arm64-node-01", help="Hostname in syslog lines")
    p.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    p.add_argument("--output", default="-", help="Output file path (- for stdout)")
    p.add_argument(
        "--start-time",
        default=None,
        help="Start timestamp ISO format (default: 2024-01-15T08:00:00)",
    )
    args = p.parse_args(argv)

    start = (
        datetime.datetime.fromisoformat(args.start_time)
        if args.start_time
        else datetime.datetime(2024, 1, 15, 8, 0, 0)
    )

    lines = generate_events(
        n_events=args.events,
        start_time=start,
        rate_per_second=args.rate,
        n_controllers=args.controllers,
        n_csrows=args.csrows,
        n_channels=args.channels,
        burst=args.burst_pattern,
        ue_rate=args.ue_rate,
        hostname=args.hostname,
        seed=args.seed,
    )

    output = "\n".join(lines) + "\n"
    if args.output == "-":
        sys.stdout.write(output)
    else:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Written {len(lines)} events to {args.output}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
