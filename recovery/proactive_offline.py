from __future__ import annotations
import argparse, json, logging, time
from dataclasses import dataclass, field
from pathlib import Path

SOFT_OFFLINE_PATH = Path("/sys/devices/system/memory/soft_offline_page")
logger = logging.getLogger(__name__)
logging.basicConfig(format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}')


@dataclass
class PageOfflinePolicy:
    ce_threshold: int = 10
    time_window_s: float = 3600.0
    dry_run: bool = False


@dataclass
class PageStats:
    pfn: int
    ce_count: int
    last_ce_time: float
    controller: str
    csrow: int


@dataclass
class OfflineAction:
    pfn: int
    reason: str
    ce_count: int
    recommended: bool


def evaluate_pages(page_stats: dict[int, PageStats], policy: PageOfflinePolicy | None = None) -> list[OfflineAction]:
    policy = policy or PageOfflinePolicy()
    now = time.time()
    actions: list[OfflineAction] = []
    for pfn, stats in page_stats.items():
        if stats.ce_count > policy.ce_threshold and (now - stats.last_ce_time) < policy.time_window_s:
            actions.append(OfflineAction(
                pfn=pfn, reason=f"CE count {stats.ce_count} exceeds threshold {policy.ce_threshold}",
                ce_count=stats.ce_count, recommended=True,
            ))
    return actions


def execute_offline(page_pfn: int, dry_run: bool = False) -> bool:
    if dry_run:
        logger.warning(json.dumps({"action": "soft_offline", "pfn": hex(page_pfn), "dry_run": True}))
        return True
    if not SOFT_OFFLINE_PATH.exists():
        logger.error(json.dumps({"action": "soft_offline", "pfn": hex(page_pfn), "error": "sysfs not available"}))
        return False
    try:
        SOFT_OFFLINE_PATH.write_text(hex(page_pfn))
        logger.info(json.dumps({"action": "soft_offline", "pfn": hex(page_pfn), "success": True}))
        return True
    except OSError as e:
        logger.error(json.dumps({"action": "soft_offline", "pfn": hex(page_pfn), "error": str(e)}))
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Proactive memory page offlining")
    parser.add_argument("--threshold", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    args = parser.parse_args()
    policy = PageOfflinePolicy(ce_threshold=args.threshold, dry_run=args.dry_run)
    # In production: query Prometheus for per-page CE counts; here stub for demonstration
    logger.setLevel(logging.INFO)
    logger.info(json.dumps({"event": "start", "policy": {"threshold": policy.ce_threshold, "dry_run": policy.dry_run, "prometheus_url": args.prometheus_url}}))


if __name__ == "__main__":
    main()
