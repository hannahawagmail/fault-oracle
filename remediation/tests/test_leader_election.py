"""Tests for leader_election module."""
from __future__ import annotations

import urllib.error
from unittest.mock import patch, MagicMock
from io import BytesIO

from remediation.leader_election import LeaderElector


class TestNonK8sEnvironment:
    @patch("remediation.leader_election._in_cluster", return_value=False)
    def test_always_leader(self, _mock):
        le = LeaderElector("default", "test-lease", "node-1")
        assert le.is_leader() is True
        assert le.try_acquire() is True

    @patch("remediation.leader_election._in_cluster", return_value=False)
    def test_release_then_still_fallback(self, _mock):
        le = LeaderElector("default", "test-lease", "node-1")
        le.release()
        assert le.is_leader() is False


class TestK8sLeaseAcquire:
    @patch("remediation.leader_election._in_cluster", return_value=True)
    @patch("urllib.request.urlopen")
    @patch("builtins.open", create=True)
    def test_acquire_success(self, mock_open, mock_urlopen, _mock_cluster):
        mock_open.return_value.__enter__ = lambda s: MagicMock(read=lambda: "fake-token", strip=lambda: "fake-token")
        mock_open.return_value.__exit__ = lambda *a: None
        mock_urlopen.return_value = MagicMock(status=200)

        le = LeaderElector("ns", "lease", "id-1")
        assert le.try_acquire() is True
        assert le.is_leader() is True

    @patch("remediation.leader_election._in_cluster", return_value=True)
    @patch("urllib.request.urlopen")
    @patch("builtins.open", create=True)
    def test_acquire_conflict_409(self, mock_open, mock_urlopen, _mock_cluster):
        mock_open.return_value.__enter__ = lambda s: MagicMock(read=lambda: "fake-token", strip=lambda: "fake-token")
        mock_open.return_value.__exit__ = lambda *a: None
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=409, msg="Conflict", hdrs={}, fp=BytesIO(b"")
        )

        le = LeaderElector("ns", "lease", "id-2")
        assert le.try_acquire() is False
        assert le.is_leader() is False


class TestIdentity:
    @patch("remediation.leader_election._in_cluster", return_value=False)
    def test_identity_preserved(self, _mock):
        le = LeaderElector("default", "my-lease", "unique-node-id", duration_s=30)
        assert le.identity == "unique-node-id"
        assert le.namespace == "default"
        assert le.lease_name == "my-lease"
        assert le.duration_s == 30
