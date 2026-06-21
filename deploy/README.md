# SPDX-License-Identifier: Apache-2.0
# deploy/ — Deployment Reference for hw-fault-exporter

This directory contains everything needed to deploy the `hw-fault-exporter` and its
observability stack (Prometheus + Grafana + Alertmanager) in three modes:

1. **Docker Compose** — local development, single-node experimentation
2. **Kubernetes DaemonSet** — production fleet deployment (raw manifests)
3. **Helm chart** — production fleet deployment (parameterized)

---

## Docker Compose Quick-Start

### Prerequisites

- Docker Engine 24+ with Compose v2
- ARM64 host (or QEMU emulation for linux/arm64)

### Start the stack

```bash
# From the repository root:
docker compose -f deploy/docker-compose.yml up -d

# Check status:
docker compose -f deploy/docker-compose.yml ps

# Follow logs:
docker compose -f deploy/docker-compose.yml logs -f hw-fault-exporter
```

### Endpoints

| Service        | URL                          | Credentials |
|----------------|------------------------------|-------------|
| Exporter       | http://localhost:9100/metrics | —           |
| Health check   | http://localhost:9100/healthz | —           |
| Prometheus UI  | http://localhost:9090         | —           |
| Grafana        | http://localhost:3000         | admin/admin |
| Alertmanager   | http://localhost:9093         | —           |

### Grafana dashboard

The `fleet-fault-resilience.json` dashboard is auto-provisioned. Open Grafana at
`http://localhost:3000`, navigate to Dashboards → Hardware Fault Resilience →
Fleet Hardware Fault Resilience.

### Stop and clean up

```bash
docker compose -f deploy/docker-compose.yml down
docker compose -f deploy/docker-compose.yml down -v  # also remove volumes
```

---

## Kubernetes DaemonSet Deployment

### Prerequisites

- `kubectl` configured for your cluster
- ARM64 nodes labelled `kubernetes.io/arch: arm64` (standard label, set automatically)
- Prometheus scraping via annotations (no Prometheus Operator required for basic scraping)

### Deploy

```bash
# Create namespace, ServiceAccount, DaemonSet, and Service:
kubectl apply -f deploy/k8s-daemonset.yaml

# Check rollout:
kubectl -n hw-fault-resilience rollout status daemonset/hw-fault-exporter

# Verify pods are running:
kubectl -n hw-fault-resilience get pods -o wide

# Check metrics from a pod:
kubectl -n hw-fault-resilience port-forward daemonset/hw-fault-exporter 9100:9100
curl http://localhost:9100/metrics | grep edac_
```

### Prometheus Operator (ServiceMonitor)

The manifest includes a `ServiceMonitor` resource at the bottom. If you are NOT using
Prometheus Operator, delete that section. If you ARE using it (e.g. kube-prometheus-stack),
ensure the `release` label matches your Prometheus Operator's `serviceMonitorSelector`.

The default label is `release: kube-prometheus-stack`. To check your selector:

```bash
kubectl get prometheus -A -o jsonpath='{.items[*].spec.serviceMonitorSelector}'
```

### Tear down

```bash
kubectl delete -f deploy/k8s-daemonset.yaml
```

---

## Helm Chart Installation

### Prerequisites

- Helm 3.12+
- `kubectl` configured for your cluster

### Install

```bash
# Install with default values (ARM64 nodes, metrics port 9100):
helm install hw-fault-exporter ./deploy/helm \
  --namespace hw-fault-resilience \
  --create-namespace

# Install with custom image tag:
helm install hw-fault-exporter ./deploy/helm \
  --namespace hw-fault-resilience \
  --create-namespace \
  --set image.tag=v1.2.3

# Install without ServiceMonitor (no Prometheus Operator):
helm install hw-fault-exporter ./deploy/helm \
  --namespace hw-fault-resilience \
  --create-namespace \
  --set prometheus.serviceMonitor.enabled=false

# Disable specific collectors:
helm install hw-fault-exporter ./deploy/helm \
  --namespace hw-fault-resilience \
  --create-namespace \
  --set disableAER=true
```

### Upgrade

```bash
helm upgrade hw-fault-exporter ./deploy/helm \
  --namespace hw-fault-resilience \
  --set image.tag=v1.3.0
```

### Uninstall

```bash
helm uninstall hw-fault-exporter --namespace hw-fault-resilience
kubectl delete namespace hw-fault-resilience
```

### Key values

| Value | Default | Description |
|-------|---------|-------------|
| `image.repository` | `ghcr.io/hanna-hawa/fault-oracle/hw-fault-exporter` | Container image |
| `image.tag` | `1.0.0` | Image tag |
| `service.port` | `9100` | Metrics/health port |
| `nodeSelector` | `kubernetes.io/arch: arm64` | Only deploy to ARM64 nodes |
| `prometheus.serviceMonitor.enabled` | `true` | Create ServiceMonitor for Prometheus Operator |
| `sysfsRoot` | `/sys` | sysfs mount path |
| `logLevel` | `info` | Log verbosity (debug/info/warn/error) |
| `disableAER` | `false` | Disable PCIe AER collector |
| `disableMCE` | `false` | Disable MCE collector |
| `disableEDAC` | `false` | Disable EDAC collector |

---

## DPU / SmartNIC Configuration

DPU management endpoints expose metrics on port `9200` by default (configurable via
`--listen-addr`). Add DPU targets to `prometheus.yml` under the `dpu-fault-exporter` job:

```yaml
scrape_configs:
  - job_name: 'dpu-fault-exporter'
    static_configs:
      - targets:
          - '10.0.1.10:9200'   # DPU 0 management IP
          - '10.0.1.11:9200'   # DPU 1 management IP
    labels:
      role: dpu
```

For Kubernetes deployments, run a separate DaemonSet targeting DPU management nodes, or
use an additional Helm release with `service.port: 9200` and appropriate node selectors.

---

## Alertmanager Configuration

Edit `deploy/alertmanager.yml` to add your notification channels. Example Slack config:

```yaml
receivers:
  - name: 'critical'
    slack_configs:
      - api_url: 'https://hooks.slack.com/services/YOUR/WEBHOOK/URL'
        channel: '#hw-fault-alerts'
        title: 'Hardware Fault Alert: {{ .GroupLabels.alertname }}'
        text: '{{ range .Alerts }}{{ .Annotations.summary }}{{ end }}'
```

For PagerDuty:

```yaml
receivers:
  - name: 'critical'
    pagerduty_configs:
      - routing_key: 'YOUR_PAGERDUTY_ROUTING_KEY'
        severity: 'critical'
```

Reload Alertmanager config without restart:

```bash
curl -X POST http://localhost:9093/-/reload
```
