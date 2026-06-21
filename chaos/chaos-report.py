#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""chaos/chaos-report.py — Parse chaos runner output and produce pass/fail report."""
import sys
from pathlib import Path


def parse_runner_output(text: str) -> dict:
    lines = text.splitlines()
    passes = [line for line in lines if line.startswith("[PASS]")]
    fails  = [line for line in lines if line.startswith("[FAIL]")]
    return {"passes": passes, "fails": fails, "total": len(passes) + len(fails)}


def main():
    if len(sys.argv) < 2:
        print("Usage: chaos-report.py <runner-output-file>", file=sys.stderr)
        sys.exit(1)

    output = Path(sys.argv[1]).read_text()
    report = parse_runner_output(output)

    print("Chaos Report")
    print(f"  Passed: {len(report['passes'])}")
    print(f"  Failed: {len(report['fails'])}")

    if report["fails"]:
        print("\nFailures:")
        for f in report["fails"]:
            print(f"  {f}")
        sys.exit(1)

    print("\nAll chaos scenarios passed.")


if __name__ == "__main__":
    main()
