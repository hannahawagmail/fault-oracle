# SPDX-License-Identifier: Apache-2.0
"""
conftest.py — Shared pytest fixtures and mock sysfs tree for the test suite.

The mock sysfs tree mirrors the structure the exporter reads in production,
allowing all exporter unit and integration tests to run without real hardware,
without root access, and in any CI environment.

Fixtures provided:
  mock_sysfs        Temporary directory with a complete mock /sys tree
  edac_sysfs        The /sys/devices/system/edac/ portion of mock_sysfs
  aer_sysfs         The /sys/bus/pci/devices/ portion of mock_sysfs
  mce_sysfs         The /sys/firmware/acpi/errors/ portion of mock_sysfs
  sample_events     Pre-parsed HardwareEvent list from sample_ce_storm.log
  exporter_process  A running hw-fault-exporter process (if binary available)
"""

import sys
from pathlib import Path

import pytest

# Add the project root to the Python path so we can import the replay module
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "replay"))


# ---------------------------------------------------------------------------
# Mock sysfs topology
# ---------------------------------------------------------------------------

MOCK_TOPOLOGY = {
    # Memory controller 0: 2 csrows × 2 channels
    "mc0": {
        "csrows": 2,
        "channels": 2,
        "mc_name": "cortex-a72-l2-ecc",
        "size_mb": 8192,
        "ce_counts": [[3, 0], [0, 1]],  # [csrow][channel] CE counts
        "ue_counts": [0, 0],  # [csrow] UE counts
    },
    # Memory controller 1: 2 csrows × 2 channels (clean)
    "mc1": {
        "csrows": 2,
        "channels": 2,
        "mc_name": "cortex-a72-l2-ecc",
        "size_mb": 8192,
        "ce_counts": [[0, 0], [0, 0]],
        "ue_counts": [0, 0],
    },
}

MOCK_AER_DEVICES = {
    # PCIe device 0000:01:00.0 — has a few BadTLP events
    "0000:01:00.0": {
        "correctable": {
            "RxErr": 0,
            "BadTLP": 3,
            "BadDLLP": 0,
            "Rollover": 0,
            "Timeout": 0,
            "NonFatalErr": 0,
            "CorrIntErr": 0,
            "HeaderOF": 0,
            "TOTAL_ERR_COR": 3,
        },
        "nonfatal": {
            "Undefined": 0,
            "BlockedTLP": 0,
            "AtomicOpBlocked": 0,
            "TLPBlockedErr": 0,
            "PoisonTLPBlocked": 0,
            "ViErr": 0,
            "MCBlockedTLP": 0,
            "SurpriseDownErr": 0,
            "PoisonedTLP": 0,
            "FlowControl": 0,
            "CmpltTimeout": 0,
            "CmpltAbort": 0,
            "UnxCmplt": 0,
            "RxOF": 0,
            "MalfTLP": 0,
            "ECRC": 0,
            "UnsupReq": 0,
            "ACSViol": 0,
            "UncorrIntErr": 0,
            "TOTAL_ERR_UNCOR": 0,
        },
        "fatal": {
            "Undefined": 0,
            "BlockedTLP": 0,
            "DLP": 0,
            "SDES": 0,
            "TLP": 0,
            "FCP": 0,
            "CmpltTO": 0,
            "CA": 0,
            "UC": 0,
            "RxOF": 0,
            "MALFTLP": 0,
            "ECRC": 0,
            "UR": 0,
            "TOTAL_ERR_UNCOR": 0,
        },
    },
    # PCIe device 0000:02:00.0 — clean
    "0000:02:00.0": {
        "correctable": {
            "RxErr": 0,
            "BadTLP": 0,
            "BadDLLP": 0,
            "Rollover": 0,
            "Timeout": 0,
            "NonFatalErr": 0,
            "CorrIntErr": 0,
            "HeaderOF": 0,
            "TOTAL_ERR_COR": 0,
        },
        "nonfatal": {"TOTAL_ERR_UNCOR": 0},
        "fatal": {"TOTAL_ERR_UNCOR": 0},
    },
}

MOCK_MCE = {
    "corrected_errors": 5,
    "deferred_errors": 0,
    "uncorrected_errors": 0,
}


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def build_mock_sysfs(root: Path) -> None:
    """Populate root/ with a mock sysfs tree matching MOCK_TOPOLOGY."""

    # --- EDAC ---
    for mc_name, mc in MOCK_TOPOLOGY.items():
        mc_dir = root / "devices" / "system" / "edac" / "mc" / mc_name
        mc_dir.mkdir(parents=True, exist_ok=True)

        _write(mc_dir / "mc_name", mc["mc_name"] + "\n")
        _write(mc_dir / "size_mb", str(mc["size_mb"]) + "\n")

        # Compute controller-level totals
        total_ce = sum(
            mc["ce_counts"][r][c] for r in range(mc["csrows"]) for c in range(mc["channels"])
        )
        total_ue = sum(mc["ue_counts"])
        _write(mc_dir / "ce_count", str(total_ce) + "\n")
        _write(mc_dir / "ue_count", str(total_ue) + "\n")
        _write(mc_dir / "ce_noinfo_count", "0\n")
        _write(mc_dir / "ue_noinfo_count", "0\n")
        _write(mc_dir / "reset_counters", "0\n")

        for r in range(mc["csrows"]):
            csrow_dir = mc_dir / f"csrow{r}"
            csrow_dir.mkdir(exist_ok=True)
            csrow_ce = sum(mc["ce_counts"][r])
            _write(csrow_dir / "ce_count", str(csrow_ce) + "\n")
            _write(csrow_dir / "ue_count", str(mc["ue_counts"][r]) + "\n")
            for c in range(mc["channels"]):
                ch_ce = mc["ce_counts"][r][c]
                _write(csrow_dir / f"ch{c}_ce_count", str(ch_ce) + "\n")
                _write(csrow_dir / f"ch{c}_dimm_label", f"DIMM_{r}_CH{c}\n")

    # --- PCIe AER ---
    for bdf, dev in MOCK_AER_DEVICES.items():
        dev_dir = root / "bus" / "pci" / "devices" / bdf
        dev_dir.mkdir(parents=True, exist_ok=True)

        for error_class, errors in dev.items():
            fname_map = {
                "correctable": "aer_dev_correctable",
                "nonfatal": "aer_dev_nonfatal",
                "fatal": "aer_dev_fatal",
            }
            lines = "\n".join(f"{k} {v}" for k, v in errors.items()) + "\n"
            _write(dev_dir / fname_map[error_class], lines)

    # --- MCE / ACPI APEI ---
    apei_dir = root / "firmware" / "acpi" / "errors"
    apei_dir.mkdir(parents=True, exist_ok=True)
    for fname, count in MOCK_MCE.items():
        _write(apei_dir / fname, str(count) + "\n")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def mock_sysfs(tmp_path_factory) -> Path:
    """
    Session-scoped mock sysfs tree. Built once per pytest session.
    Returns the root Path of the mock /sys directory.
    """
    root = tmp_path_factory.mktemp("mock-sysfs")
    build_mock_sysfs(root)
    return root


@pytest.fixture(scope="session")
def edac_sysfs(mock_sysfs) -> Path:
    """Path to the EDAC mc directory within the mock sysfs."""
    return mock_sysfs / "devices" / "system" / "edac" / "mc"


@pytest.fixture(scope="session")
def aer_sysfs(mock_sysfs) -> Path:
    """Path to the PCIe devices directory within the mock sysfs."""
    return mock_sysfs / "bus" / "pci" / "devices"


@pytest.fixture(scope="session")
def mce_sysfs(mock_sysfs) -> Path:
    """Path to the MCE/APEI errors directory within the mock sysfs."""
    return mock_sysfs / "firmware" / "acpi" / "errors"


@pytest.fixture(scope="session")
def mock_topology() -> dict:
    """The MOCK_TOPOLOGY dict for test assertions."""
    return MOCK_TOPOLOGY


@pytest.fixture(scope="session")
def mock_aer_devices() -> dict:
    """The MOCK_AER_DEVICES dict for test assertions."""
    return MOCK_AER_DEVICES


@pytest.fixture(scope="session")
def mock_mce() -> dict:
    """The MOCK_MCE dict for test assertions."""
    return MOCK_MCE


@pytest.fixture(scope="session")
def sample_trace_path() -> Path:
    """Path to the sample CE storm trace log."""
    return REPO_ROOT / "replay" / "example_traces" / "sample_ce_storm.log"


@pytest.fixture(scope="session")
def sample_events(sample_trace_path) -> list:
    """
    Parsed events from the sample CE storm trace.
    Returns a list of HardwareEvent objects.
    """
    from parse_edac_trace import TraceParser

    parser = TraceParser()
    with open(sample_trace_path) as f:
        return parser.parse_file(f)


@pytest.fixture
def writable_sysfs(tmp_path) -> Path:
    """
    Function-scoped writable mock sysfs for tests that need to modify counters.
    Each test gets a fresh copy.
    """
    build_mock_sysfs(tmp_path)
    return tmp_path


@pytest.fixture
def zero_sysfs(tmp_path) -> Path:
    """
    A mock sysfs where all counters are zero.
    Used for testing counter increment behavior.
    """
    global MOCK_TOPOLOGY
    zero_topo = {}
    for mc_name, mc in MOCK_TOPOLOGY.items():
        zero_topo[mc_name] = {
            **mc,
            "ce_counts": [[0] * mc["channels"] for _ in range(mc["csrows"])],
            "ue_counts": [0] * mc["csrows"],
        }

    orig = MOCK_TOPOLOGY
    MOCK_TOPOLOGY = zero_topo
    build_mock_sysfs(tmp_path)
    MOCK_TOPOLOGY = orig
    return tmp_path


# ---------------------------------------------------------------------------
# Collector factory helpers (used by exporter unit tests)
# ---------------------------------------------------------------------------


@pytest.fixture
def edac_collector(mock_sysfs):
    """An EDACCollector instance pointed at the mock sysfs."""
    # Import lazily to avoid requiring Go-built binary for pure Python tests
    try:
        # Use the Go test helper if available, otherwise use Python mock
        return _MockEDACCollector(str(mock_sysfs))
    except Exception:
        pytest.skip("Go exporter not available")


class _MockEDACCollector:
    """
    Python-side mock of the Go EDACCollector.

    Reads counter files directly from the mock sysfs tree built by
    build_mock_sysfs(), returning a nested dict that mirrors the structure
    emitted by the real Go collector:

    {
        "mc0": {
            "ce_count": <int>,          # controller-level CE total
            "ue_count": <int>,          # controller-level UE total
            "csrows": {
                "csrow0": {
                    "ce_count": <int>,  # csrow-level CE total
                    "ue_count": <int>,  # csrow-level UE count
                    "channels": {
                        "ch0": <int>,   # per-channel CE count
                        "ch1": <int>,
                        ...
                    },
                },
                ...
            },
        },
        ...
    }
    """

    def __init__(self, sysfs_root: str):
        self.sysfs_root = sysfs_root

    def collect_raw(self) -> dict:
        """
        Walk the mock sysfs tree and return raw counter values.

        Returns a dict matching the structure described in the class docstring.
        Missing files are silently skipped (mirrors Go collector error handling).
        """
        result: dict = {}
        mc_root = Path(self.sysfs_root) / "devices" / "system" / "edac" / "mc"

        if not mc_root.is_dir():
            return result

        for mc_dir in sorted(mc_root.iterdir()):
            if not mc_dir.is_dir():
                continue
            # Only process directories named mc<N>
            import re as _re

            if not _re.match(r"^mc\d+$", mc_dir.name):
                continue

            mc_name = mc_dir.name

            def _read_int(path: Path) -> int:
                """Read a sysfs counter file, returning 0 on any error."""
                try:
                    return int(path.read_text().strip())
                except (OSError, ValueError):
                    return 0

            result[mc_name] = {
                "ce_count": _read_int(mc_dir / "ce_count"),
                "ue_count": _read_int(mc_dir / "ue_count"),
                "csrows": {},
            }

            for csrow_dir in sorted(mc_dir.iterdir()):
                if not csrow_dir.is_dir():
                    continue
                if not _re.match(r"^csrow\d+$", csrow_dir.name):
                    continue

                csrow_name = csrow_dir.name
                channels: dict = {}

                # Collect all ch<N>_ce_count files
                for ch_file in sorted(csrow_dir.glob("ch*_ce_count")):
                    # ch_file.name is like "ch0_ce_count" → ch key is "ch0"
                    ch_key = ch_file.name.replace("_ce_count", "")
                    channels[ch_key] = _read_int(ch_file)

                result[mc_name]["csrows"][csrow_name] = {
                    "ce_count": _read_int(csrow_dir / "ce_count"),
                    "ue_count": _read_int(csrow_dir / "ue_count"),
                    "channels": channels,
                }

        return result


# ---------------------------------------------------------------------------
# Auto-skip for optional heavy dependencies
# ---------------------------------------------------------------------------

# Optional deps that should cause graceful skips when not installed.
# Tests import these via pytest.importorskip() or the fixtures below.
_OPTIONAL_DEPS = {
    "prophet": "Facebook Prophet (pip install prophet)",
    "cmdstanpy": "CmdStanPy (pip install cmdstanpy)",
    "tensorflow": "TensorFlow (pip install tensorflow)",
    "torch": "PyTorch (pip install torch)",
}


@pytest.fixture
def require_prophet():
    """Skip test if prophet is not installed."""
    pytest.importorskip("prophet")


@pytest.fixture
def require_numpy():
    """Skip test if numpy is not installed."""
    pytest.importorskip("numpy")


@pytest.fixture
def require_pandas():
    """Skip test if pandas is not installed."""
    pytest.importorskip("pandas")


def pytest_configure(config):
    """Register custom markers for optional dependencies."""
    for dep, desc in _OPTIONAL_DEPS.items():
        config.addinivalue_line(
            "markers",
            f"requires_{dep}: test requires {desc}",
        )
