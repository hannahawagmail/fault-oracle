"""Tests for ticket_integration module."""
from __future__ import annotations

import io
import json
from unittest.mock import MagicMock
import pytest

from remediation.ticket_integration import (
    TicketRequest, create_ticket, SEVERITY_MAP, _BACKENDS,
)


@pytest.fixture
def ticket():
    return TicketRequest(
        title="CE storm on node-42",
        description="Correctable errors exceeded threshold",
        severity="critical",
        node="node-42",
        component="mc0",
        labels=["hardware", "edac"],
    )


def _mock_opener(response_body: dict):
    opener = MagicMock()
    resp = MagicMock()
    resp.read.return_value = json.dumps(response_body).encode()
    opener.open.return_value = resp
    return opener


class TestJiraBackend:
    def test_payload_construction(self, ticket):
        opener = _mock_opener({"key": "HW-123"})
        config = {"url": "https://jira.example.com", "user": "bot", "token": "tok", "project": "HW"}
        resp = create_ticket("jira", config, ticket, opener=opener)

        assert resp.ticket_id == "HW-123"
        assert resp.url == "https://jira.example.com/browse/HW-123"
        assert resp.status == "created"

        call_args = opener.open.call_args[0][0]
        body = json.loads(call_args.data)
        assert body["fields"]["project"]["key"] == "HW"
        assert body["fields"]["summary"] == ticket.title
        assert body["fields"]["priority"]["id"] == "1"  # critical
        assert body["fields"]["labels"] == ["hardware", "edac"]
        assert "Basic" in call_args.get_header("Authorization")


class TestWebhookBackend:
    def test_payload_sent(self, ticket):
        opener = _mock_opener({"id": "evt-99", "url": "https://hook.example.com/evt-99"})
        config = {"url": "https://hook.example.com/incoming"}
        resp = create_ticket("webhook", config, ticket, opener=opener)

        assert resp.ticket_id == "evt-99"
        assert resp.status == "accepted"

        call_args = opener.open.call_args[0][0]
        body = json.loads(call_args.data)
        assert body["title"] == ticket.title
        assert body["node"] == "node-42"
        assert body["severity"] == "critical"


class TestMissingConfig:
    def test_jira_missing_url(self, ticket):
        with pytest.raises(ValueError, match="Missing config field: url"):
            create_ticket("jira", {"user": "x", "token": "y", "project": "P"}, ticket)

    def test_webhook_missing_url(self, ticket):
        with pytest.raises(ValueError, match="Missing config field: url"):
            create_ticket("webhook", {}, ticket)

    def test_servicenow_missing_token(self, ticket):
        with pytest.raises(ValueError, match="Missing config field: token"):
            create_ticket("servicenow", {"url": "http://x", "user": "u"}, ticket)


class TestSeverityMapping:
    @pytest.mark.parametrize("sev,expected", [
        ("critical", "1"), ("high", "2"), ("medium", "3"), ("low", "4"),
    ])
    def test_mapping(self, sev, expected):
        assert SEVERITY_MAP[sev] == expected

    def test_unknown_severity_defaults(self, ticket):
        ticket.severity = "unknown"
        opener = _mock_opener({"key": "HW-1"})
        config = {"url": "https://j.co", "user": "u", "token": "t", "project": "P"}
        create_ticket("jira", config, ticket, opener=opener)
        body = json.loads(opener.open.call_args[0][0].data)
        assert body["fields"]["priority"]["id"] == "3"  # defaults to medium
