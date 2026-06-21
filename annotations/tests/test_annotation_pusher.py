# SPDX-License-Identifier: Apache-2.0
import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from annotation_pusher import (
    alert_fingerprint, build_annotation, TAG_PREFIX
)


def _alert(name="EDACCorrectableStorm", severity="warning", instance="node1",
           summary="CE storm detected"):
    return {
        "labels": {"alertname": name, "severity": severity, "instance": instance},
        "annotations": {"summary": summary},
        "status": {"state": "active"},
    }


class TestAlertFingerprint:
    def test_same_labels_same_fingerprint(self):
        a1 = _alert()
        a2 = _alert()
        assert alert_fingerprint(a1) == alert_fingerprint(a2)

    def test_different_labels_different_fingerprint(self):
        a1 = _alert(instance="node1")
        a2 = _alert(instance="node2")
        assert alert_fingerprint(a1) != alert_fingerprint(a2)

    def test_fingerprint_is_16_chars(self):
        fp = alert_fingerprint(_alert())
        assert len(fp) == 16

    def test_fingerprint_is_hex(self):
        fp = alert_fingerprint(_alert())
        assert all(c in "0123456789abcdef" for c in fp)


class TestBuildAnnotation:
    def test_has_required_fields(self):
        ann = build_annotation(_alert())
        assert "time" in ann
        assert "tags" in ann
        assert "text" in ann

    def test_tags_include_prefix(self):
        ann = build_annotation(_alert())
        assert TAG_PREFIX in ann["tags"]

    def test_tags_include_alert_name(self):
        ann = build_annotation(_alert(name="EDACUncorrectable"))
        assert "EDACUncorrectable" in ann["tags"]

    def test_tags_include_severity(self):
        ann = build_annotation(_alert(severity="critical"))
        assert "critical" in ann["tags"]

    def test_tags_include_instance(self):
        ann = build_annotation(_alert(instance="worker-42"))
        assert any("worker-42" in t for t in ann["tags"])

    def test_text_contains_alert_name(self):
        ann = build_annotation(_alert(name="EDACCorrectableStorm"))
        assert "EDACCorrectableStorm" in ann["text"]

    def test_text_contains_summary(self):
        ann = build_annotation(_alert(summary="Memory CE storm on mc0"))
        assert "Memory CE storm on mc0" in ann["text"]

    def test_time_is_milliseconds(self):
        import time
        ann = build_annotation(_alert())
        # Should be within 5s of now in ms
        assert abs(ann["time"] - time.time() * 1000) < 5000

    def test_no_instance_still_works(self):
        alert = {"labels": {"alertname": "Test", "severity": "info"}, "annotations": {}}
        ann = build_annotation(alert)
        assert "Test" in ann["text"]
