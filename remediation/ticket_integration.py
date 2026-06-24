"""Ticket integration for hardware fault threshold alerts."""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field, asdict
from typing import Any
from urllib.request import Request, urlopen, OpenerDirector
import base64


@dataclass
class TicketRequest:
    title: str
    description: str
    severity: str  # critical/high/medium/low
    node: str
    component: str
    labels: list[str] = field(default_factory=list)


@dataclass
class TicketResponse:
    ticket_id: str
    url: str
    status: str


SEVERITY_MAP = {"critical": "1", "high": "2", "medium": "3", "low": "4"}


def _require(config: dict, *keys: str) -> None:
    for k in keys:
        if k not in config:
            raise ValueError(f"Missing config field: {k}")


def _jira(config: dict, ticket: TicketRequest, opener: OpenerDirector | None = None) -> TicketResponse:
    _require(config, "url", "user", "token", "project")
    payload = json.dumps({"fields": {
        "project": {"key": config["project"]},
        "summary": ticket.title,
        "description": ticket.description,
        "priority": {"id": SEVERITY_MAP.get(ticket.severity, "3")},
        "labels": ticket.labels,
        "issuetype": {"name": "Bug"},
    }}).encode()
    cred = base64.b64encode(f"{config['user']}:{config['token']}".encode()).decode()
    req = Request(f"{config['url']}/rest/api/2/issue", data=payload,
                  headers={"Authorization": f"Basic {cred}", "Content-Type": "application/json"})
    resp = (opener.open(req) if opener else urlopen(req))
    body = json.loads(resp.read())
    return TicketResponse(ticket_id=body["key"], url=f"{config['url']}/browse/{body['key']}", status="created")


def _servicenow(config: dict, ticket: TicketRequest, opener: OpenerDirector | None = None) -> TicketResponse:
    _require(config, "url", "user", "token")
    payload = json.dumps({"short_description": ticket.title, "description": ticket.description,
                          "urgency": SEVERITY_MAP.get(ticket.severity, "3"), "node": ticket.node}).encode()
    cred = base64.b64encode(f"{config['user']}:{config['token']}".encode()).decode()
    req = Request(f"{config['url']}/api/now/table/incident", data=payload,
                  headers={"Authorization": f"Basic {cred}", "Content-Type": "application/json"})
    resp = (opener.open(req) if opener else urlopen(req))
    body = json.loads(resp.read())
    r = body.get("result", body)
    return TicketResponse(ticket_id=r["sys_id"], url=f"{config['url']}/nav_to.do?uri=incident.do?sys_id={r['sys_id']}", status="created")


def _webhook(config: dict, ticket: TicketRequest, opener: OpenerDirector | None = None) -> TicketResponse:
    _require(config, "url")
    payload = json.dumps(asdict(ticket)).encode()
    req = Request(config["url"], data=payload, headers={"Content-Type": "application/json"})
    resp = (opener.open(req) if opener else urlopen(req))
    body = json.loads(resp.read())
    return TicketResponse(ticket_id=body.get("id", ""), url=body.get("url", config["url"]), status="accepted")


_BACKENDS: dict[str, Any] = {"jira": _jira, "servicenow": _servicenow, "webhook": _webhook}


def create_ticket(backend: str, config: dict, ticket: TicketRequest, opener: OpenerDirector | None = None) -> TicketResponse:
    if backend not in _BACKENDS:
        raise ValueError(f"Unknown backend: {backend}")
    return _BACKENDS[backend](config, ticket, opener)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a ticket from a fault event")
    parser.add_argument("--config", required=True, help="JSON config file")
    parser.add_argument("--event", required=True, help="JSON event file")
    args = parser.parse_args()
    with open(args.config) as f:
        cfg = json.load(f)
    with open(args.event) as f:
        evt = json.load(f)
    ticket = TicketRequest(**{k: evt[k] for k in TicketRequest.__dataclass_fields__ if k in evt})
    resp = create_ticket(cfg.pop("backend", "webhook"), cfg, ticket)
    print(json.dumps(asdict(resp), indent=2))


if __name__ == "__main__":
    main()
