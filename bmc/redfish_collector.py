# SPDX-License-Identifier: Apache-2.0
"""
bmc/redfish_collector.py — Python-import shim for redfish-collector.py.

Allows test code and other Python modules to import Redfish collector
functions using the standard underscore module name convention while
the deployable script uses the hyphenated filename convention.

Usage:
    from redfish_collector import redfish_get, collect_thermal, emit_metrics
"""

import importlib.util
import sys
from pathlib import Path

_script_path = Path(__file__).parent / "redfish-collector.py"
_spec = importlib.util.spec_from_file_location("_redfish_collector_impl", _script_path)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["_redfish_collector_impl"] = _mod
_spec.loader.exec_module(_mod)

# Re-export all public names into this module's namespace.
globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
