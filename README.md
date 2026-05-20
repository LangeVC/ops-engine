<h1 align="center">LangeVC Ops Engine</h1>

<p align="center">
  <em>A generic, rate-limited async webhook queue and orchestration engine for GitHub and Forgejo.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/License-Apache%202.0-green.svg" alt="License: Apache 2.0">
  <img src="https://img.shields.io/badge/architecture-config--driven-orange.svg" alt="Architecture: Config-Driven">
</p>

---

## ⚡ The Problem: The Thundering Herd
When managing multiple organizations and repositories, relying purely on GitHub Actions for simple organizational hygiene (like labeling PRs, marking stale issues, or triggering dependent tests) quickly drains runner capacity and exhausts API rate limits. Simultaneous cron jobs (e.g., CodeQL scans) running across 20+ repos can easily cause **Runner Starvation** and block your actual developers.

## 🚀 The Solution: Ops Engine
`ops-engine` is a pure-infrastructure Python framework that abstracts away this complexity. It acts as a central brain, capturing incoming webhooks from your git forges, placing them in an asynchronous queue, and processing them strictly sequentially.

### Core Features
| Feature | Description | Trigger |
|---------|-------------|---------|
| 🚦 **Rate Limit Queue** | Processes incoming API tasks sequentially (e.g. 1 API call/sec) to guarantee you never hit HTTP 403 / 429 limits. | Always |
| 🏷️ **Triage & Auto-Labeling** | Automatically labels PRs and Issues based on title keywords, entirely without wasting GitHub Action runner minutes. | Webhook |
| 🧹 **Stale Cleanup** | Centralized, cron-based scanning to mark and close old issues across all organizations. | Cron |
| 🔄 **Dependency Triggers** | Automatically fires `repository_dispatch` events in downstream repos when upstream repos cut a release. | Webhook |
| ⏱️ **Cron Dispatcher** | Replaces decentral `.github/workflows/` cron jobs. Triggers heavy jobs like CodeQL centrally and sequentially. | Cron |

---

## 🏗️ Architecture Philosophy: The "Layover" Model
`ops-engine` contains **zero** business logic. It provides the generic queue, the API adapters, and the abstract handlers.

You consume it by building a thin **"Layover"** application for your organization (e.g., `fusionaize-ops`), which imports this engine and defines rules via a Pydantic/YAML Configuration.

---

## 📦 Quickstart

Install the engine via pip (requires Python 3.10+):

```bash
pip install git+https://github.com/LangeVC/ops-engine.git
```

### Usage Example (in your Layover)

```python
import asyncio
from fastapi import FastAPI, Request
from ops_engine import QueueManager, TriageHandler, OpsEngineConfig
from ops_engine.adapters.github_adapter import GithubAdapter

app = FastAPI()
queue = QueueManager(rate_limit_delay_seconds=1.0)
adapter = GithubAdapter(token="...", webhook_secret="...")

# Your specific rules loaded from YAML
config = OpsEngineConfig(...) 

@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.body()
    event = await adapter.parse_webhook(dict(request.headers), payload)
    
    # Enqueue the generic TriageHandler with your specific config
    await queue.enqueue(adapter, event, lambda a, e: TriageHandler.process_event(a, e, config.auto_triage))
    
    return {"status": "queued"}
```

---

## 🤝 Community & Support
- **Contributing:** See [CONTRIBUTING.md](CONTRIBUTING.md) for local setup and PR guidelines.
- **Security:** Vulnerabilities should be reported confidentially. See [SECURITY.md](SECURITY.md).
- **Code of Conduct:** Please adhere to our [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## 📄 License
Licensed under the Apache License 2.0. See [LICENSE](LICENSE) for details.
