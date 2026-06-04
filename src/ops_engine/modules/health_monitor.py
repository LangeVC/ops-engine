"""CORE-006: HealthMonitor — scheduled HTTP probes with pluggable sinks.

Replaces the anti-pattern of committing health-check logs back into the source
repo. Configured via YAML in the org layover; sinks decide where results land
(stdout / file / webhook / github_issue). No business logic in this module.

CLI usage (from a GitHub Action workflow or cron):

    python -m ops_engine.modules.health_monitor --config config.yml [--org NAME]

Exit code 1 if any probe fails AND ``fail_run_on_error`` is true (default).

YAML example (org layover ``config.yml``):

    Capacium:
      health_monitor:
        enabled: true
        checks:
          - name: exchange-api
            url: https://api.capacium.xyz/v2/stats
            expect_status: 200
            expect_json_field: crawler_health
            expect_json_value: healthy
          - name: dashboard
            url: https://dash.capacium.xyz
        sinks:
          - type: stdout
          - type: github_issue
            issue_label: health-alert
            only_on_failure: true
          # - type: webhook
          #   url: https://example.com/health
          # - type: file
          #   path: /tmp/health-${DATE}.jsonl   # ephemeral CI path, NOT in the repo
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ops_engine.config_loader import (
    HealthCheck,
    HealthMonitorConfig,
    HealthSink,
)

logger = logging.getLogger(__name__)


# ── Probe ────────────────────────────────────────────────────────────────────

_DEFAULT_UA = "ops-engine-health-monitor/1.0 (+https://github.com/Capacium/ops-engine)"


def _probe(check: HealthCheck) -> dict[str, Any]:
    """Run one probe and return a normalized result dict."""
    started = datetime.now(timezone.utc)
    # Sensible default UA so CF/WAF doesn't 403 python-urllib's default;
    # per-check headers override.
    headers = {"User-Agent": _DEFAULT_UA, **check.headers}
    req = urllib.request.Request(check.url, method=check.method.upper(), headers=headers)
    result: dict[str, Any] = {
        "name": check.name,
        "url": check.url,
        "timestamp": started.isoformat(),
        "ok": False,
        "status": None,
        "error": None,
        "duration_ms": None,
    }
    try:
        with urllib.request.urlopen(req, timeout=check.timeout_seconds) as r:
            body = r.read()
            result["status"] = r.status
            result["duration_ms"] = int(
                (datetime.now(timezone.utc) - started).total_seconds() * 1000
            )
            if r.status != check.expect_status:
                result["error"] = f"status {r.status} != expected {check.expect_status}"
                return result
            if check.expect_json_field is not None:
                try:
                    j = json.loads(body)
                except Exception as e:  # noqa: BLE001 (broad — log + record)
                    result["error"] = f"json decode failed: {e}"
                    return result
                got = j.get(check.expect_json_field)
                if got != check.expect_json_value:
                    result["error"] = (
                        f"json[{check.expect_json_field}]={got!r} "
                        f"!= expected {check.expect_json_value!r}"
                    )
                    return result
            result["ok"] = True
    except urllib.error.HTTPError as e:
        result["status"] = e.code
        result["error"] = f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
    result["duration_ms"] = int(
        (datetime.now(timezone.utc) - started).total_seconds() * 1000
    )
    return result


# ── Sinks ────────────────────────────────────────────────────────────────────

def _emit_stdout(results: list[dict[str, Any]]) -> None:
    failures = [r for r in results if not r["ok"]]
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "ok": sum(1 for r in results if r["ok"]),
        "failed": len(failures),
        "checks": results,
    }
    print(json.dumps(summary, separators=(",", ":")))


def _emit_file(results: list[dict[str, Any]], path_template: str) -> None:
    # Expand a single placeholder ${DATE} (UTC YYYYMMDD) for partitioned logs.
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = Path(path_template.replace("${DATE}", date))
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps({"ts": ts, **r}, separators=(",", ":")) + "\n")


def _emit_webhook(results: list[dict[str, Any]], sink: HealthSink) -> None:
    body = json.dumps(
        {"timestamp": datetime.now(timezone.utc).isoformat(), "checks": results}
    ).encode("utf-8")
    headers = {"Content-Type": "application/json", **sink.headers}
    req = urllib.request.Request(sink.url, data=body, method="POST", headers=headers)
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # noqa: BLE001
        logger.warning("webhook sink %s failed: %s", sink.url, e)


def _emit_github_issue(
    results: list[dict[str, Any]],
    sink: HealthSink,
    repo: str | None,
    token: str | None,
) -> None:
    """Create or update a single labeled issue on failure (avoids spam)."""
    failures = [r for r in results if not r["ok"]]
    if sink.only_on_failure and not failures:
        return
    if not repo or not token:
        logger.warning(
            "github_issue sink: missing GITHUB_REPOSITORY or GITHUB_TOKEN; skipping"
        )
        return
    api = "https://api.github.com"
    hdr = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ops-engine/health-monitor",
    }
    failure_lines = "\n".join(
        f"- **{r['name']}** ({r['url']}) → {r.get('error') or r.get('status')}"
        for r in failures
    )
    body = (
        f"## Health Check Failed\n\n"
        f"**Timestamp:** {datetime.now(timezone.utc).isoformat()}\n\n"
        f"### Failures\n{failure_lines}\n"
    )
    # find existing open issue with the label
    url = f"{api}/repos/{repo}/issues?state=open&labels={sink.issue_label}"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=hdr), timeout=10
        ) as r:
            issues = json.load(r)
    except Exception as e:  # noqa: BLE001
        logger.warning("github_issue sink: list failed: %s", e)
        return
    existing = next((i for i in issues if i.get("title") == sink.issue_title), None)
    try:
        if existing:
            url = f"{api}/repos/{repo}/issues/{existing['number']}/comments"
            urllib.request.urlopen(
                urllib.request.Request(
                    url,
                    data=json.dumps({"body": body}).encode("utf-8"),
                    method="POST",
                    headers={**hdr, "Content-Type": "application/json"},
                ),
                timeout=10,
            ).read()
        else:
            url = f"{api}/repos/{repo}/issues"
            urllib.request.urlopen(
                urllib.request.Request(
                    url,
                    data=json.dumps(
                        {
                            "title": sink.issue_title,
                            "body": body,
                            "labels": [sink.issue_label],
                        }
                    ).encode("utf-8"),
                    method="POST",
                    headers={**hdr, "Content-Type": "application/json"},
                ),
                timeout=10,
            ).read()
    except Exception as e:  # noqa: BLE001
        logger.warning("github_issue sink: write failed: %s", e)


# ── HealthMonitor ────────────────────────────────────────────────────────────

class HealthMonitor:
    """Pure-config health monitor — no business logic, just probes + sinks."""

    @staticmethod
    def run(config: HealthMonitorConfig) -> int:
        """Run all probes, emit to all sinks. Return non-zero on failure."""
        if not config.enabled or not config.checks:
            logger.info("HealthMonitor: disabled or no checks; skipping.")
            return 0
        results = [_probe(c) for c in config.checks]
        repo = os.environ.get("GITHUB_REPOSITORY")
        token = os.environ.get("GITHUB_TOKEN")
        for sink in config.sinks:
            try:
                if sink.type == "stdout":
                    _emit_stdout(results)
                elif sink.type == "file":
                    if not sink.path:
                        logger.warning("file sink: missing path")
                    else:
                        _emit_file(results, sink.path)
                elif sink.type == "webhook":
                    if not sink.url:
                        logger.warning("webhook sink: missing url")
                    else:
                        _emit_webhook(results, sink)
                elif sink.type == "github_issue":
                    _emit_github_issue(results, sink, repo, token)
                else:
                    logger.warning("unknown sink type %r — skipping", sink.type)
            except Exception as e:  # noqa: BLE001
                logger.warning("sink %s failed: %s", sink.type, e)
        any_fail = any(not r["ok"] for r in results)
        return 1 if (any_fail and config.fail_run_on_error) else 0


# ── CLI ──────────────────────────────────────────────────────────────────────

def _load_config(path: str, org: str | None) -> HealthMonitorConfig:
    raw = yaml.safe_load(open(path, encoding="utf-8"))
    # Org-layover layout: { OrgName: { health_monitor: {...} } }
    org_name = org or next(iter(raw or {}), None)
    if not org_name or org_name not in raw:
        raise SystemExit(f"no org section found in {path} (orgs: {list(raw or {})!r})")
    org_block = raw[org_name] or {}
    hm = org_block.get("health_monitor")
    if not hm:
        raise SystemExit(
            f"org {org_name!r} has no 'health_monitor' section in {path}"
        )
    return HealthMonitorConfig(**hm)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ops-engine.health_monitor")
    p.add_argument("--config", required=True, help="path to org-layover config.yml")
    p.add_argument(
        "--org",
        default=None,
        help="org section name (default: first top-level key in the YAML)",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    cfg = _load_config(args.config, args.org)
    return HealthMonitor.run(cfg)


if __name__ == "__main__":
    sys.exit(main())
