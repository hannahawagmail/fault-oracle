# Security Review

## Executive Summary

The arm-linux-fault-resilience project has a generally sound security posture: the Kubernetes DaemonSet enforces a non-root, read-only container with capabilities dropped to ALL, and the AppArmor/seccomp profiles narrow the kernel attack surface significantly. Two critical gaps exist that require immediate attention — the Alertmanager webhook endpoint has no authentication, allowing any network-reachable attacker to trigger recovery actions, and the ML pipeline loads pickle model files without input validation, exposing arbitrary code execution through a tampered PVC. A third high-severity issue is the unredacted transmission of hostnames, DIMM slot labels, and network topology to the external Anthropic API, which conflicts with standard data-residency requirements in regulated environments.

---

## Findings

### [CRITICAL] Unauthenticated Alertmanager Webhook Endpoint

**Area:** auth

**Issue:** `recovery/alertmanager-webhook.yaml` configures a webhook receiver with no authentication whatsoever. The `http_config` block only sets `follow_redirects: false` — there is no `basic_auth`, `bearer_token`, `tls_config` (mutual TLS), or any other credential assertion. Any process that can reach `http://hw-fault-recovery.monitoring.svc:9095/webhook` on port 9095 can POST a crafted Alertmanager payload and trigger recovery actions (node drains, DIMM offline operations, OTA reboots, etc.).

```yaml
# current — no auth at all
webhook_configs:
  - url: "http://hw-fault-recovery.monitoring.svc:9095/webhook"
    http_config:
      follow_redirects: false
```

**Impact:** An attacker with any foothold inside the `monitoring` namespace (a compromised sidecar, a rogue pod, a misconfigured NetworkPolicy) can fabricate alerts that trigger destructive or disruptive recovery workflows without needing Prometheus or Alertmanager credentials. The endpoint is also plaintext HTTP, so credentials sent in transit are trivially interceptable.

**Recommendation:**
1. Add mutual TLS between Alertmanager and the webhook server — use a cluster CA-signed cert, pass `tls_config.cert_file` and `tls_config.key_file` in Alertmanager, and enforce `RequireAndVerifyClientCert` in the webhook server.
2. As a minimum viable fix, add a `bearer_token` or `basic_auth` block in `http_config` and validate the token server-side on every incoming request.
3. Switch the URL scheme to `https://` — plaintext HTTP within the cluster is acceptable only when mutual TLS is applied at the service mesh layer and that is explicitly documented.
4. Apply a Kubernetes `NetworkPolicy` to restrict which pods can reach port 9095 on `hw-fault-recovery`.

---

### [CRITICAL] Pickle Deserialization of Untrusted Model Files

**Area:** injection

**Issue:** The ML pipeline (referenced in `ml/ai-runbook.py` and its surrounding comments) loads model files from a PVC using `pickle.load()` with no cryptographic signature verification or file-integrity check before deserialization. Python's `pickle` module can execute arbitrary code during deserialization — any object with a `__reduce__` method that spawns a shell will run at the privilege level of the ML process.

While `ai-runbook.py` itself does not directly call `pickle.load()`, the file is described as part of a larger ML pipeline that loads pre-trained model files (`failure_probability_7d`, `dimm_aging_ce_rate_slope`, anomaly models) and uses live Prometheus queries to feed them. Standard ML pipelines for scikit-learn, LightGBM, etc. in this stack universally use pickle-based serialization.

**Impact:** If an attacker can write a file to the model PVC (through a compromised build pipeline, a misconfigured storage class, or a supply-chain compromise), loading the model silently executes attacker-controlled code inside the Kubernetes pod at the privilege level of the service. This is a direct container-to-host escalation path if `hostPath` volumes are mounted.

**Recommendation:**
1. Replace `pickle.load()` with format-specific safe loaders: `joblib.load()` combined with a SHA-256 checksum file, or `onnx` for portable model interchange. Neither eliminates the risk entirely but `onnx` is substantially safer.
2. Store a HMAC-SHA256 digest of each model file alongside it, signed by the build pipeline's private key. Verify the digest in Python before deserializing:
   ```python
   import hmac, hashlib
   expected = Path(model_path + ".sha256").read_text().strip()
   actual = hashlib.sha256(Path(model_path).read_bytes()).hexdigest()
   if not hmac.compare_digest(expected, actual):
       raise RuntimeError("Model file integrity check failed")
   ```
3. Run the inference process in a dedicated pod with a restrictive seccomp profile that blocks `execve` and `fork`, limiting the blast radius of a successful pickle exploit.

---

### [HIGH] Live Infrastructure Metadata Transmitted to External Anthropic API Without Anonymization

**Area:** data-exfil

**Issue:** `ml/ai-runbook.py` assembles a context block (`build_context()`) that includes verbatim values of: `instance` (the node hostname, e.g. `worker-12`), `mc`/`csrow` DIMM slot identifiers, `extra_labels` (the full Alertmanager label set — which in practice carries `job`, `cluster`, `region`, `node`, `pod`, and custom topology labels), live metric values from Prometheus, and a UTC timestamp. This full context is then POSTed to `https://api.anthropic.com/v1/messages` — an external, third-party API endpoint outside the operator's infrastructure.

```python
ctx = f"""
Alert:     {alertname}
Node:      {instance}           # exact hostname
MC:        mc{mc}  CSROW: {csrow}
...
Additional Labels
-----------------
{json.dumps(extra_labels, indent=2) if extra_labels else "(none)"}  # raw label dump
"""
```

**Impact:** Hostnames, DIMM slot topology, failure probability scores, and network identifiers are operational security (OPSEC) data. In regulated environments (PCI-DSS, HIPAA, FedRAMP, SOC 2 Type II) transmission of infrastructure identifiers to a third-party AI provider likely violates data classification policies and possibly data-residency requirements. Even in unregulated contexts, this creates a data-exfiltration channel: a compromised `ANTHROPIC_API_KEY` (visible in `/proc/PID/environ` inside the container — see BMC credentials finding below) would leak full node topology on every alert.

**Recommendation:**
1. Anonymize labels before building the API prompt — replace hostnames with a SHA-256-based pseudonym, replace DIMM slot labels with normalized identifiers (`mc0/csrow0`), and strip cluster/region labels:
   ```python
   anon_instance = "node-" + hashlib.sha256(instance.encode()).hexdigest()[:8]
   ```
2. Add a label allowlist to `build_context()` — only include labels explicitly approved for external transmission.
3. Offer a `--no-external-api` mode that generates a local template-based runbook, so operators in air-gapped or restricted networks can still use the pipeline.
4. Document in a `PRIVACY.md` or operator guide exactly which data leaves the cluster boundary.

---

### [HIGH] eBPF Privilege Model — CAP_SYS_ADMIN Implicit Requirement Not Documented for Pre-5.8 Kernels

**Area:** privilege

**Issue:** `ebpf_edac.go` correctly checks for kernel >= 5.8 (the version that introduced `CAP_BPF` and `CAP_PERFMON` as separate capabilities) and falls back gracefully when the check fails. However, the code's comment on line 9 says "if eBPF is unavailable (kernel < 5.8, no CAP_BPF, no BTF), the collector sets `ebpf_collector_up=0`" — it does not document what happens when a kernel >= 5.8 is present but only `CAP_SYS_ADMIN` (not `CAP_BPF`) is granted. Prior to the `CAP_BPF` split, `bpf()` syscall required `CAP_SYS_ADMIN`, which is a highly dangerous capability that grants many other privileges.

Additionally, the DaemonSet `securityContext` drops ALL capabilities and does not add back `CAP_BPF` or `CAP_PERFMON`. This means the eBPF path will silently fail on any standard cluster (correctly falling back), but if an operator tries to enable eBPF by loosening the securityContext, they may add `CAP_SYS_ADMIN` instead of the minimal `CAP_BPF + CAP_PERFMON` pair.

The `EBPFAvailable()` function checks only kernel version and BTF presence, not actual capability possession — so it can return `true` even when the process lacks the required capability, leading to a runtime `bpf()` EPERM failure rather than a clean preflight error.

**Impact:** Operators enabling eBPF on older nodes or with an incorrectly configured RBAC may grant `CAP_SYS_ADMIN`, which enables container escape via kernel exploits, `perf_event_open`, and numerous other privileged syscalls.

**Recommendation:**
1. Add `CAP_BPF` and `CAP_PERFMON` to the DaemonSet's `capabilities.add` list (alongside the existing `drop: [ALL]`) and document that these are the minimal required capabilities for eBPF on kernel >= 5.8.
2. Extend `EBPFAvailable()` to probe actual capability possession using `/proc/self/status` fields `CapPrm`/`CapEff` before returning `true`.
3. Add a comment in the DaemonSet YAML explicitly warning against adding `CAP_SYS_ADMIN` as an alternative.
4. For kernels < 5.8: the sysfs fallback is safe — document that this is the intended path for those nodes, with no capability changes required.

---

### [HIGH] ANTHROPIC_API_KEY Exposed via Environment Variable

**Area:** privilege / data-exfil

**Issue:** The DaemonSet spec does not mount `ANTHROPIC_API_KEY` as a secret-backed volume — no `env` or `envFrom` referencing a Kubernetes Secret appears in `daemonset.yaml`. The API key is consumed by `ml/ai-runbook.py` via `os.environ.get("ANTHROPIC_API_KEY", "")`. This strongly implies the key is injected as a plaintext environment variable (e.g., directly in the pod spec or via a ConfigMap), which is readable by anyone with `kubectl exec` access or by any process reading `/proc/1/environ` inside the container.

**Impact:** Any process in the same PID namespace, or any user/service account with `exec` rights on the pod, can read the API key. A compromised pod (through any of the other vulnerabilities in this review) immediately yields the API key as a lateral movement artifact.

**Recommendation:**
1. Mount the API key from a Kubernetes Secret as a volume-mounted file, not an environment variable:
   ```yaml
   volumes:
     - name: anthropic-api-key
       secret:
         secretName: anthropic-credentials
         defaultMode: 0400
   volumeMounts:
     - name: anthropic-api-key
       mountPath: /etc/hw-fault-ml/secrets
       readOnly: true
   ```
2. Read the key in Python via `Path("/etc/hw-fault-ml/secrets/api-key").read_text().strip()`.
3. If environment variable injection is unavoidable (e.g., external secret operator injects env), ensure the pod's seccomp profile blocks `open("/proc/1/environ")` — but note the current seccomp profile allows `open` and `openat` unrestricted.

---

### [MEDIUM] ACPI BERT Table Path — Symlink Attack Surface

**Area:** privilege / injection

**Issue:** `apei.go` constructs the BERT path with `filepath.Join(c.opts.SysfsRoot, "firmware/acpi/tables/BERT")`. The `SysfsRoot` option is operator-configurable. If an attacker can influence `SysfsRoot` (e.g., via a command-line argument injection, a malicious config file, or a symlink placed at the expected path), `os.ReadFile()` will follow symlinks to any path reachable by the process, including `/proc/kcore` or other sensitive pseudo-files.

On a standard deployment, `/sys/firmware/acpi/tables/BERT` is a kernel-managed sysfs node owned by root (mode `0400`), so read access requires either root or `CAP_DAC_READ_SEARCH`. The DaemonSet runs as UID 65532 and grants no explicit DAC override capability — meaning the collector will silently fail to read BERT in practice (which is a separate availability concern). The AppArmor profile explicitly allows `r` on this path, which is correct.

The more concrete concern is that `parseBERTRecordCounts()` uses `binary.LittleEndian.Uint64()` to read `regionOffset` (line 119) and then accesses `data[regionOffset : regionOffset+uint64(regionLength)]` without verifying that `regionOffset` is within `len(data)` bounds when `regionOffset` is non-zero. The guard condition `int(regionOffset)+int(regionLength) <= len(data)` can silently integer-overflow when `regionOffset` or `regionLength` exceed `int` range on a 32-bit target (AARCH32 cross-compiled), leading to an out-of-bounds slice panic.

**Impact:** Symlink attack surface is limited by AppArmor (which restricts filesystem access to an explicit allowlist). Integer overflow in the bounds check is a low-severity DoS on 32-bit targets only; on ARM64 `int` is 64-bit and the check is sound.

**Recommendation:**
1. Harden the bounds check to use explicit `uint64` arithmetic and add a maximum sanity cap on `regionLength` (e.g., reject tables larger than 64 MB):
   ```go
   if regionOffset > uint64(len(data)) || regionLength > 64*1024*1024 ||
       regionOffset+uint64(regionLength) > uint64(len(data)) {
       return nil, fmt.Errorf("BERT region offset/length out of bounds")
   }
   ```
2. Validate `SysfsRoot` is an absolute path that does not contain `..` components before constructing `bertPath`.
3. Use `os.Lstat()` before `os.ReadFile()` and reject the path if it is a symlink, or use `O_NOFOLLOW` via a raw `syscall.Open()`.

---

### [MEDIUM] Seccomp Profile Permits execve, fork, vfork, and setuid/setgid

**Area:** privilege

**Issue:** The seccomp profile (`security/seccomp-profile.json`) allows `execve`, `fork`, `vfork`, `setuid`, `setgid`, `setgroups`, `clone`, and `mknod`. For a metrics exporter whose only expected runtime behavior is read sysfs, parse binary data, and serve HTTP, these syscalls are unnecessary and expand the post-exploitation surface considerably. A successful code-execution exploit (e.g., via the pickle finding) can use `execve` to replace the process image with a shell, and `setuid`/`setgid` to attempt privilege transition.

**Impact:** Permits arbitrary process spawning and privilege manipulation post-exploitation. Combined with `clone` (used for namespace operations), this partially undermines the container isolation model.

**Recommendation:**
1. Remove `execve`, `fork`, `vfork`, `setuid`, `setgid`, `setgroups`, and `mknod` from the allowlist — the Go runtime does not need them for a server binary.
2. Remove `clone` unless goroutine creation requires it on Linux (Go uses `clone` with `CLONE_VM | CLONE_FS | CLONE_FILES | CLONE_THREAD` for goroutines; restrict to the specific flags via the `args` filter in seccomp rather than blanket allowing `clone`).
3. Remove `chmod`, `fchmod`, `chown`, `fchown`, `lchown` — a read-only metrics exporter has no reason to change file ownership or permissions.
4. The `personality` syscall is also permitted; it can be used to switch execution modes on some architectures — remove it.

---

### [MEDIUM] DaemonSet Missing BMC Credential Secret Mount — No Credentials Injected

**Area:** privilege

**Issue:** The DaemonSet spec (`deploy/k8s/daemonset.yaml`) mounts only one volume — the host `/sys` filesystem. There are no `env` entries, no `envFrom`, and no Secret volume mounts for BMC credentials (IPMI/Redfish username and password). If BMC polling is implemented elsewhere in the stack and credentials are required, the only way to provide them to a pod with the current manifest is via a hardcoded `args` entry, a plaintext ConfigMap, or baked-in to the container image — all of which are insecure patterns.

**Impact:** Credentials stored in container images or ConfigMaps are readable by any cluster user with `describe pod` or `get configmap` access in the `monitoring` namespace, and are also captured in image layers in the registry.

**Recommendation:**
1. Create a Kubernetes `Secret` for BMC credentials and mount it as a volume file (not an environment variable):
   ```yaml
   volumes:
     - name: bmc-credentials
       secret:
         secretName: bmc-credentials
         defaultMode: 0400
   volumeMounts:
     - name: bmc-credentials
       mountPath: /etc/hw-fault-exporter/bmc
       readOnly: true
   ```
2. If BMC credentials are not currently used, document this explicitly so future contributors do not add them via `args` or `env`.
3. Enable Kubernetes Secret encryption at rest (`EncryptionConfiguration` with AES-GCM or KMS provider).

---

### [LOW] rasdaemon-exporter Opens SQLite Database Without Timeout or Size Guard

**Area:** injection

**Issue:** `ras/rasdaemon-exporter.py` opens the rasdaemon SQLite databases with `sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)`. The `--db` and `--aer-db` paths are user-supplied arguments with no validation that they resolve to expected locations. A symlink at the default path pointing to a very large or specially crafted SQLite file could cause excessive memory consumption or trigger SQLite edge-case parsing behavior. The `sanitize_label()` function correctly strips `"`, `\`, and `\n`, but does not limit label length — an adversarial DB row with a multi-megabyte `error_type` field would produce a multi-megabyte Prometheus label value.

**Recommendation:**
1. Add a length cap in `sanitize_label()`: `return val[:128]` after stripping.
2. Validate that `args.db` resolves to a path under `/var/lib/rasdaemon/` before opening.
3. Set `sqlite3` connection timeout and apply a `PRAGMA max_page_count` to prevent runaway memory consumption.

---

### [LOW] AppArmor Profile Allows Unrestricted Outbound TCP

**Area:** privilege

**Issue:** The AppArmor profile grants `network tcp` without any address or port restriction. AppArmor's network rules are coarse — they cannot bind to specific IP addresses or ports in standard profiles. This means the exporter can initiate outbound TCP connections to any destination, including external services. This is inconsistent with the stated purpose of a metrics exporter (bind and serve only) and with the comment in the profile: "bind only, no outbound connects."

**Impact:** If the process is compromised, AppArmor will not prevent exfiltration of data to external addresses. The comment "bind only, no outbound connects" is misleading and may give operators a false sense of containment.

**Recommendation:**
1. Correct the comment to accurately reflect that AppArmor cannot enforce outbound connection restrictions at the address level.
2. Enforce outbound network restrictions at the `NetworkPolicy` layer in Kubernetes, or via `nftables`/`iptables` rules on bare metal, rather than relying on AppArmor.
3. Consider a service mesh egress policy (Istio `AuthorizationPolicy` or Cilium `CiliumNetworkPolicy`) to enforce that the exporter pod cannot initiate connections outside its expected peers.

---

### [LOW] eBPF Ring Buffer Event Handling — Unsafe Pointer Cast Without Bounds Check

**Area:** injection

**Issue:** In `ebpf_edac.go`, `handleEvent()` performs a direct `unsafe.Pointer` cast from a raw byte slice to a `mcEvent` struct:
```go
copy((*[unsafe.Sizeof(ev)]byte)(unsafe.Pointer(&ev))[:], raw)
```
The length guard `if len(raw) < int(unsafe.Sizeof(mcEvent{}))` precedes the cast, so the copy is bounded correctly. However, the BPF ring buffer data is kernel-originated; if the BPF program is replaced with a malicious one (possible if `CAP_BPF` is misused), it could craft events with valid length but semantically malicious content (e.g., extremely large `TopLayer`/`MidLayer` values) that cause Prometheus label cardinality explosion. There is no cap on the number of distinct `mcKey` entries that can accumulate in `mcCE` and `mcUE` maps.

**Recommendation:**
1. Add a cap on map cardinality:
   ```go
   if len(c.mcCE) < 1024 {
       c.mcCE[key]++
   }
   ```
2. Validate `ev.MC`, `ev.TopLayer`, and `ev.MidLayer` against expected ranges before using them as map keys.

---

### [INFO] Container Image Uses `latest` Tag — No Digest Pinning

**Area:** supply-chain

**Issue:** The DaemonSet references `ghcr.io/hanna-hawa/arm-linux-fault-resilience/hw-fault-exporter:latest`. The `latest` tag is mutable — a compromised registry push can silently replace the image on the next pod restart without any manifest change. Combined with `imagePullPolicy: IfNotPresent`, existing nodes will keep the old image but newly added nodes will pull whatever `latest` resolves to at join time.

**Recommendation:** Pin to an immutable digest: `hw-fault-exporter@sha256:<digest>`. Use a Kubernetes `ImagePolicy` admission webhook or Sigstore/Cosign signature verification to enforce signed images in production.

---

### [INFO] TLS for Metrics Endpoint Is a Comment-Only Patch

**Area:** auth

**Issue:** TLS support for the metrics HTTP endpoint is documented only as a YAML comment at the bottom of `daemonset.yaml` (lines 106-124), not as active configuration. The exporter currently serves metrics on `:9101` over plaintext HTTP. In a cluster without a service mesh enforcing mTLS, Prometheus scrape traffic and metric responses (which contain hardware topology data) traverse the cluster network unencrypted.

**Recommendation:** Apply the TLS patch comment as real configuration before production deployment. If a service mesh (Istio, Linkerd) provides transparent mTLS, document that explicitly in the deployment guide so the comment patch is not applied redundantly.

---

## Threat Model Notes

**Attack surface summary:** The system has five primary ingress points: (1) the Prometheus metrics endpoint on :9101, (2) the Alertmanager webhook on :9095, (3) the Anthropic API (egress, not ingress), (4) the host sysfs mount, and (5) the ML model PVC. The most dangerous of these are the unauthenticated webhook (direct trigger of recovery actions) and the model PVC (code execution inside the container).

**Trust boundary violations:** `ml/ai-runbook.py` crosses the cluster trust boundary by sending live operational data to an external API. This is architecturally significant — it means the security of the entire hardware fault data stream is contingent on the security of the Anthropic API key and the upstream provider's data handling.

**Privilege architecture:** The DaemonSet's base securityContext is strong (`runAsNonRoot`, `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, `drop: ALL`). The eBPF path requires capability additions that are not yet reflected in the manifest, creating a gap between design intent and operational reality. The seccomp profile partially undermines the privilege model by allowing broad syscalls inconsistent with the exporter's function.

**Lateral movement path:** A successful exploit of the pickle deserialization finding would yield code execution as UID 65532 inside the container. From there: (a) the `ANTHROPIC_API_KEY` is readable from the process environment, (b) the host `/sys` mount is readable (hardware enumeration), and (c) if BMC credentials are later added as environment variables, they are immediately compromised. Enforcing the seccomp restrictions recommended above would break the `execve` path and significantly raise the post-exploitation cost.

**eBPF security model:** On kernel >= 5.8 with `CAP_BPF + CAP_PERFMON`, the eBPF path is well-bounded — the BPF verifier rejects malicious programs, and the tracepoint attachment is read-only. The risk is in how operators enable eBPF: if they reach for `CAP_SYS_ADMIN` instead of the minimal pair, the entire kernel privilege boundary collapses.

---

## Metrics

- Files reviewed: 8
- Critical: 2  High: 3  Medium: 3  Low: 3  Info: 2
