<!-- SPDX-License-Identifier: Apache-2.0 -->
<!--
  Fill out every section. Delete sections that genuinely don't apply
  (e.g. "Metrics Added" for a docs-only PR) rather than leaving them blank.
-->

## Summary

<!--
  One paragraph. What does this PR do and WHY is it needed?
  Link the task: "Implements task #N — <task subject>."
-->

Implements task #<!-- N --> — <!-- task subject -->

<!-- What problem does this solve? Why now? -->

---

## Type of change

<!-- Tick all that apply -->

- [ ] `feat` — new feature or collector
- [ ] `fix` — bug fix
- [ ] `test` — new or improved tests only
- [ ] `docs` — documentation only
- [ ] `refactor` — behaviour-preserving restructure
- [ ] `ci` — CI pipeline or tooling
- [ ] `chore` — dependency bump, formatting

---

## Metrics Added / Changed

<!--
  List every new Prometheus metric introduced or renamed.
  Format: `metric_name{label1, label2}` TYPE — description
  Leave blank for docs/ci/chore PRs.
-->

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| | | | |

---

## Tests Added

<!--
  List new test files and their class/test counts.
  State which hardware paths are covered by mocks vs. skipped with coverage:ignore.
-->

| File | Tests | Notes |
|------|-------|-------|
| | | |

---

## Coverage Delta

<!--
  Before/after numbers from `make coverage-go` or `make coverage-python`.
  If a package dropped, explain why it's still acceptable.
-->

| Package | Before | After | Threshold |
|---------|--------|-------|-----------|
| | | | |

---

## Breaking Changes

<!--
  Any metric renames, removed labels, changed scrape paths, or
  alert rule name changes that require operator action on upgrade.
  Write "None" if there are none — don't leave blank.
-->

None

---

## Deployment Notes

<!--
  Anything an operator needs to do: new env vars, new Secrets,
  kernel version requirements, new Kubernetes RBAC, new systemd units.
  Write "None" if there are none.
-->

None

---

## Reviewer Checklist

<!-- To be completed by the reviewer, not the author -->

- [ ] Commit messages follow `type(scope): subject` with `Refs: #N` footer
- [ ] Each commit is atomic — tests pass at every commit, no "fix tests" commits
- [ ] New metrics have descriptive HELP strings visible in the Grafana metric browser
- [ ] Hardware-absent code paths degrade gracefully (`collector_up=0`, no panic)
- [ ] No secrets, hostnames, or proprietary content
- [ ] Coverage thresholds met per `CONTRIBUTING.md` table
- [ ] Alert rules tested: correct label sets match Alertmanager routes
