"""Correlate hardware memory errors (EDAC CE/UE) with running workloads."""
from __future__ import annotations

import argparse
import os
import platform
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

PAGE_SIZE = 4096
PAGEMAP_ENTRY_SIZE = 8


@dataclass
class WorkloadInfo:
    pid: int
    comm: str
    cgroup: str
    pod_name: str | None = None
    namespace: str | None = None


def parse_cgroup_for_pod(cgroup_text: str) -> tuple[str | None, str | None]:
    """Extract pod name and namespace from a K8s cgroup path."""
    # Standard format: /kubepods/<qos>/pod<uid>/...
    m = re.search(r"/kubepods[^/]*/[^/]*/pod([^/]+)", cgroup_text)
    if not m:
        # Systemd slice format: kubepods-<qos>-pod<uid>.slice
        m = re.search(r"kubepods-[^-]+-pod([^.]+)\.slice", cgroup_text)
    if not m:
        return None, None
    return m.group(1), None


def _pid_owns_pfn(pid: int, target_pfn: int) -> bool:
    """Check if a PID has a virtual page backed by target_pfn."""
    maps_path = Path(f"/proc/{pid}/maps")
    pagemap_path = Path(f"/proc/{pid}/pagemap")
    try:
        with open(maps_path) as mf, open(pagemap_path, "rb") as pmf:
            for line in mf:
                parts = line.split()
                addr_range = parts[0].split("-")
                start = int(addr_range[0], 16)
                end = int(addr_range[1], 16)
                for vaddr in range(start, end, PAGE_SIZE):
                    offset = (vaddr // PAGE_SIZE) * PAGEMAP_ENTRY_SIZE
                    pmf.seek(offset)
                    data = pmf.read(PAGEMAP_ENTRY_SIZE)
                    if len(data) < PAGEMAP_ENTRY_SIZE:
                        break
                    entry = struct.unpack("Q", data)[0]
                    if entry & (1 << 63):  # page present
                        pfn = entry & ((1 << 55) - 1)
                        if pfn == target_pfn:
                            return True
    except (OSError, PermissionError, ValueError):
        pass
    return False


def correlate_page_to_workload(page_pfn: int) -> WorkloadInfo | None:
    """Map a physical page frame number to the owning workload."""
    if platform.system() != "Linux":
        return None
    proc = Path("/proc")
    if not proc.exists():
        return None
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if not _pid_owns_pfn(pid, page_pfn):
            continue
        try:
            comm = (entry / "comm").read_text().strip()
        except OSError:
            comm = ""
        try:
            cgroup = (entry / "cgroup").read_text().strip()
        except OSError:
            cgroup = ""
        pod_name, namespace = parse_cgroup_for_pod(cgroup)
        return WorkloadInfo(pid=pid, comm=comm, cgroup=cgroup, pod_name=pod_name, namespace=namespace)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Correlate page address to workload")
    parser.add_argument("--page", required=True, help="Physical page address in hex")
    args = parser.parse_args()
    pfn = int(args.page, 16) // PAGE_SIZE
    result = correlate_page_to_workload(pfn)
    if result:
        print(f"PID: {result.pid}, Comm: {result.comm}, Pod: {result.pod_name}, NS: {result.namespace}")
    else:
        print("No workload found for this page address.")
    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
