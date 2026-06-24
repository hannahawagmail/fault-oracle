from __future__ import annotations
import time
from unittest.mock import patch, MagicMock
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from proactive_offline import PageOfflinePolicy, PageStats, evaluate_pages, execute_offline


def _make_stats(pfn: int, ce_count: int) -> dict[int, PageStats]:
    return {pfn: PageStats(pfn=pfn, ce_count=ce_count, last_ce_time=time.time(), controller="mc0", csrow=0)}


def test_page_exceeding_threshold_is_flagged():
    policy = PageOfflinePolicy(ce_threshold=10)
    actions = evaluate_pages(_make_stats(0xABCD, 15), policy)
    assert len(actions) == 1
    assert actions[0].pfn == 0xABCD
    assert actions[0].recommended is True
    assert actions[0].ce_count == 15


def test_page_below_threshold_not_flagged():
    policy = PageOfflinePolicy(ce_threshold=10)
    actions = evaluate_pages(_make_stats(0x1234, 5), policy)
    assert len(actions) == 0


def test_dry_run_does_not_write_to_sysfs():
    with patch("proactive_offline.SOFT_OFFLINE_PATH") as mock_path:
        result = execute_offline(0xABCD, dry_run=True)
        assert result is True
        mock_path.write_text.assert_not_called()


def test_execute_offline_returns_false_on_missing_sysfs():
    with patch("proactive_offline.SOFT_OFFLINE_PATH") as mock_path:
        mock_path.exists.return_value = False
        result = execute_offline(0xABCD, dry_run=False)
        assert result is False
