"""Validate Prometheus rule files for correct YAML structure and promtool compatibility."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

RULE_FILES = [
    "deploy/alerts/bmc-rules.yaml",
    "deploy/alerts/cxl-rules.yaml",
    "deploy/alerts/fault-resilience-rules.yaml",
    "deploy/alerts/gpu-rules.yaml",
    "deploy/alerts/ml-rules.yaml",
    "deploy/alerts/network-rules.yaml",
    "deploy/alerts/recording-rules.yaml",
    "deploy/alerts/slo-rules.yaml",
    "deploy/alerts/storage-rules.yaml",
    "dashboards/alert-rules.yml",
    "dashboards/recording-rules.yml",
]


@pytest.fixture(params=RULE_FILES)
def rule_file(request):
    return REPO_ROOT / request.param


def _load_rules(path):
    assert path.exists(), f"Rule file not found: {path}"
    with open(path) as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), f"Expected YAML dict, got {type(data)}"
    return data


class TestPrometheusRuleStructure:
    """Validate rule file structure without promtool."""

    @pytest.fixture(autouse=True)
    def setup(self, rule_file):
        self.path = rule_file
        self.data = _load_rules(rule_file)

    def test_has_groups(self):
        assert "groups" in self.data, f"Missing 'groups' key in {self.path.name}"
        assert isinstance(self.data["groups"], list)

    def test_groups_have_rules(self):
        for group in self.data["groups"]:
            assert "name" in group, "Group missing 'name'"
            assert "rules" in group, f"Group '{group['name']}' missing 'rules'"
            assert isinstance(group["rules"], list)

    def test_rules_have_alert_or_record_and_expr(self):
        for group in self.data["groups"]:
            for rule in group["rules"]:
                has_alert = "alert" in rule
                has_record = "record" in rule
                assert has_alert or has_record, (
                    f"Rule in group '{group['name']}' missing 'alert' or 'record': {rule}"
                )
                assert "expr" in rule, (
                    f"Rule '{rule.get('alert') or rule.get('record')}' missing 'expr'"
                )


@pytest.mark.parametrize("rule_path", RULE_FILES)
def test_promtool_check(rule_path):
    """Run promtool check rules if available."""
    promtool = shutil.which("promtool")
    if promtool is None:
        pytest.skip("promtool not found in PATH")

    full_path = REPO_ROOT / rule_path
    assert full_path.exists(), f"Rule file not found: {full_path}"

    result = subprocess.run(
        [promtool, "check", "rules", str(full_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"promtool check failed for {rule_path}:\n{result.stderr}\n{result.stdout}"
    )
