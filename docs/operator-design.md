# Self-Healing Operator Design

## Overview

The `hw-fault-exporter-operator` is a lightweight Kubernetes controller that automatically
restarts DaemonSet pods when a hardware fault collector becomes unavailable.

## Control Loop

```
Every 60s:
  query Prometheus for fault_resilience_collector_up == 0
  for each (node, collector) pair with value 0:
    check rate-limit (max 3 restarts/hour per pair, 30s global cooldown)
    find the DaemonSet pod running on that node
    delete the pod → kubelet restarts it from the DaemonSet spec
```

## Safety Limits

| Limit | Default | Purpose |
|-------|---------|---------|
| Max restarts / (node, collector) / hour | 3 | Prevent restart loops on persistent failures |
| Global cooldown between any deletions | 30s | Avoid pod thundering herd |
| Poll interval | 60s | Reduce Prometheus query load |

## Failure Modes

- **Prometheus unavailable**: operator logs a warning, skips the cycle, retries next interval.
- **Pod not found on node**: logs an error (pod may have already been restarted), skips.
- **k8s delete fails**: logs error, retry at next cycle.
- **Persistent failure** (e.g., sysfs path genuinely missing): rate-limit kicks in after 3
  restarts, generating a warning log; the SLO alert fires separately to page on-call.

## RBAC

The operator only requires `get`/`list`/`delete` on `pods` in its own namespace.
It does not need cluster-wide permissions.
