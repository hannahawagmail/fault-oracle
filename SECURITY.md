<!-- SPDX-License-Identifier: Apache-2.0 -->
# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.x (main branch) | Yes — active development |
| < 1.0 | No — pre-release snapshots, no security backports |

Security fixes are applied to the `main` branch and tagged as patch releases.
Older branches are not maintained.

---

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Send a report by email to:

```
hanna.hawa@gmail.com
```

Use the subject line prefix: `SECURITY:`

Example: `SECURITY: privilege escalation in inject_edac_ce.sh`

### What to include in your report

1. **Description** — a clear summary of the vulnerability and its impact
2. **Affected component** — which file(s) or script(s) are involved
3. **Steps to reproduce** — the minimal sequence of commands or conditions that trigger the issue
4. **Impact** — what an attacker could achieve (privilege escalation, information disclosure, etc.)
5. **Environment** — Linux distribution, kernel version, and any relevant configuration
6. **Suggested fix** — if you have one; optional but appreciated

Providing a proof-of-concept script is helpful but not required.

---

## Response Timeline

| Milestone | Target |
|-----------|--------|
| Acknowledgement | Within 48 hours of receipt |
| Initial assessment | Within 5 business days |
| Patch for critical vulnerabilities | Within 14 days of confirmation |
| Patch for moderate vulnerabilities | Within 30 days of confirmation |
| Public disclosure | Coordinated with reporter after patch is available |

If a patch requires more time (e.g., because it requires upstream kernel changes),
we will communicate the revised timeline to the reporter before the initial deadline.

---

## Scope

### In scope

- Shell scripts that, when run as root or with elevated privileges on an embedded Linux system,
  could allow unintended privilege escalation, arbitrary code execution, or persistent system
  modification beyond their stated purpose
- The Prometheus exporter binary (`exporter/`) if it exposes sensitive host data beyond
  the documented metric set, or if it can be exploited to execute code on the host
- Kubernetes manifests or Helm chart templates that grant excessive permissions to the
  DaemonSet pod (e.g., unnecessary host namespace access, over-privileged RBAC)
- Fault-injection scripts that could cause unrecoverable damage to a system when run
  in a non-test environment without adequate warnings

### Out of scope

- Issues that require physical access to the target hardware
- Denial-of-service against the Prometheus metrics endpoint (it is intended to be
  network-accessible within a trusted monitoring network)
- Vulnerabilities in third-party dependencies (Prometheus, Grafana, Alertmanager) —
  please report those to the upstream projects directly
- Security issues that only affect systems running with the kernel fault-injection
  framework enabled (`CONFIG_FAULT_INJECTION=y`) in production — this configuration
  is documented as a CI/test-only setting
- Missing HTTPS on the metrics endpoint — the exporter is designed for use behind
  a network perimeter or service mesh that handles TLS termination

---

## Credits

Responsibly disclosed vulnerabilities will be credited in the release notes and
CHANGELOG unless the reporter prefers to remain anonymous.
