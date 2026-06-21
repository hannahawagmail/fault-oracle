#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
network/tests/test_network_collectors.py — Pytest suite for ib-collector and
nic-collector.  No real IB or NIC hardware required; all subprocess calls and
sysfs reads are mocked with fixture strings.

Run:
    python3 -m pytest network/tests/ -v
"""
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

import pytest

# ---------------------------------------------------------------------------
# Path setup — allow importing the collectors from the network/ directory
# ---------------------------------------------------------------------------
NETWORK_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(NETWORK_DIR))

import ib_collector as ib_mod    # noqa: E402
import nic_collector as nic_mod  # noqa: E402


# ===========================================================================
# IB Collector fixtures
# ===========================================================================

IBSTAT_OUTPUT = """
CA 'mlx5_0'
\tCA type: MT4119
\tNumber of ports: 1
\tPort 1:
\t\tState: Active
\t\tPhysical state: LinkUp
\t\tRate: 100 Gb/sec
\t\tSM lid: 1
\t\tCapability mask: 0x2651e84a
\t\tPort GUID: 0x0002c903003d9e41
\t\tLink layer: InfiniBand
"""

IBSTAT_OUTPUT_DOWN = """
CA 'mlx5_0'
\tCA type: MT4119
\tNumber of ports: 1
\tPort 1:
\t\tState: Down
\t\tPhysical state: Disabled
\t\tRate: 10 Gb/sec
"""

IBSTAT_OUTPUT_MULTI_PORT = """
CA 'mlx5_1'
\tCA type: MT4120
\tNumber of ports: 2
\tPort 1:
\t\tState: Active
\t\tPhysical state: LinkUp
\t\tRate: 100 Gb/sec
\tPort 2:
\t\tState: Down
\t\tPhysical state: Polling
\t\tRate: 100 Gb/sec
"""

PERFQUERY_OUTPUT = """
# Port extended counters: Lid 5 port 1 (CapMask: 0x5300)
SymbolErrorCounter:......................0
LinkRecovers:...........................3
LinkDowned:.............................0
PortRcvErrors:..........................0
PortRcvRemotePhysicalErrors:............0
PortXmtDiscards:........................0
VL15Dropped:............................0
"""

PERFQUERY_WITH_ERRORS = """
# Port extended counters: Lid 5 port 1 (CapMask: 0x5300)
SymbolErrorCounter:......................42
LinkRecovers:...........................3
LinkDowned:.............................1
PortRcvErrors:..........................7
PortRcvRemotePhysicalErrors:............0
PortXmtDiscards:........................2
VL15Dropped:............................0
"""

# ===========================================================================
# NIC Collector fixtures
# ===========================================================================

SYSFS_NIC_STATS = {
    'rx_errors': '42', 'tx_errors': '0', 'rx_dropped': '5',
    'tx_dropped': '0', 'rx_crc_errors': '2', 'rx_missed_errors': '1'
}

ETHTOOL_ROCE_OUTPUT = """\
NIC statistics:
     rx_packets: 1000000
     tx_packets: 999000
     out_of_buffer: 17
     req_rnr_retry_exceeded: 3
     rx_bytes: 1048576
"""

ETHTOOL_NO_ROCE_OUTPUT = """\
NIC statistics:
     rx_packets: 500000
     tx_packets: 499000
     rx_bytes: 524288
"""


# ===========================================================================
# TestIBCollector  (10 tests)
# ===========================================================================

class TestIBCollector:
    """Tests for network/ib-collector.py. No IB hardware required."""

    def test_no_ib_dir_emits_collector_up_zero(self):
        """When /sys/class/infiniband does not exist, emit ib_collector_up 0."""
        with patch.object(ib_mod.os.path, "isdir", return_value=False):
            result = ib_mod.detect_ib()
        assert result is False
        output = ib_mod.emit_metrics(False, {}, 1000.0)
        assert "ib_collector_up 0" in output

    def test_empty_ib_dir_emits_collector_up_zero(self):
        """When /sys/class/infiniband exists but is empty, detect_ib returns False."""
        with patch.object(ib_mod.os.path, "isdir", return_value=True), \
             patch.object(ib_mod.os, "listdir", return_value=[]):
            result = ib_mod.detect_ib()
        assert result is False

    def test_active_port_parsed_from_ibstat(self):
        """parse_ibstat correctly identifies State: Active for port 1."""
        devices = ib_mod.parse_ibstat(IBSTAT_OUTPUT)
        assert "mlx5_0" in devices
        assert devices["mlx5_0"]["ports"]["1"]["state"] == "Active"

    def test_symbol_error_counter_zero_emitted(self):
        """SymbolErrorCounter=0 from perfquery is emitted in output."""
        counters = ib_mod.parse_perfquery(PERFQUERY_OUTPUT)
        assert counters["SymbolErrorCounter"] == 0

        device_metrics = {
            "mlx5_0": {
                "ibstat_ok": True,
                "ports": {
                    "1": {
                        "state": "Active",
                        "rate_gbps": 100,
                        "counters": counters,
                    }
                },
            }
        }
        output = ib_mod.emit_metrics(True, device_metrics, 1000.0)
        assert 'ib_port_error_total{device="mlx5_0",port="1",counter="SymbolErrorCounter"} 0' in output

    def test_link_recovers_3_emitted(self):
        """LinkRecovers=3 from PERFQUERY_OUTPUT is correctly parsed and emitted."""
        counters = ib_mod.parse_perfquery(PERFQUERY_OUTPUT)
        assert counters["LinkRecovers"] == 3

        device_metrics = {
            "mlx5_0": {
                "ibstat_ok": True,
                "ports": {
                    "1": {
                        "state": "Active",
                        "rate_gbps": 100,
                        "counters": counters,
                    }
                },
            }
        }
        output = ib_mod.emit_metrics(True, device_metrics, 1000.0)
        assert 'ib_port_error_total{device="mlx5_0",port="1",counter="LinkRecovers"} 3' in output

    def test_inactive_port_state_down_emits_zero(self):
        """A port with State: Down emits ib_port_state=0."""
        devices = ib_mod.parse_ibstat(IBSTAT_OUTPUT_DOWN)
        assert devices["mlx5_0"]["ports"]["1"]["state"] == "Down"

        device_metrics = {
            "mlx5_0": {
                "ibstat_ok": True,
                "ports": {
                    "1": {
                        "state": "Down",
                        "rate_gbps": 10,
                        "counters": {},
                    }
                },
            }
        }
        output = ib_mod.emit_metrics(True, device_metrics, 1000.0)
        assert 'ib_port_state{device="mlx5_0",port="1"} 0' in output

    def test_rate_100_gbps_parsed(self):
        """Rate: 100 Gb/sec in ibstat output parses to rate_gbps=100."""
        devices = ib_mod.parse_ibstat(IBSTAT_OUTPUT)
        assert devices["mlx5_0"]["ports"]["1"]["rate_gbps"] == 100

    def test_ibstat_file_not_found_returns_collector_up_zero(self):
        """When ibstat binary is missing (_run returns rc=-1), ibstat_ok is False."""
        with patch.object(ib_mod, "_run", return_value=("", -1)):
            info = ib_mod.collect_device_metrics("mlx5_0")
        assert info["ibstat_ok"] is False

    def test_multi_port_device_both_ports_present(self):
        """parse_ibstat correctly handles a 2-port device."""
        devices = ib_mod.parse_ibstat(IBSTAT_OUTPUT_MULTI_PORT)
        assert "mlx5_1" in devices
        ports = devices["mlx5_1"]["ports"]
        assert "1" in ports
        assert "2" in ports
        assert ports["1"]["state"] == "Active"
        assert ports["2"]["state"] == "Down"

    def test_perfquery_absent_graceful_empty_counters(self):
        """When perfquery is absent (rc=-1), active port gets empty counters dict."""
        def mock_run(cmd, timeout=10):
            if cmd[0] == "ibstat":
                return (IBSTAT_OUTPUT, 0)
            # perfquery absent
            return ("", -1)

        with patch.object(ib_mod, "_run", side_effect=mock_run):
            info = ib_mod.collect_device_metrics("mlx5_0")

        assert info["ibstat_ok"] is True
        assert info["ports"]["1"]["counters"] == {}

    def test_collector_up_one_when_active_ib(self):
        """emit_metrics with ib_present=True emits ib_collector_up 1."""
        output = ib_mod.emit_metrics(True, {}, 1000.0)
        assert "ib_collector_up 1" in output

    def test_ib_port_link_rate_emitted(self):
        """ib_port_link_rate_gbps metric is emitted with correct value."""
        device_metrics = {
            "mlx5_0": {
                "ibstat_ok": True,
                "ports": {
                    "1": {
                        "state": "Active",
                        "rate_gbps": 100,
                        "counters": {},
                    }
                },
            }
        }
        output = ib_mod.emit_metrics(True, device_metrics, 1000.0)
        assert 'ib_port_link_rate_gbps{device="mlx5_0",port="1"} 100' in output

    def test_timestamp_metric_emitted(self):
        """ib_collector_last_run_timestamp is always emitted."""
        output = ib_mod.emit_metrics(False, {}, 1234567890.0)
        assert "ib_collector_last_run_timestamp 1234567890.000" in output


# ===========================================================================
# TestNICCollector  (10 tests)
# ===========================================================================

class TestNICCollector:
    """Tests for network/nic-collector.py. No real NIC required."""

    def test_rx_errors_parsed_from_sysfs_fixture(self, tmp_path):
        """rx_errors=42 read from sysfs returns correct value."""
        # Build fake sysfs tree
        iface_dir = tmp_path / "eth0" / "statistics"
        iface_dir.mkdir(parents=True)
        (iface_dir / "rx_errors").write_text("42\n")

        with patch.object(nic_mod, "NET_SYSFS_ROOT", str(tmp_path)):
            val = nic_mod.read_sysfs_stat("eth0", "rx_errors")
        assert val == 42

    def test_multiple_interfaces_collected(self, tmp_path):
        """Multiple physical interfaces are all collected."""
        for iface in ("eth0", "eth1"):
            stat_dir = tmp_path / iface / "statistics"
            stat_dir.mkdir(parents=True)
            device_dir = tmp_path / iface / "device"
            device_dir.mkdir(parents=True)
            for stat in ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped",
                         "rx_crc_errors", "rx_missed_errors"):
                (stat_dir / stat).write_text("0\n")

        with patch.object(nic_mod, "NET_SYSFS_ROOT", str(tmp_path)):
            ifaces = nic_mod.list_physical_interfaces()
        assert "eth0" in ifaces
        assert "eth1" in ifaces

    def test_loopback_lo_excluded(self, tmp_path):
        """The 'lo' interface is always excluded from collection."""
        lo_dir = tmp_path / "lo"
        lo_dir.mkdir()
        (tmp_path / "lo" / "device").mkdir()

        with patch.object(nic_mod, "NET_SYSFS_ROOT", str(tmp_path)):
            ifaces = nic_mod.list_physical_interfaces()
        assert "lo" not in ifaces

    def test_virtual_interface_excluded_no_device(self, tmp_path):
        """An interface without a /device directory is excluded as virtual."""
        veth_dir = tmp_path / "veth0"
        veth_dir.mkdir()
        # No 'device' subdirectory — this is virtual

        with patch.object(nic_mod, "NET_SYSFS_ROOT", str(tmp_path)):
            ifaces = nic_mod.list_physical_interfaces()
        assert "veth0" not in ifaces

    def test_roce_detected_via_mlx5_core_driver(self, tmp_path):
        """Interface with driver symlink pointing to mlx5_core is detected as RoCE."""
        device_dir = tmp_path / "mlx5_0" / "device"
        device_dir.mkdir(parents=True)
        driver_target = tmp_path / "drivers" / "mlx5_core"
        driver_target.mkdir(parents=True)
        driver_link = device_dir / "driver"
        driver_link.symlink_to(str(driver_target))

        with patch.object(nic_mod, "NET_SYSFS_ROOT", str(tmp_path)):
            result = nic_mod.is_roce_interface("mlx5_0")
        assert result is True

    def test_ethtool_roce_counters_parsed(self):
        """out_of_buffer=17 and req_rnr_retry_exceeded=3 parsed from ethtool output."""
        with patch.object(nic_mod, "_run", return_value=(ETHTOOL_ROCE_OUTPUT, 0)):
            counters = nic_mod.collect_ethtool_roce("eth0")
        assert counters.get("out_of_buffer") == 17
        assert counters.get("req_rnr_retry_exceeded") == 3

    def test_ethtool_absent_graceful_empty_dict(self):
        """When ethtool binary is missing (_run rc=-1), returns empty dict."""
        with patch.object(nic_mod, "_run", return_value=("", -1)):
            counters = nic_mod.collect_ethtool_roce("eth0")
        assert counters == {}

    def test_tx_dropped_zero_still_emitted(self):
        """tx_dropped=0 is emitted in output, not suppressed."""
        iface_stats = {
            "eth0": {
                "rx": {"errors": 0, "dropped": 0, "crc_errors": 0, "missed_errors": 0},
                "tx": {"errors": 0, "dropped": 0},
                "is_roce": False,
                "roce_counters": {},
            }
        }
        output = nic_mod.emit_metrics(True, iface_stats, 1000.0)
        assert 'nic_errors_total{interface="eth0",direction="tx",error_type="dropped"} 0' in output

    def test_collector_up_one_when_interfaces_found(self):
        """emit_metrics with ifaces_found=True emits nic_collector_up 1."""
        output = nic_mod.emit_metrics(True, {}, 1000.0)
        assert "nic_collector_up 1" in output

    def test_collector_up_zero_when_no_interfaces(self):
        """emit_metrics with ifaces_found=False emits nic_collector_up 0."""
        output = nic_mod.emit_metrics(False, {}, 1000.0)
        assert "nic_collector_up 0" in output

    def test_timestamp_metric_emitted(self):
        """nic_collector_last_run_timestamp is always emitted."""
        output = nic_mod.emit_metrics(False, {}, 9999999.5)
        assert "nic_collector_last_run_timestamp 9999999.500" in output

    def test_rx_errors_in_output_metric(self):
        """rx_errors from SYSFS_NIC_STATS fixture appears correctly in emitted output."""
        iface_stats = {
            "eth0": {
                "rx": {
                    "errors": int(SYSFS_NIC_STATS["rx_errors"]),
                    "dropped": int(SYSFS_NIC_STATS["rx_dropped"]),
                    "crc_errors": int(SYSFS_NIC_STATS["rx_crc_errors"]),
                    "missed_errors": int(SYSFS_NIC_STATS["rx_missed_errors"]),
                },
                "tx": {
                    "errors": int(SYSFS_NIC_STATS["tx_errors"]),
                    "dropped": int(SYSFS_NIC_STATS["tx_dropped"]),
                },
                "is_roce": False,
                "roce_counters": {},
            }
        }
        output = nic_mod.emit_metrics(True, iface_stats, 1000.0)
        assert 'nic_errors_total{interface="eth0",direction="rx",error_type="errors"} 42' in output

    def test_roce_counters_emitted_in_output(self):
        """RoCE out_of_buffer counter appears in emitted Prometheus output."""
        iface_stats = {
            "eth0": {
                "rx": {"errors": 0, "dropped": 0, "crc_errors": 0, "missed_errors": 0},
                "tx": {"errors": 0, "dropped": 0},
                "is_roce": True,
                "roce_counters": {"out_of_buffer": 17, "req_rnr_retry_exceeded": 3},
            }
        }
        output = nic_mod.emit_metrics(True, iface_stats, 1000.0)
        assert 'roce_errors_total{interface="eth0",counter="out_of_buffer"} 17' in output

    def test_non_roce_interface_not_roce_detected(self, tmp_path):
        """Interface with driver 'e1000e' is not detected as RoCE."""
        device_dir = tmp_path / "eth1" / "device"
        device_dir.mkdir(parents=True)
        driver_target = tmp_path / "drivers" / "e1000e"
        driver_target.mkdir(parents=True)
        driver_link = device_dir / "driver"
        driver_link.symlink_to(str(driver_target))

        with patch.object(nic_mod, "NET_SYSFS_ROOT", str(tmp_path)):
            result = nic_mod.is_roce_interface("eth1")
        assert result is False
