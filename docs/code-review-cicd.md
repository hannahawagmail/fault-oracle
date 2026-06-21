# Code Review: CI/CD Pipeline and Makefile

## Summary

Four files were reviewed: the GitHub Actions workflow (`ci/build-and-test.yml`),
the top-level `Makefile`, the eBPF sub-`Makefile` (`ebpf/Makefile`), and the
pre-commit configuration (`.pre-commit-config.yaml`). The pipeline is
substantially mature — it has multi-kernel matrix builds, SBOM generation,
CVE scanning, and good coverage gating. However, several issues range from
blocking security/correctness problems down to style nits.

---

## Findings

### [BLOCKER] vmlinux.h stub is a single-line comment — clang syntax check will fail on any BPF CO-RE include

File: `ci/build-and-test.yml`  Line: 865–867  
File: `ebpf/Makefile`  Line: 26

**Issue:** When `/sys/kernel/btf/vmlinux` is absent (which it is on every
standard GitHub-hosted `ubuntu-*` runner because the runner kernel is not
compiled with `CONFIG_DEBUG_INFO_BTF=y`), the CI step generates a stub:

```bash
echo "/* stub */" > ebpf/vmlinux.h
```

Any eBPF C source that contains `#include "vmlinux.h"` and then uses
kernel types (`struct task_struct`, `__u64`, BPF CO-RE field accessors, etc.)
will emit an avalanche of `undeclared identifier` errors when compiled even
with `-fsyntax-only`. A one-line comment is not a valid substitute — the
syntax check will fail as if vmlinux.h were empty.

The `ebpf/Makefile`'s `vmlinux:` target has the same problem: it calls
`bpftool btf dump` unconditionally with no fallback at all, so `make` will
abort on any host without BTF support.

**Fix:** Generate a minimal but type-complete stub. At a minimum it must
define the primitive types and a handful of used kernel structs.
A common approach is to vendor a pre-generated `vmlinux.h` for a recent LTS
kernel inside the repository (or download it from the `vmlinux.h` GitHub
releases maintained by the libbpf project). Alternatively, use
`-D __VMLINUX_H__` to guard the include and provide `-D`-based
forward-declarations for any CO-RE types actually used in the source:

```bash
# In CI, when BTF is absent:
curl -fsSL \
  https://github.com/libbpf/libbpf-bootstrap/raw/main/vmlinux/arm64/vmlinux.h \
  -o ebpf/vmlinux.h
```

At minimum the stub must contain:
```c
typedef unsigned char      __u8;
typedef unsigned short     __u16;
typedef unsigned int       __u32;
typedef unsigned long long __u64;
typedef signed long long   __s64;
/* ... other types used by edac_trace.bpf.c and workload_attr.bpf.c ... */
```

---

### [BLOCKER] CVE container scan builds without pinned base image — trivy scan target is non-reproducible

File: `ci/build-and-test.yml`  Line: 533

**Issue:** The security job builds the container image for scanning with:

```yaml
run: docker build -t hw-fault-exporter:scan exporter/
```

This invocation uses whatever `Dockerfile` is present in `exporter/` at build
time — and if that `Dockerfile` references a base image without a digest pin
(e.g. `FROM golang:1.21-alpine` rather than `FROM golang:1.21-alpine@sha256:...`),
the trivy scan runs against an image that may differ between CI runs due to
upstream rebuilds. The scan result is therefore not reproducible and the same
CVE may pass or fail on different days. Separately, the `docker` job (Stage 6)
uses `docker/build-push-action@v5` with `cache-to: type=gha` while the
`security` job re-runs `docker build` without the Buildx cache — wasting
compute and diverging from the cached image.

**Fix:** Reuse the image artifact from the `docker` job (export as a tarball
via `docker save`, upload as an artifact, download in `security`), or pin the
Dockerfile `FROM` digest and use Buildx in the security job too.

---

### [MAJOR] Go version inconsistency across jobs — `lint`/`build-go` use 1.21, `qemu-integration`/`riscv64` use 1.22

File: `ci/build-and-test.yml`  Lines: 29, 773, 820

**Issue:** The top-level env block sets `GO_VERSION: "1.21"`, which is used by
the `lint` and `build-go` jobs. However, `qemu-integration` (line 773) and
`riscv64-cross-compile` (line 820) hard-code `go-version: "1.22"` directly,
bypassing the env variable. A binary built with 1.22 may depend on
runtime behaviour or standard library APIs absent in 1.21, yet the artefact
used by the rest of the pipeline was compiled with 1.21 (in `build-go`). This
is a hidden semantic split.

**Fix:** Replace all hard-coded `go-version:` values with
`${{ env.GO_VERSION }}` and update the env block to a single authoritative
version.

---

### [MAJOR] `pip install` in CI has no version pins — tool versions are non-deterministic

File: `ci/build-and-test.yml`  Lines: 50, 146

**Issue:** The `lint` job installs Python tools with bare package names:

```yaml
run: pip install flake8 black isort
```

The `test-py` job installs:

```yaml
run: pip install pytest pytest-cov pytest-xdist
```

Without version pins, pip resolves to whatever is latest at job execution time.
A new release of `black` or `flake8` can change formatting rules or add new
lint checks overnight, causing flapping failures unrelated to code changes.

**Fix:** Pin every package to an exact version in a `requirements-dev.txt`
(or `constraints.txt`) and install with `pip install -r requirements-dev.txt`.
The `test-modules` job already references `tests/requirements.txt` — extend
that pattern here.

---

### [MAJOR] `shellcheck` installed from apt in CI — version is runner-OS-dependent, not pinned

File: `ci/build-and-test.yml`  Line: 61

**Issue:**

```yaml
run: sudo apt-get install -y shellcheck
```

`ubuntu-22.04` ships shellcheck 0.8.0; `ubuntu-24.04` ships 0.10.0. Error
codes and detection rules differ between versions. The pre-commit config
correctly pins `shellcheck-py` to `v0.10.0.1`, creating a divergence between
local pre-commit and CI.

**Fix:** Use the pre-commit shellcheck version in CI:
```yaml
- name: Install shellcheck
  run: |
    SHELLCHECK_VERSION="0.10.0"
    curl -fsSL "https://github.com/koalaman/shellcheck/releases/download/v${SHELLCHECK_VERSION}/shellcheck-v${SHELLCHECK_VERSION}.linux.x86_64.tar.xz" \
      | tar -xJ --strip-components=1 -C /usr/local/bin shellcheck-v${SHELLCHECK_VERSION}/shellcheck
```
Or install via the `shellcheck-py` wheel which pins the binary version.

---

### [MAJOR] `clang` in eBPF build stage is not version-pinned

File: `ci/build-and-test.yml`  Line: 856

**Issue:**

```yaml
run: |
  sudo apt-get install -y clang llvm libbpf-dev linux-headers-$(uname -r) \
    linux-tools-$(uname -r) linux-tools-generic bpftool || true
```

`clang` is installed from the distro package manager without a version
constraint. The `ebpf/Makefile` requires `clang >= 14` (per its own comment,
line 3). Ubuntu 22.04 ships clang-14; Ubuntu 24.04 ships clang-18. Without
pinning, a runner OS change silently changes the compiler. The `|| true` also
masks installation failures, meaning the step can appear to succeed with no
tools installed at all.

**Fix:** Pin the clang major version explicitly:
```yaml
sudo apt-get install -y clang-16 llvm-16 libbpf-dev ...
sudo update-alternatives --install /usr/bin/clang clang /usr/bin/clang-16 100
```
Remove the `|| true` so installation failures are surfaced.

---

### [MAJOR] BATS shell tests are silently swallowed in CI — non-zero exit suppressed

File: `ci/build-and-test.yml`  Lines: 168–169

**Issue:**

```bash
bash tests/bats/run_bats.sh --tap 2>&1 | tee bats-output.tap || true
echo "BATS tests complete (non-blocking in CI)"
```

The `|| true` combined with the pipe means a BATS test failure is silently
discarded. The `tee` exit status is lost in bash (PIPESTATUS is not checked),
and the `|| true` converts any failure to success. This makes BATS tests
completely toothless.

**Fix:** Either make the step blocking (remove `|| true`, add
`set -o pipefail`) or explicitly document it as advisory-only with
`continue-on-error: true` at the step level so the intent is clear and GitHub
records it as a warning rather than silently passing.

---

### [MINOR] `actions/setup-python` pinned to `@v5` major tag — not SHA-pinned

File: `ci/build-and-test.yml`  Lines: 45, 140, 207

**Issue:** Third-party Actions are referenced by mutable semver tags
(`@v4`, `@v5`). A tag can be moved by the action author to point to a
different commit. Security best practice (and GitHub's own hardening guide)
is to pin to a full commit SHA:

```yaml
uses: actions/setup-python@82c7e631bb3cdc910f68e0081d67478d79c6982d  # v5.5.0
```

This applies to all `uses:` lines in the workflow (checkout, setup-go,
upload-artifact, download-artifact, codecov, trivy, codeql, etc.).

**Fix:** Pin all Actions to full SHAs. Tools like `pin-github-actions` or
Dependabot's `update-scheduled-jobs` can automate this.

---

### [MINOR] `gitleaks-action@v2` uses a mutable major-version tag with no config-path guard

File: `ci/build-and-test.yml`  Line: 109–113

**Issue:** The gitleaks step references `.gitleaks.toml` via `config-path`:

```yaml
with:
  config-path: .gitleaks.toml
```

If `.gitleaks.toml` does not exist in the repository, gitleaks silently falls
back to default rules and the step still passes. There is no guard to verify
the config file is present before invoking the action. Additionally, `@v2` is
a mutable tag.

**Fix:** Add a pre-check:
```yaml
- name: Verify gitleaks config present
  run: test -f .gitleaks.toml || (echo ".gitleaks.toml missing" && exit 1)
```
Pin the action to a commit SHA.

---

### [MINOR] Kernel module build falls back to running-kernel headers, not the matrix kernel version

File: `ci/build-and-test.yml`  Lines: 373–386

**Issue:** The `build-mod` job is parameterised on `matrix.kernel_version`
(`6.1.0`, `6.6.0`, `6.12.0`) but the actual kernel headers path is resolved
as:

```bash
KERNEL_DIR="/usr/src/linux-headers-$(uname -r)"
```

`uname -r` returns the runner's kernel version (e.g. `5.15.0-1053-aws`), not
the matrix kernel version. The matrix version variable is checked but never
used for the actual header path. The job therefore builds against the same
headers in all three matrix cells, making the matrix meaningless.

The `kernel-matrix` job (Stage 8) correctly downloads and uses kernel sources
from kernel.org for each matrix entry, making it the correct implementation.
`build-mod` appears to be a redundant, broken predecessor.

**Fix:** Either delete `build-mod` and rely solely on `kernel-matrix`, or fix
the header-path logic to use the matrix version (as `kernel-matrix` does).

---

### [MINOR] `test-modules` uses `continue-on-error: true` on every module — failures are invisible

File: `ci/build-and-test.yml`  Lines: 219, 224, 229, 234, 239, 244

**Issue:** Every module test step carries `continue-on-error: true`. If the
module code is broken, CI will still report green. The `summary` job does not
include `test-modules` in its failure check (line 559), so the job result is
never evaluated.

**Fix:** Remove `continue-on-error: true` from steps where the tests are
meaningful. If a test genuinely requires hardware and will skip cleanly, let
pytest report the skips via `pytest.mark.skip` — do not suppress the exit
code. Add `test-modules` to the `summary` job's `needs` and failure check.

---

### [MINOR] `isort` installed in `lint` job but never invoked

File: `ci/build-and-test.yml`  Line: 50

**Issue:** `pip install flake8 black isort` installs isort, but there is no
step that runs `isort --check`. The pre-commit config does run isort (line 83
of `.pre-commit-config.yaml`), creating a gap where import ordering is only
enforced locally, not in CI.

**Fix:** Add an isort check step after the black check:
```yaml
- name: Check Python import order (isort --check)
  run: isort --check-only --profile=black --line-length=100 replay/ tests/
```

---

### [MINOR] `ebpf/Makefile` `vmlinux.h` target has no fallback for CI environments

File: `ebpf/Makefile`  Line: 24–27

**Issue:** The `vmlinux.h` recipe calls `bpftool btf dump` unconditionally:

```makefile
vmlinux.h:
    bpftool btf dump file /sys/kernel/btf/vmlinux format c > vmlinux.h
```

On a developer machine or CI runner where BTF is unavailable this hard-errors
and blocks `make all`. There is no guard, no conditional, and no stub
generation path.

**Fix:** Add a conditional:
```makefile
vmlinux.h:
    @if [ -f /sys/kernel/btf/vmlinux ]; then \
        bpftool btf dump file /sys/kernel/btf/vmlinux format c > vmlinux.h; \
        echo "vmlinux.h: generated ($$(wc -l < vmlinux.h) lines)"; \
    else \
        echo "BTF not available — cannot generate vmlinux.h; provide it manually"; \
        exit 1; \
    fi
```
Or vendor a pre-generated `vmlinux.h` and skip the recipe entirely.

---

### [MINOR] `pytest -n auto` in `test-py` with hardcoded `HOST_PORT=18080` in `qemu-integration` — port collision risk

File: `ci/build-and-test.yml`  Lines: 151, 788

**Issue:** `pytest -n auto` in `test-py` uses `pytest-xdist` to run tests in
parallel across worker processes. If any test in `tests/` starts a local HTTP
server (e.g. a mock Prometheus endpoint) on a fixed port, multiple workers
will collide on the same port and fail intermittently. The `qemu-integration`
job separately hard-codes `HOST_PORT=18080` and `HOST_PORT=18081`. These are
static port assignments — if two workflow runs execute concurrently on the same
self-hosted runner, both will try to bind the same port.

**Fix:** Let each pytest worker pick an ephemeral port with
`socket.bind(('', 0))` and pass the port via a fixture. For QEMU integration,
use `$((18080 + RANDOM % 100))` or a port-allocation helper.

---

### [MINOR] `go mod tidy` run in CI can silently alter `go.sum` without surfacing the diff

File: `ci/build-and-test.yml`  Line: 288–289

**Issue:**

```yaml
- name: Tidy Go modules
  run: cd exporter && go mod tidy
```

Running `go mod tidy` as a CI step mutates `go.mod`/`go.sum` if they are
out of date. This mutation exists only in the ephemeral workspace — the diff
is never reported. If the files diverge from the committed state the build
succeeds anyway, masking the staleness.

**Fix:** Check for staleness rather than silently fixing it:
```bash
cd exporter
go mod tidy
git diff --exit-code go.mod go.sum \
  || (echo "go.mod/go.sum out of date — run 'go mod tidy' and commit" && exit 1)
```

---

### [NIT] `Makefile` `lint-python` target calls `mypy` comment but does not run it

File: `Makefile`  Line: 160 (comment) vs. lines 162–167

**Issue:** The target comment says "Run black + flake8 + mypy on Python
sources" but the recipe only runs `black` and `flake8` — there is no `mypy`
invocation. This is inconsistent with the pre-commit config which does run
mypy, and with the comment.

**Fix:** Add `$(PYTHON) -m mypy --ignore-missing-imports replay/` to the
`lint-python` recipe, or correct the comment.

---

### [NIT] `sbom` job uses `anchore/sbom-action/download-syft@v0` — a wildcard major version

File: `ci/build-and-test.yml`  Line: 701

**Issue:** `@v0` is a mutable major-version tag that will automatically track
all `0.x.y` releases of syft, including potentially breaking changes to SBOM
output format or CLI flags.

**Fix:** Pin to a specific release tag or commit SHA, e.g.
`anchore/sbom-action/download-syft@v0.18.0`.

---

### [NIT] `grype --fail-on medium` failure is caught and suppressed with a warning echo

File: `ci/build-and-test.yml`  Lines: 721–728

**Issue:**

```bash
grype sbom:sbom-source.spdx.json \
  --fail-on medium \
  ... \
  2>&1 || {
  echo "WARNING: grype found vulnerabilities — check sbom-grype.sarif"
}
```

The `|| { ... }` block converts a non-zero grype exit (i.e. CVEs found at
medium or above) into a warning that produces exit code 0. Combined with
`continue-on-error: true` on the `sbom` job, medium-severity CVEs in the SBOM
never block the pipeline. The `--fail-on` flag is effectively a no-op.

**Fix:** Remove the `||` suppression and let grype's non-zero exit propagate;
rely on `continue-on-error: true` at the job level only if you genuinely want
it non-blocking, but remove the misleading `--fail-on medium` flag if so.

---

### [NIT] Pre-commit `markdownlint` uses `--fix` flag — auto-mutates files during commit

File: `.pre-commit-config.yaml`  Lines: 130–132

**Issue:**

```yaml
args: [--config=.markdownlint.yaml, --fix]
```

Running markdownlint with `--fix` during a pre-commit hook means the hook
auto-modifies staged files. The modified files are not automatically
re-staged, so the committed diff and the working-tree diff may diverge. This
can confuse reviewers and cause subsequent `git status` noise.

**Fix:** Remove `--fix` from the pre-commit hook args. Use `--fix` only in a
separate `make fmt` / `make lint-fix` target for developer convenience.

---

## Missing Stages Identified

1. **mypy in CI** — Python type checking runs in pre-commit (`mirrors-mypy@v1.10.0`)
   but has no equivalent CI job or step. A type error that pre-commit would
   catch locally will pass CI if the developer skips hooks.

2. **staticcheck / golangci-lint for Go** — `go vet` is present but catches
   only a narrow set of issues. `staticcheck` or `golangci-lint` would catch
   shadowed variables, ineffective assignments, deprecated API usage, and
   performance issues. Pre-commit does not run staticcheck either.

3. **yamllint for alert rules** — the `deploy/` directory likely contains
   Prometheus alerting rules in YAML. Pre-commit runs `yamllint` globally, but
   CI has no dedicated step to validate alert rule syntax with
   `promtool check rules`. Invalid alert expressions silently deploy.

4. **markdownlint in CI** — pre-commit enforces markdown style locally; CI has
   no equivalent step. Documentation linting regressions only surface for
   developers who have pre-commit installed.

5. **isort in CI** — import ordering is enforced locally by pre-commit's isort
   hook but there is no CI step (see finding above).

6. **ruff as a faster flake8 replacement** — not strictly a missing stage, but
   the project uses flake8 + isort + black individually. Ruff subsumes all
   three and is 10-100x faster; worth considering as a unified Python linter
   in both CI and pre-commit.

7. **Dockerfile lint (hadolint)** — the `exporter/Dockerfile` is built and
   scanned but never linted. `hadolint` would catch unpinned `FROM` digests,
   missing `--no-cache` on `apt-get`, `COPY` glob hazards, etc.

8. **Go module license compliance** — the SBOM is generated but there is no
   step to check that all Go dependency licenses are permissive (e.g. with
   `go-licenses` or `lichen`). This matters for an embedded/hardware product
   that may ship the binary.

---

## Metrics

- Files reviewed: 4
- Blockers: 2
- Majors: 6
- Minors: 7
- Nits: 4
