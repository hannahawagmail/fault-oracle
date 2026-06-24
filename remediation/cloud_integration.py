"""Cloud provider maintenance API integration for fault-oracle."""
from __future__ import annotations

import argparse
import json
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime


@dataclass
class MaintenanceEvent:
    instance_id: str
    event_type: str
    not_before: str
    description: str
    provider: str


def _aws_check_events(config: dict) -> list[MaintenanceEvent]:
    """Query EC2 DescribeInstanceStatus for scheduled events via urllib."""
    region = config.get("region", "us-east-1")
    instance_id = config.get("instance_id", "")
    # Use EC2 Query API with urllib (no boto3 dependency)
    url = (
        f"https://ec2.{region}.amazonaws.com/?Action=DescribeInstanceStatus"
        f"&InstanceId.1={instance_id}&IncludeAllInstances=true&Version=2016-11-15"
    )
    headers = config.get("headers", {})
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = resp.read().decode()
    return _parse_aws_response(data, instance_id)


def _parse_aws_response(xml_text: str, instance_id: str) -> list[MaintenanceEvent]:
    """Parse EC2 XML response into MaintenanceEvent list."""
    events: list[MaintenanceEvent] = []
    # Minimal XML parsing without lxml dependency
    import xml.etree.ElementTree as ET
    ns = {"ec2": "http://ec2.amazonaws.com/doc/2016-11-15/"}
    root = ET.fromstring(xml_text)
    for item in root.iter():
        if item.tag.endswith("eventsSet"):
            for event in item:
                code = desc = not_before = ""
                for child in event:
                    tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if tag == "code":
                        code = child.text or ""
                    elif tag == "description":
                        desc = child.text or ""
                    elif tag == "notBefore":
                        not_before = child.text or ""
                if code:
                    events.append(MaintenanceEvent(
                        instance_id=instance_id, event_type=code,
                        not_before=not_before, description=desc, provider="aws",
                    ))
    return events


def _gcp_check_events(config: dict) -> list[MaintenanceEvent]:
    """Query GCP metadata server for maintenance events."""
    base = "http://metadata.google.internal/computeMetadata/v1"
    headers = {"Metadata-Flavor": "Google"}
    req = urllib.request.Request(
        f"{base}/instance/maintenance-event", headers=headers
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        event_type = resp.read().decode().strip()
    req2 = urllib.request.Request(f"{base}/instance/id", headers=headers)
    with urllib.request.urlopen(req2, timeout=5) as resp:
        iid = resp.read().decode().strip()
    events: list[MaintenanceEvent] = []
    if event_type and event_type != "NONE":
        events.append(MaintenanceEvent(
            instance_id=iid, event_type=event_type,
            not_before="", description="GCP scheduled maintenance",
            provider="gcp",
        ))
    return events


def check_maintenance_events(provider: str, config: dict) -> list[MaintenanceEvent]:
    """Abstract interface to check maintenance events across providers."""
    if provider == "aws":
        return _aws_check_events(config)
    elif provider == "gcp":
        return _gcp_check_events(config)
    raise ValueError(f"Unsupported provider: {provider}")


def trigger_live_migration(provider: str, config: dict, instance_id: str) -> bool:
    """Request live migration when failure_probability is high."""
    if provider == "aws":
        # AWS doesn't expose a direct migration API; return False
        return False
    elif provider == "gcp":
        # GCP handles live migration automatically; signal success
        return True
    raise ValueError(f"Unsupported provider: {provider}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Cloud maintenance integration")
    parser.add_argument("--provider", required=True, choices=["aws", "gcp"])
    parser.add_argument("--action", required=True, choices=["check", "migrate"])
    parser.add_argument("--instance-id", default="")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    config = {"region": args.region, "instance_id": args.instance_id}
    if args.action == "check":
        events = check_maintenance_events(args.provider, config)
        print(json.dumps([asdict(e) for e in events], indent=2))
    elif args.action == "migrate":
        result = trigger_live_migration(args.provider, config, args.instance_id)
        print(json.dumps({"migrated": result}))


if __name__ == "__main__":
    main()
