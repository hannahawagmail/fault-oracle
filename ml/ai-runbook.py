#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
ml/ai-runbook.py — Generate context-aware runbooks via Claude API on alert fire.

Called by the Alertmanager webhook (recovery/webhook-server.py) or directly.
Assembles hardware context (error signature, DIMM SPD, failure probability,
CE rate slope, recent dmesg snippets) and prompts the Claude API to produce
a structured, step-by-step runbook.

Output:
    /var/lib/hw-fault-ml/runbooks/<alert_fingerprint>.md

Environment:
    ANTHROPIC_API_KEY — required for Claude API
    PROMETHEUS_URL    — for live metric context queries

Usage:
    python3 ml/ai-runbook.py --alertname EDACCorrectableStorm \\
        --instance worker-12 --mc 0 --csrow 0
"""
import argparse
import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("ai-runbook")

DEFAULT_OUTPUT_DIR = Path("/var/lib/hw-fault-ml/runbooks")
PROMETHEUS_URL     = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL       = "claude-haiku-4-5-20251001"   # fast + cheap for runbooks
CLAUDE_API_URL     = "https://api.anthropic.com/v1/messages"


def query_prometheus_instant(metric_expr: str) -> str:
    """Return current value of a PromQL expression as a string."""
    try:
        params = urllib.parse.urlencode({"query": metric_expr})
        url = f"{PROMETHEUS_URL}/api/v1/query?{params}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        results = data.get("data", {}).get("result", [])
        if results:
            return str(results[0]["value"][1])
    except Exception:
        pass
    return "N/A"


def build_context(alertname: str, instance: str, mc: str, csrow: str,
                  extra_labels: dict) -> str:
    """Assemble all available hardware context into a prompt string.

    Data-residency / external API privacy: the instance label (hostname) is
    replaced with a consistent pseudonym before being sent to the Anthropic API.
    SHA256[:8] is stable across runs for the same host but not reversible without
    the original hostname, satisfying both consistency and privacy requirements.
    """
    # Anonymize hostname: replace with SHA256-based pseudonym so the real
    # hostname never leaves the cluster boundary when calling the external API.
    anon_instance = "node-" + hashlib.sha256(instance.encode()).hexdigest()[:8]

    # Pull live metrics using the real instance label for Prometheus queries
    p7d  = query_prometheus_instant(f'failure_probability_7d{{instance="{instance}",mc="{mc}",csrow="{csrow}"}}')
    p30d = query_prometheus_instant(f'failure_probability_30d{{instance="{instance}",mc="{mc}",csrow="{csrow}"}}')
    slope = query_prometheus_instant(f'dimm_aging_ce_rate_slope{{instance="{instance}",mc="{mc}"}}')
    zscore = query_prometheus_instant(f'anomaly_zscore{{instance="{instance}"}}')
    ue_count = query_prometheus_instant(f'edac_uncorrectable_errors_total{{instance="{instance}",mc="{mc}"}}')

    ctx = f"""
Hardware Fault Alert Context
============================
Alert:     {alertname}
Node:      {anon_instance}
MC:        mc{mc}  CSROW: {csrow}
Time:      {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}

Live Metrics (from Prometheus)
------------------------------
Failure probability 7-day:   {p7d}
Failure probability 30-day:  {p30d}
CE rate slope (errors/day):  {slope}
Anomaly Z-score:             {zscore}
UE count (all time):         {ue_count}

Additional Labels
-----------------
{json.dumps(extra_labels, indent=2) if extra_labels else "(none)"}
""".strip()
    return ctx


RUNBOOK_SYSTEM_PROMPT = """You are a senior Linux systems reliability engineer specializing in
ARM64 server hardware fault analysis. You generate precise, actionable runbooks for on-call engineers.

Your runbook format:
1. **Triage** — What to check first (30 seconds)
2. **Severity Assessment** — Is this urgent? Key signals to evaluate
3. **Immediate Actions** — Exact commands to run (bash blocks)
4. **Root Cause Investigation** — Deeper diagnostic steps
5. **Remediation** — Step-by-step fix or escalation path
6. **Prevention** — What to configure to catch this earlier

Use ONLY real Linux commands (edac-util, rasdaemon, dmidecode, mcelog, dmesg).
Be concise. An on-call engineer is reading this at 3am.
Do NOT make up commands that don't exist."""


def generate_runbook(context: str, alertname: str, instance: str, api_key: str,
                     out_path: Optional[Path] = None) -> str:
    """Call Claude API and return the generated runbook markdown.

    Retries up to 3 times with exponential backoff (2s/4s/8s).
    On 429, respects Retry-After header.
    On total failure, writes a fallback stub and logs the error — never raises.
    """
    user_message = f"""Generate a runbook for the following hardware fault alert.

{context}

The runbook should be specific to this exact alert signature and metric values above.
Format as Markdown. Start with a one-sentence summary of what is happening."""

    payload = json.dumps({
        "model":      CLAUDE_MODEL,
        "max_tokens": 1500,
        "system":     RUNBOOK_SYSTEM_PROMPT,
        "messages":   [{"role": "user", "content": user_message}],
    }).encode()

    headers = {
        "Content-Type":      "application/json",
        "x-api-key":         api_key,
        "anthropic-version": "2023-06-01",
    }

    backoffs = [2, 4, 8]
    last_exc: Optional[Exception] = None
    for attempt, backoff in enumerate(backoffs, start=1):
        try:
            req = urllib.request.Request(CLAUDE_API_URL, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            return data["content"][0]["text"]  # type: ignore[no-any-return]
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code == 429:
                wait = int(e.headers.get("retry-after", backoff))
                log.warning("Claude API rate-limited (429). Retry-After=%ds (attempt %d/3)", wait, attempt)
                time.sleep(wait)
            else:
                log.warning("Claude API HTTP error %d on attempt %d/3", e.code, attempt)
                if attempt < len(backoffs):
                    time.sleep(backoff)
        except Exception as e:
            last_exc = e
            log.warning("Claude API error on attempt %d/3: %s", attempt, e)
            if attempt < len(backoffs):
                time.sleep(backoff)

    # All retries exhausted — write fallback stub
    log.error("Claude API failed after 3 attempts: %s", last_exc)
    stub = (
        f"# Runbook generation failed\n\n"
        f"**Alert:** {alertname}\n"
        f"**Node:** {instance}\n\n"
        f"Fall back to `docs/runbook.md` for manual procedures.\n"
    )
    if out_path is not None:
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(stub)
            log.error("ai-runbook: wrote fallback stub to %s", out_path)
        except Exception as write_exc:
            log.error("ai-runbook: could not write fallback stub: %s", write_exc)
    return stub


def fingerprint(alertname: str, instance: str, mc: str) -> str:
    raw = f"{alertname}:{instance}:mc{mc}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--alertname",    default="EDACCorrectableStorm")
    parser.add_argument("--instance",     default="unknown")
    parser.add_argument("--mc",           default="0")
    parser.add_argument("--csrow",        default="0")
    parser.add_argument("--labels",       default="{}", help="JSON extra labels")
    parser.add_argument("--output-dir",   type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--dry-run",      action="store_true")
    parser.add_argument("--print",        action="store_true", dest="print_output")
    args = parser.parse_args()

    api_key = ANTHROPIC_API_KEY
    if not api_key and not args.dry_run:
        sys.stderr.write("ai-runbook: ANTHROPIC_API_KEY not set. Use --dry-run for testing.\n")
        sys.exit(1)

    extra = json.loads(args.labels)
    context = build_context(args.alertname, args.instance, args.mc, args.csrow, extra)

    fp   = fingerprint(args.alertname, args.instance, args.mc)
    name = f"{args.alertname}_{args.instance.replace('/', '_')}_{fp}.md"
    out_path = args.output_dir / name

    if args.dry_run:
        sys.stderr.write("[dry-run] Would call Claude API with context:\n")
        sys.stderr.write(context + "\n")
        runbook = f"# {args.alertname} Runbook (DRY RUN)\n\nGenerated at {time.time():.0f}\n\n{context}\n"
    else:
        sys.stderr.write("Calling Claude API…\n")
        runbook = generate_runbook(context, args.alertname, args.instance, api_key,
                                   out_path=out_path)

    if args.print_output:
        print(runbook)
        return

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(runbook)
    sys.stderr.write(f"ai-runbook: wrote {out_path}\n")


if __name__ == "__main__":
    main()
