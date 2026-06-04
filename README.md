<h1 align="center">Ops Engine</h1>

<p align="center">
  <em>Event-driven multi-org release orchestration for GitHub and Forgejo.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-2.0.0-blue.svg" alt="Version 2.0.0">
  <img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/License-Apache%202.0-green.svg" alt="License: Apache 2.0">
  <img src="https://img.shields.io/badge/architecture-config--driven-orange.svg" alt="Architecture: Config-Driven">
</p>

---

## The Problem

When managing multiple organizations and repositories, relying purely on GitHub Actions for organizational hygiene quickly drains runner capacity and exhausts API rate limits. Beyond that, release automation, auto-merge, and cross-forge mirror verification require custom glue code scattered across repos.

## The Solution

`ops-engine` is a pure-infrastructure Python framework that captures incoming webhooks from git forges, places them in a bounded async queue, and processes them through configurable handler modules. Zero business logic — all behavior is driven by YAML config.

### Modules

| Module | Description | Trigger |
|--------|-------------|---------|
| **Rate Limit Queue** | Bounded async queue with backpressure, retry, dead letter, and metrics | Always |
| **Triage & Auto-Labeling** | Labels PRs and issues based on title keywords | Webhook |
| **Stale Cleanup** | Marks and closes old issues across all orgs | Cron |
| **Dependency Triggers** | Fires `repository_dispatch` on downstream repos when upstream releases | Webhook |
| **Cron Dispatcher** | Centralized sequential cron (CodeQL, etc.) | Cron |
| **Release Automation** | Creates GitHub/Forgejo releases on tag push with CHANGELOG parsing | Webhook |
| **Auto-Merge** | Merges PRs when CI passes and trigger label is present | Webhook |
| **Mirror Verification** | Verifies cross-forge mirror sync (Forgejo ↔ GitHub) | Webhook |
| **Notifications** | Multi-channel alerts (webhook, Slack, Discord) with event filtering | Event |
| **Event Deduplication** | In-memory webhook dedup for GitHub, Forgejo, and Gitea delivery IDs | Always |
| **Health Monitor** | Scheduled HTTP probes with pluggable sinks (stdout / file / webhook / GitHub Issue). Replaces the anti-pattern of committing health logs back to the source repo. | Cron |

---

## Architecture: The 3-Layer Model

```
Layer 1: ops-engine (this package — pip install)
  QueueManager, ForgeAdapter, Handler Modules, Config Models

Layer 2: xyz-ops (your org layover — private repo per org)
  FastAPI app, config.yml, webhook endpoints, cron loop

Layer 3: .ops.yaml (per-repo overrides — optional)
  Repo-specific config that overrides org defaults
```

`ops-engine` has **no dependency** on any specific CI system, AI tool, or orchestration framework. The interface is the webhook. A `git push --tags` produces the same result regardless of what triggered it.

---

## Quickstart

```bash
pip install git+https://github.com/LangeVC/ops-engine.git
```

### Layover Example

```python
import asyncio
from fastapi import FastAPI, Request
from ops_engine import (
    QueueManager, QueueMetrics, EventDeduplicator,
    TriageHandler, ReleaseHandler, MergeHandler,
    OpsEngineConfig,
)
from ops_engine.adapters.github_adapter import GithubAdapter

app = FastAPI()
queue = QueueManager(rate_limit_delay_seconds=1.0, max_queue_size=1000)
dedup = EventDeduplicator()
adapter = GithubAdapter(token="...", webhook_secret="...")

config = OpsEngineConfig(...)  # loaded from config.yml

@app.post("/webhooks/github")
async def github_webhook(request: Request):
    headers = dict(request.headers)
    payload = await request.body()

    # Dedup
    delivery_id = dedup.extract_delivery_id(headers)
    if dedup.is_duplicate(delivery_id):
        return {"status": "duplicate"}

    # Verify signature
    if not adapter.verify_signature(payload, headers):
        return {"status": "invalid"}, 401

    event = await adapter.parse_webhook(headers, payload)
    repo_config = config.get_repo_config(org, repo)

    # Enqueue handlers
    if repo_config.release and repo_config.release.enabled:
        await queue.enqueue(adapter, event,
            lambda a, e: ReleaseHandler.process_event(a, e, repo_config.release))

    if repo_config.auto_merge and repo_config.auto_merge.enabled:
        await queue.enqueue(adapter, event,
            lambda a, e: MergeHandler.process_event(a, e, repo_config.auto_merge))

    return {"status": "queued"}
```

---

## Config Reference

All modules are configured via YAML in your org layover's `config.yml`:

### Release

```yaml
release:
  enabled: true
  trigger: "tag_push"         # tag_push | merge_label | both
  tag_pattern: "v*"           # fnmatch pattern
  changelog_path: "CHANGELOG.md"
  draft: false
```

### Auto-Merge

```yaml
auto_merge:
  enabled: true
  trigger_label: "auto-merge"
  merge_method: "squash"      # squash | merge | rebase
  required_checks: ["test", "lint"]
  delete_branch: true
```

### Mirror Verification

```yaml
mirror:
  enabled: true
  primary_forge: "forgejo"    # forgejo | github
  mirror_url: "github.com/org/repo"
  verify_on_push: true
  max_drift_seconds: 300
```

### Notifications

```yaml
notifications:
  enabled: true
  channels:
    - type: "slack"           # webhook | slack | discord
      url: "${SLACK_WEBHOOK_URL}"
      events: ["release", "mirror_drift"]
```

### Config Inheritance

Org-level defaults are inherited by all repos. Repo-level config overrides org defaults. Mirror config is repo-specific only (no org inheritance).

```yaml
MyOrg:
  # Org defaults — inherited by all repos
  auto_triage:
    add_needs_triage_label: true
  stale_management:
    days_until_stale: 60

  repositories:
    my-repo:
      release:
        enabled: true
        trigger: "tag_push"
      # Inherits org-level auto_triage and stale_management
```

---

## Development

```bash
git clone https://github.com/LangeVC/ops-engine.git
cd ops-engine
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -v
```

---

## Community & Support

- **Contributing:** See [CONTRIBUTING.md](CONTRIBUTING.md)
- **Security:** See [SECURITY.md](SECURITY.md)
- **Code of Conduct:** See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE) for details.
