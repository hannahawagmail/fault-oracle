"""Tests for cloud_integration module."""
from __future__ import annotations

import io
import pytest
from unittest.mock import patch, MagicMock

from remediation.cloud_integration import (
    MaintenanceEvent,
    _parse_aws_response,
    check_maintenance_events,
    trigger_live_migration,
)

AWS_XML_RESPONSE = """\
<?xml version="1.0" encoding="UTF-8"?>
<DescribeInstanceStatusResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">
  <instanceStatusSet>
    <item>
      <instanceId>i-1234567890abcdef0</instanceId>
      <eventsSet>
        <item>
          <code>instance-reboot</code>
          <description>Scheduled reboot</description>
          <notBefore>2026-07-01T00:00:00Z</notBefore>
        </item>
      </eventsSet>
    </item>
  </instanceStatusSet>
</DescribeInstanceStatusResponse>
"""


def test_aws_parse_maintenance_events():
    events = _parse_aws_response(AWS_XML_RESPONSE, "i-1234567890abcdef0")
    assert len(events) == 1
    e = events[0]
    assert e.instance_id == "i-1234567890abcdef0"
    assert e.event_type == "instance-reboot"
    assert e.not_before == "2026-07-01T00:00:00Z"
    assert e.description == "Scheduled reboot"
    assert e.provider == "aws"


def test_gcp_metadata_parsing():
    def mock_urlopen(req, **kwargs):
        url = req.full_url
        resp = MagicMock()
        if "maintenance-event" in url:
            resp.read.return_value = b"MIGRATE_ON_HOST_MAINTENANCE"
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
        elif "instance/id" in url:
            resp.read.return_value = b"123456789"
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        events = check_maintenance_events("gcp", {})
    assert len(events) == 1
    assert events[0].event_type == "MIGRATE_ON_HOST_MAINTENANCE"
    assert events[0].instance_id == "123456789"
    assert events[0].provider == "gcp"


def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unsupported provider"):
        check_maintenance_events("azure", {})


def test_trigger_live_migration_returns_bool():
    assert trigger_live_migration("aws", {}, "i-123") is False
    assert trigger_live_migration("gcp", {}, "i-123") is True


def test_trigger_live_migration_unknown_provider():
    with pytest.raises(ValueError, match="Unsupported provider"):
        trigger_live_migration("azure", {}, "i-123")
