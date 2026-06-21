# SPDX-License-Identifier: Apache-2.0
"""tests/test_chaos_scenarios.py — Validate chaos scenario YAML schema."""
import yaml
from pathlib import Path
import pytest

SCENARIOS_DIR = Path(__file__).parent.parent / "scenarios"
REQUIRED_FIELDS = ["name", "description", "inject", "expect"]
REQUIRED_INJECT = ["script", "args"]
REQUIRED_EXPECT = ["alert", "alert_timeout_s", "resolve_timeout_s"]


def load_scenarios():
    return list(SCENARIOS_DIR.glob("*.yaml"))


@pytest.mark.parametrize("path", load_scenarios())
def test_scenario_has_required_fields(path):
    data = yaml.safe_load(path.read_text())
    for field in REQUIRED_FIELDS:
        assert field in data, f"{path.name} missing field: {field}"


@pytest.mark.parametrize("path", load_scenarios())
def test_scenario_inject_section(path):
    data = yaml.safe_load(path.read_text())
    for field in REQUIRED_INJECT:
        assert field in data["inject"], f"{path.name} inject missing: {field}"


@pytest.mark.parametrize("path", load_scenarios())
def test_scenario_expect_section(path):
    data = yaml.safe_load(path.read_text())
    for field in REQUIRED_EXPECT:
        assert field in data["expect"], f"{path.name} expect missing: {field}"


@pytest.mark.parametrize("path", load_scenarios())
def test_scenario_timeouts_are_positive(path):
    data = yaml.safe_load(path.read_text())
    assert data["expect"]["alert_timeout_s"] > 0
    assert data["expect"]["resolve_timeout_s"] > 0
    assert data["expect"]["resolve_timeout_s"] >= data["expect"]["alert_timeout_s"]


@pytest.mark.parametrize("path", load_scenarios())
def test_scenario_name_matches_filename(path):
    data = yaml.safe_load(path.read_text())
    assert data["name"] == path.stem, \
        f"name '{data['name']}' does not match filename '{path.stem}'"


def test_all_four_scenarios_exist():
    names = {p.stem for p in load_scenarios()}
    required = {"ce-storm", "ue-single", "aer-fatal", "multi-node-ce-cascade"}
    assert required.issubset(names), f"Missing scenarios: {required - names}"
