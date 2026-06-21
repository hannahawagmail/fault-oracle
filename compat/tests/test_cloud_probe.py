# SPDX-License-Identifier: Apache-2.0
import json, subprocess
from pathlib import Path

PROBE_SH = Path(__file__).parent.parent / "cloud-probe.sh"

def run(args=None):
    return subprocess.run(["bash", str(PROBE_SH)] + (args or []), capture_output=True, text=True)

def test_probe_exits_0():
    assert run().returncode == 0

def test_probe_json_valid():
    r = run(["--json"])
    assert r.returncode == 0
    json.loads(r.stdout)

def test_probe_json_has_all_collectors():
    data = json.loads(run(["--json"]).stdout)
    for key in ["edac", "mce", "aer", "thermal", "cpufreq", "pmu"]:
        assert key in data["collectors"]

def test_probe_values_are_binary():
    data = json.loads(run(["--json"]).stdout)
    for k, v in data["collectors"].items():
        assert v in (0, 1), f"{k}={v}"

def test_probe_text_has_product_line():
    assert "Product:" in run().stdout
