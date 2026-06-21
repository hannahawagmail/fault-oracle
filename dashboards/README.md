# dashboards — Grafana Dashboard

## fleet-fault-resilience.json

A Grafana dashboard for fleet-level hardware fault visibility. Panels:

- **CE Rate / Correctable Errors** — per-controller CE rate (errors/min)
- **UE Events** — any uncorrectable errors (threshold alert: > 0)
- **CE Storm Heatmap** — csrow × channel CE counts as a heatmap
- **PCIe AER Correctable Rate** — BadTLP, ReceiverError per device
- **PCIe AER Uncorrectable Events** — CompletionTimeout, SurpriseDown
- **MCE Events** — machine check events by severity
- **Exporter Health** — scrape duration, error rate

## Import

1. Grafana → Dashboards → Import
2. Upload `fleet-fault-resilience.json`
3. Select your Prometheus data source (default variable: `$datasource`)

## Alerting

Recommended Grafana alert rules:

```
# Any UE in 15 minutes — critical
increase(edac_uncorrectable_errors_total[15m]) > 0

# CE rate > 10/min on any controller — warning
rate(edac_controller_ce_total[5m]) * 60 > 10

# AER BadTLP rate > 5/min on any device — warning
rate(pcie_aer_correctable_total{error_type="BadTLP"}[5m]) * 60 > 5

# Exporter scrape failing — warning
hw_fault_exporter_scrape_errors_total > 5
```

For production-grade alerting, use the Prometheus alerting rules in
`alert-rules.yml` rather than Grafana alerts — they support inhibition,
routing, and on-call integration via Alertmanager.

---

## Prometheus Alerting Rules (alert-rules.yml)

`alert-rules.yml` contains 11 production-ready Prometheus alerting rules
covering EDAC memory errors, PCIe AER errors, MCE events, and exporter
health.  Each rule includes inline threshold rationale comments.

### Standalone Prometheus

Add the file to your `prometheus.yml`:

```yaml
rule_files:
  - /etc/prometheus/alert-rules.yml
  - /etc/prometheus/recording-rules.yml
```

Then reload Prometheus:

```bash
curl -X POST http://localhost:9090/-/reload
# or
systemctl reload prometheus
```

Verify the rules loaded:

```bash
curl -s http://localhost:9090/api/v1/rules | python3 -m json.tool | grep '"name"'
```

### Kubernetes / Prometheus Operator (PrometheusRule CRD)

Wrap the rule groups in a PrometheusRule custom resource:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: hw-fault-resilience
  namespace: monitoring
  labels:
    # Must match the ruleSelector in your Prometheus CR:
    prometheus: kube-prometheus
    role: alert-rules
spec:
  groups:
    # Paste the contents of alert-rules.yml "groups:" section here,
    # or use kustomize to inline the file.
```

Apply with kubectl:

```bash
kubectl apply -f alert-rules.yml
# Prometheus Operator will pick up the rule automatically.

# Verify:
kubectl get prometheusrule -n monitoring hw-fault-resilience
```

A combined PrometheusRule covering both alert and recording rules can be
generated with:

```bash
cat > hw-fault-prometheusrule.yml <<'EOF'
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: hw-fault-resilience
  namespace: monitoring
  labels:
    prometheus: kube-prometheus
    role: alert-rules
spec:
EOF
# Append alert groups (strip file-level comments):
echo "  groups:" >> hw-fault-prometheusrule.yml
grep -v "^#\|^$\|^groups:" alert-rules.yml \
  | sed 's/^/    /' >> hw-fault-prometheusrule.yml
echo "  # Recording rules:" >> hw-fault-prometheusrule.yml
grep -v "^#\|^$\|^groups:" recording-rules.yml \
  | sed 's/^/    /' >> hw-fault-prometheusrule.yml
kubectl apply -f hw-fault-prometheusrule.yml
```

---

## Recording Rules (recording-rules.yml)

`recording-rules.yml` pre-computes five expensive range-vector queries as
instant-vector time series.  This significantly improves dashboard load time
on large fleets where many nodes contribute to the same metric.

| Recording metric | Source query |
|---|---|
| `job:edac_ce:rate1h` | `sum by (job, instance, controller) (rate(edac_controller_ce_total[1h]))` |
| `job:edac_ue:increase5m` | `sum by (job, instance, controller) (increase(edac_controller_ue_total[5m]))` |
| `job:pcie_aer_correctable:rate1h` | `sum by (job, instance, device, error_type) (rate(pcie_aer_correctable_total[1h]))` |
| `job:mce_corrected:rate1h` | `rate(mce_events_total{severity="corrected"}[1h])` |
| `job:hw_fault_exporter:scrape_error_rate5m` | `rate(hw_fault_exporter_scrape_errors_total[5m])` |

---

## Alert Response Runbook

When an alert fires, consult **`docs/runbook.md`** for step-by-step operator
procedures, including:

- Shell commands to isolate the failing DIMM or PCIe device
- Escalation criteria and thresholds
- Controlled page-offline and node drain procedures
- DIMM replacement decision tree (ASCII flowchart)
- Post-replacement verification checklist
