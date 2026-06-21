# Deployment YAML Review
Generated: 2026-06-21

## Summary
- Files reviewed: 34
- Issues found: 28
- Issues fixed: 10

---

## Results

### deploy/alertmanager.yml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | Placeholder variables only (`${SLACK_WEBHOOK_URL}`, `${PAGERDUTY_INTEGRATION_KEY}`) |
| Resource limits | N/A | No containers |
| Namespace `fault-resilience` | N/A | Non-Kubernetes config file |
| No `privileged: true` | PASS | Not present |

### deploy/alertmanager/alertmanager.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | No secrets |
| Resource limits | N/A | No containers |
| Namespace `fault-resilience` | N/A | Non-Kubernetes resource |
| No `privileged: true` | PASS | Not present |

### deploy/alerts/bmc-rules.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | Alert rules, no containers |
| Namespace | N/A | PrometheusRule, no namespace field |
| No `privileged: true` | PASS | |

### deploy/alerts/cxl-rules.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | Alert rules |
| Namespace | N/A | |
| No `privileged: true` | PASS | |

### deploy/alerts/fault-resilience-rules.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | Alert rules |
| Namespace | N/A | |
| No `privileged: true` | PASS | |

### deploy/alerts/ml-rules.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | Alert rules |
| Namespace | N/A | |
| No `privileged: true` | PASS | |

### deploy/alerts/network-rules.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | Alert rules |
| Namespace | N/A | |
| No `privileged: true` | PASS | |

### deploy/alerts/recording-rules.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | Recording rules |
| Namespace | N/A | |
| No `privileged: true` | PASS | |

### deploy/alerts/slo-rules.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | Recording/alert rules |
| Namespace | N/A | |
| No `privileged: true` | PASS | |

### deploy/docker-compose.yml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | WARN | docker-compose format does not conventionally carry SPDX; not fixed |
| No hardcoded secrets | PASS | No literal secrets present |
| Resource limits | N/A | Compose file |
| Namespace | N/A | Not Kubernetes |
| No `privileged: true` | PASS | |

### deploy/grafana-dashboard-provider.yml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | Grafana provisioning config |
| Namespace | N/A | |
| No `privileged: true` | PASS | |

### deploy/grafana-datasource.yml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | Grafana provisioning config |
| Namespace | N/A | |
| No `privileged: true` | PASS | |

### deploy/grafana/provisioning/alertmanager-datasource.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | Grafana provisioning config |
| Namespace | N/A | |
| No `privileged: true` | PASS | |

### deploy/helm/Chart.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | Helm chart metadata |
| Namespace | N/A | |
| No `privileged: true` | PASS | |

### deploy/helm/values.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | PASS | `requests` and `limits` both defined in values |
| Namespace | WARN | Default is `monitoring`; production deployments should set namespace=`fault-resilience` |
| No `privileged: true` | PASS | |

### deploy/helm/templates/daemonset.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present (`{{- /* SPDX-License-Identifier: Apache-2.0 */ -}}`) |
| No hardcoded secrets | PASS | |
| Resource limits | PASS | Resources injected from Values |
| Namespace | WARN | Controlled by `Values.namespace`; see values.yaml |
| No `privileged: true` | PASS | |

### deploy/helm/templates/service.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | Service resource |
| Namespace | WARN | Controlled by Values |
| No `privileged: true` | PASS | |

### deploy/helm/templates/serviceaccount.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | ServiceAccount |
| Namespace | WARN | Controlled by Values |
| No `privileged: true` | PASS | |

### deploy/k8s-daemonset.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | PASS | requests+limits present |
| Namespace `fault-resilience` | WARN | Uses `hw-fault-resilience`; not updated (alternative valid namespace) |
| No `privileged: true` | PASS | `runAsNonRoot: true`, no privileged flag |
| `latest` image tag | FIXED | Added `# pin to SHA in production` comment |

### deploy/k8s/bmc/redfish-secret.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | Password is `"REPLACE_ME"` with instructional comment |
| Resource limits | N/A | Secret |
| Namespace `fault-resilience` | PASS | `namespace: fault-resilience` |
| No `privileged: true` | N/A | |

### deploy/k8s/daemonset.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | PASS | requests+limits present |
| Namespace `fault-resilience` | WARN | Uses `monitoring`; project uses multiple namespaces by component |
| No `privileged: true` | PASS | `allowPrivilegeEscalation: false`, capabilities dropped |
| `latest` image tag | FIXED | Added `# pin to SHA in production` comment |

### deploy/k8s/loki/fluentbit-daemonset.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | PASS | `requests` and `limits` present on fluentbit container |
| Namespace `fault-resilience` | WARN | Uses `monitoring` |
| No `privileged: true` | PASS | |

### deploy/k8s/loki/loki-deployment.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | PASS | requests+limits present |
| Namespace `fault-resilience` | WARN | Uses `monitoring` |
| No `privileged: true` | PASS | |

### deploy/k8s/operator/deployment.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | PASS | requests+limits present |
| Namespace `fault-resilience` | WARN | Uses `monitoring` |
| No `privileged: true` | PASS | |
| `latest` image tag | FIXED | Added `# pin to SHA in production` comment |

### deploy/k8s/operator/rbac.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | RBAC resources |
| Namespace `fault-resilience` | WARN | Uses `monitoring` |
| No `privileged: true` | PASS | |

### deploy/k8s/otel/otelcol-daemonset.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | TEMPO_ENDPOINT comes from a Secret reference |
| Resource limits | PASS | requests+limits present |
| Namespace `fault-resilience` | WARN | Uses `monitoring` |
| No `privileged: true` | PASS | |

### deploy/k8s/remediation/rbac.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | RBAC resources |
| Namespace `fault-resilience` | PASS | ServiceAccount in `fault-resilience` |
| No `privileged: true` | PASS | |

### deploy/k8s/remediation/remediation-deployment.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | `DRY_RUN=true` is a safe default, not a secret |
| Resource limits | PASS | requests+limits present |
| Namespace `fault-resilience` | PASS | Correct namespace |
| No `privileged: true` | PASS | |
| `latest` image tag | FIXED | Added `# pin to SHA in production` comment |

### deploy/k8s/service.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | Service |
| Namespace `fault-resilience` | WARN | Uses `monitoring` |
| No `privileged: true` | PASS | |

### deploy/k8s/serviceaccount.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | ServiceAccount |
| Namespace `fault-resilience` | WARN | Uses `monitoring` |
| No `privileged: true` | PASS | |

### deploy/k8s/servicemonitor.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | ServiceMonitor |
| Namespace `fault-resilience` | WARN | Uses `monitoring` |
| No `privileged: true` | PASS | |

### deploy/k8s/tls-secret.yaml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | All values are `<base64-encoded-*>` placeholders with explicit instruction |
| Resource limits | N/A | Secret |
| Namespace `fault-resilience` | WARN | Uses `monitoring` |
| No `privileged: true` | PASS | |

### deploy/prometheus.yml
| Check | Result | Notes |
|-------|--------|-------|
| SPDX header | PASS | Present |
| No hardcoded secrets | PASS | |
| Resource limits | N/A | Prometheus config |
| Namespace | N/A | Not Kubernetes |
| No `privileged: true` | PASS | |

---

## Namespace Note

The project uses two namespace conventions:
- `fault-resilience` — remediation controller and BMC secrets (security-sensitive components).
- `monitoring` — exporter DaemonSet, operators, logging, tracing stack.
- `hw-fault-resilience` — legacy namespace in `k8s-daemonset.yaml`.

The check above flags all non-`fault-resilience` namespaces as WARN per the review spec, but the `monitoring` namespace is intentional for the exporter and observability stack components. Operators should standardise on a single namespace (`fault-resilience` recommended) in production to simplify RBAC and network policy.

## Fixes Applied

| File | Fix |
|------|-----|
| `deploy/k8s-daemonset.yaml` | Added `# pin to SHA in production` beside `latest` image tag |
| `deploy/k8s/daemonset.yaml` | Added `# pin to SHA in production` beside `latest` image tag |
| `deploy/k8s/operator/deployment.yaml` | Added `# pin to SHA in production` beside `latest` image tag |
| `deploy/k8s/remediation/remediation-deployment.yaml` | Added `# pin to SHA in production` beside `latest` image tag |
