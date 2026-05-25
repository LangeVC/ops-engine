# Changelog

All notable changes to ops-engine are documented in this file.

## [2.0.0] — 2026-05-25

### Added

- **ReleaseHandler** (CORE-002): Automated release creation on tag push with CHANGELOG.md parsing, tag pattern matching (`fnmatch`), and idempotency via `release_exists()` check
- **MergeHandler** (CORE-003): Auto-merge PRs when CI passes and trigger label is present, supports `check_suite`, `check_run`, `status`, and `pull_request` labeled events
- **MirrorHandler** (CORE-004): Cross-forge mirror drift verification comparing HEAD SHA between primary and mirror forges with configurable drift timeout
- **NotificationHandler** (CORE-005): Multi-channel notification dispatch (webhook, Slack, Discord) with event filtering, built-in templates, and delivery deduplication
- **ChangelogParser** (CORE-006): Extracts version-specific release notes from CHANGELOG.md files, supports multiple header formats
- **QueueManager v2** (CORE-007): Bounded async queue with backpressure (`max_queue_size`), retry with configurable `max_retries`, dead letter queue, `QueueMetrics` tracking, and graceful shutdown with 30s drain timeout
- **EventDeduplicator** (CORE-008): In-memory webhook dedup cache supporting GitHub (`x-github-delivery`), Forgejo (`x-forgejo-delivery`), and Gitea (`x-gitea-delivery`) delivery headers
- **Config models**: `ReleaseConfig`, `MergeConfig`, `MirrorConfig`, `NotificationConfig`, `NotificationChannel` — all Pydantic v2 with org-level inheritance (mirror is repo-specific only)
- **ForgeAdapter v2 methods**: `create_release()`, `create_tag()`, `merge_pull_request()`, `get_pull_request()`, `get_ci_status()`, `get_file_content()`, `get_latest_commit_sha()`, `release_exists()`

### Changed

- **GithubAdapter**: Rewritten with real `httpx` async HTTP, lazy client init, exponential backoff retry (3 attempts on 429/502/503), HMAC-SHA256 webhook verification
- **ForgejoAdapter**: Rewritten with real `httpx` async HTTP for Gitea-compatible API, label-name-to-ID resolution, configurable base URL for self-hosted instances
- **QueueManager**: Complete rewrite from simple queue to production-grade bounded queue with metrics, retries, and dead letter handling

### Layover Configs

- `elementeer-ops/config.yml` — 6 repos, Forgejo primary
- `capacium-ops/config.yml` — 18 repos, GitHub primary
- `fusionaize-ops/config.yml` — 16 repos, updated from v1 with v2 fields
- `skillweave-ops/config.yml` — 1 repo, Forgejo primary with GitHub mirror

## [0.1.0] — 2025-05-18

### Added

- Initial release: QueueManager, ForgeAdapter (GitHub + Forgejo), TriageHandler, StaleHandler, DispatchHandler
- 3-Layer architecture (OSS Core, Org Layover, Repo Override)
- Pydantic config loader with YAML support
