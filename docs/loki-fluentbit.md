# Loki + Fluentbit Log Aggregation

## Overview

The exporter emits structured JSON logs (via zap) to stdout. Fluentbit ships them to Loki, enabling correlated log+metric views in Grafana.

## Log format

```json
{"level":"info","ts":"2026-06-20T14:31:00.000Z","caller":"collectors/edac.go:87","msg":"EDAC scrape","mc":"0","ce_count":3,"ue_count":0}
{"level":"warn","ts":"2026-06-20T14:31:10.000Z","caller":"collectors/aer.go:42","msg":"AER sysfs path missing","path":"/sys/bus/pci/devices/0000:01:00.0/aer_dev_correctable"}
```

## Stack components

| Component | Role |
|-----------|------|
| `fluent-bit` DaemonSet | Tails `/var/log/containers/hw-fault-exporter-*.log`, parses JSON, ships to Loki |
| `loki` Deployment | Log storage + query backend |
| Grafana Loki datasource | Renders logs inline with metric timeseries |

## Deployment

```bash
kubectl apply -f deploy/k8s/loki/loki-deployment.yaml
kubectl apply -f deploy/k8s/loki/fluentbit-daemonset.yaml
```

Add the Loki datasource in Grafana pointing to `http://loki.monitoring.svc:3100`.

## Correlated view

Import `deploy/grafana/loki-correlated-panels.json`. The dashboard shows:
1. EDAC CE rate (Prometheus) and log error count (Loki) on the same timeseries
2. Raw log panel below, auto-filtered by node and log level

LogQL query used:
```logql
{job="hw-fault-exporter", node=~"$node"} |= "" | json | level=~"$log_level"
```
