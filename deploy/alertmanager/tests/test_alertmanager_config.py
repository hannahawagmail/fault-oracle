# SPDX-License-Identifier: Apache-2.0
"""Tests for Alertmanager config structure and routing logic."""
from pathlib import Path
import pytest

CONFIG_PATH = Path(__file__).parent.parent / "alertmanager.yaml"
TEMPLATE_PATH = Path(__file__).parent.parent / "templates.tmpl"

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

pytestmark = pytest.mark.skipif(not HAS_YAML, reason="pyyaml not installed")


@pytest.fixture(scope="module")
def config():
    import yaml
    return yaml.safe_load(CONFIG_PATH.read_text())


class TestRouting:
    def test_default_receiver_exists(self, config):
        default = config["route"]["receiver"]
        receiver_names = {r["name"] for r in config["receivers"]}
        assert default in receiver_names

    def test_critical_routes_to_pagerduty(self, config):
        routes = config["route"]["routes"]
        found = any(
            r.get("receiver") == "pagerduty-critical" and
            any("critical" in str(m) for m in r.get("matchers", []))
            for r in routes
        )
        assert found

    def test_warning_routes_to_slack_warning(self, config):
        routes = config["route"]["routes"]
        found = any(
            r.get("receiver") == "slack-warning" and
            any("warning" in str(m) for m in r.get("matchers", []))
            for r in routes
        )
        assert found

    def test_recovery_webhook_in_routes(self, config):
        routes = config["route"]["routes"]
        webhook_route = next(
            (r for r in routes if r.get("receiver") == "recovery-webhook"), None
        )
        assert webhook_route is not None
        assert webhook_route.get("continue") is True

    def test_group_by_includes_instance(self, config):
        assert "instance" in config["route"]["group_by"]


class TestInhibitionRules:
    def test_node_down_inhibits_hardware_alerts(self, config):
        rules = config["inhibit_rules"]
        node_down_rule = next(
            (r for r in rules
             if any("NodeExporterDown" in str(m) for m in r["source_matchers"])),
            None
        )
        assert node_down_rule is not None
        assert "instance" in node_down_rule["equal"]

    def test_ue_inhibits_ce(self, config):
        rules = config["inhibit_rules"]
        ue_ce_rule = next(
            (r for r in rules
             if any("EDACUncorrectable" in str(m) for m in r["source_matchers"])),
            None
        )
        assert ue_ce_rule is not None
        assert "mc" in ue_ce_rule["equal"]

    def test_fast_burn_inhibits_moderate_burn(self, config):
        rules = config["inhibit_rules"]
        fb_rule = next(
            (r for r in rules
             if any("FastBurn" in str(m) for m in r["source_matchers"])),
            None
        )
        assert fb_rule is not None


class TestReceivers:
    def test_pagerduty_receiver_has_routing_key(self, config):
        pd = next(r for r in config["receivers"] if r["name"] == "pagerduty-critical")
        assert "pagerduty_configs" in pd
        assert "routing_key" in pd["pagerduty_configs"][0]

    def test_slack_warning_has_channel(self, config):
        sl = next(r for r in config["receivers"] if r["name"] == "slack-warning")
        assert sl["slack_configs"][0]["channel"] == "#hw-fault-alerts"

    def test_slack_info_has_channel(self, config):
        sl = next(r for r in config["receivers"] if r["name"] == "slack-info")
        assert sl["slack_configs"][0]["channel"] == "#hw-fault-info"

    def test_recovery_webhook_url(self, config):
        wh = next(r for r in config["receivers"] if r["name"] == "recovery-webhook")
        url = wh["webhook_configs"][0]["url"]
        assert "hw-fault-recovery" in url
        assert "/webhook" in url

    def test_all_receivers_have_name(self, config):
        for r in config["receivers"]:
            assert "name" in r
            assert r["name"]


class TestTemplates:
    def test_template_file_exists(self):
        assert TEMPLATE_PATH.exists()

    def test_slack_title_defined(self):
        content = TEMPLATE_PATH.read_text()
        assert 'define "slack.title"' in content

    def test_slack_text_defined(self):
        content = TEMPLATE_PATH.read_text()
        assert 'define "slack.text"' in content

    def test_pagerduty_description_defined(self):
        content = TEMPLATE_PATH.read_text()
        assert 'define "pagerduty.description"' in content

    def test_color_template_handles_resolved(self):
        content = TEMPLATE_PATH.read_text()
        assert "resolved" in content
        assert "good" in content
