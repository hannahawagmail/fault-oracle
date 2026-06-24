"""Tests for correlation.workload_impact module."""
from __future__ import annotations

from unittest.mock import patch

from correlation.workload_impact import (
    WorkloadInfo,
    correlate_page_to_workload,
    parse_cgroup_for_pod,
)


class TestParseCgroupForPod:
    def test_extracts_pod_uid(self):
        cgroup = "12:memory:/kubepods/burstable/pod1234-abcd-5678/container_id"
        pod_name, _ = parse_cgroup_for_pod(cgroup)
        assert pod_name == "1234-abcd-5678"

    def test_extracts_besteffort_pod(self):
        cgroup = "0::/kubepods.slice/kubepods-besteffort.slice/kubepods-besteffort-podabc123.slice/cri-containerd.scope"
        pod_name, _ = parse_cgroup_for_pod(cgroup)
        assert pod_name == "abc123"

    def test_returns_none_for_non_k8s(self):
        cgroup = "12:memory:/user.slice/user-1000.slice"
        pod_name, namespace = parse_cgroup_for_pod(cgroup)
        assert pod_name is None
        assert namespace is None


class TestCorrelatePageToWorkload:
    @patch("correlation.workload_impact.platform.system", return_value="Darwin")
    def test_returns_none_on_non_linux(self, mock_sys):
        result = correlate_page_to_workload(0x12345)
        assert result is None

    @patch("correlation.workload_impact.platform.system", return_value="Linux")
    @patch("correlation.workload_impact.Path.exists", return_value=False)
    def test_returns_none_when_proc_unavailable(self, mock_exists, mock_sys):
        result = correlate_page_to_workload(0x12345)
        assert result is None


class TestWorkloadInfo:
    def test_fields(self):
        info = WorkloadInfo(pid=42, comm="python3", cgroup="/sys/fs/cgroup/mem")
        assert info.pid == 42
        assert info.comm == "python3"
        assert info.cgroup == "/sys/fs/cgroup/mem"
        assert info.pod_name is None
        assert info.namespace is None

    def test_optional_fields(self):
        info = WorkloadInfo(pid=1, comm="app", cgroup="/k8s", pod_name="pod-x", namespace="default")
        assert info.pod_name == "pod-x"
        assert info.namespace == "default"
