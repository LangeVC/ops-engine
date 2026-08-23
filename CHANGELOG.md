# Changelog

## 3.0.0

**Breaking.** Organisation keys in `config.yml` are now canonical Forgejo
`lower_name` values. A layover keyed `fusionAIze` no longer resolves; it must be
`fusionaize`, with the display spelling kept as an attribute. Run
`scripts/migrate-org-keys.py` before upgrading: it lowercases existing keys,
preserves the old spelling as `github.login`, and fails loudly on a collision
rather than letting one key silently win.

Why a major bump: the Python contract surface changed additively only, but
`config.yml` is the contract the five layovers actually consume, and they have
to act. A migration script is the admission that consumers must act, so the
version says so.

### Organisation identity is data, not string interpolation (LVC-229)

- The webhook ingress derives the canonical org key from `lower_name` and never
  from `full_name` or a display name. Previously a case mismatch made
  `get_repo_config` return nothing, so the `ReleaseHandler` never ran — measured
  on the faigrid v1.8.0 release, which produced no release object at all.
- `forgejo.display_name` and `github.login` are attributes under the canonical
  key, never lookup keys.
- Release names come from a configured `name_template`, not from
  `repo.split('/')[-1]`. The convention renders identically on Forgejo and
  GitHub. The generic template ships `orgs: {}` and carries no product name.
- Every repo reference resolves through the canonical key path; a configured
  target that does not resolve fails the config load rather than the dispatch.

### Configuration is typed at the boundary (LVC-224)

- `mirror` and `notifications` are returned as `MirrorConfig` and
  `NotificationConfig`, never as raw dicts. The previous behaviour raised
  `'dict' object has no attribute 'enabled'` on every push and sent the event to
  the dead-letter queue.
- A section that cannot be typed fails with a named `ConfigSectionError`
  identifying the section, instead of surfacing as an `AttributeError` later.

### Release reconciliation

- A tag that exists without a release is detectable and repairable without
  deleting or recreating the tag.
- Release and mirror state are queryable as records.

### Release gate

- `check-tag` strips the `v` prefix, as its docstring always claimed and its code
  did not: `check-tag v2.4.3` against source of truth `2.4.3` now passes.
- A prerelease tag can be gated when the caller allows it, and is refused with a
  named error when it does not — previously any `-rc.N` was rejected as "not
  semver" before the tag was ever compared.

### Public surface

- `CONTRACT.md` declares which exported names are contract and which are
  internal, and what a minor versus a major bump may change. A name absent from
  the declaration is not promised. `tests/test_public_surface.py` asserts the
  declaration and `__all__` agree at CI time.

### Template purity

- The release-name gate reads its expected pattern from configuration; the
  product name no longer sits hard-coded in the generic template.


## 2.2.0

### `version-sync.py` — declare where a repo keeps its version

Generic tooling, moved here from `langevc/lvc-ops` because it belongs to the
open base rather than behind one org's privacy boundary. It carries no
organisation, host or product name — 266 lines, zero references to any of
them — and anyone can use it unchanged.

A repository declares its version locations in `.version.yaml`:

```yaml
schema: 1
source_of_truth: pyproject.toml
locations:
  - path: pyproject.toml
    pattern: '^version\s*=\s*"(\d+\.\d+\.\d+)"'
    required: true
```

- `check` — every required location agrees with the source of truth
- `check-tag TAG` — the same, plus the tag equals the source of truth
- `bump VERSION` — write the new version to every declared location

The declaration stays in the consuming repository: the tool is generic, the
version locations are not. That split is the point.

### Why it moved

It lived in a private ops repository and was fetched at runtime by release
gates in other organisations. Those fetches got an HTML login page with HTTP
200 and reported success — a release gate then died on
`SyntaxError: <!DOCTYPE html>` three steps later. Here it is reachable from
the public mirror without a credential, which removes the cross-organisation
dependency instead of papering over it.


All notable changes to ops-engine are documented in this file.

## [2.1.2] — 2026-06-04

### Fixed

- **HealthMonitor**: actually declare `pyyaml>=6.0` as a runtime dependency.
  v2.1.1 claimed to fix this in the CHANGELOG but the corresponding edit to
  `pyproject.toml` never landed (silent Edit-call failure during release).
  This release is the real fix. Symptom on consumers without transitive
  pyyaml: `ModuleNotFoundError: No module named 'yaml'` at first module run.

## [2.1.1] — 2026-06-04

### Fixed

- **HealthMonitor**: declare `pyyaml>=6.0` as a runtime dependency. The
  module imports `yaml` to parse the consumer's `config.yml` but the
  package only listed `pydantic` + `httpx` in `dependencies`. Triggered
  `ModuleNotFoundError: No module named 'yaml'` on first run for any
  consumer that didn't already have pyyaml installed transitively.

  (Note: the CHANGELOG entry above was correct but the corresponding
  `pyproject.toml` edit did not land — see 2.1.2.)

## [2.1.0] — 2026-06-04

### Added

- **HealthMonitor** (CORE-006): Scheduled HTTP probes with pluggable sinks
  - Probe targets defined in consumer's `config.yml` (URL, expected status, optional JSON-field assertion)
  - Sinks: `stdout` (CI log), `file` (per-run log file), `webhook` (POST JSON), `github_issue` (label-targeted)
  - Replaces the anti-pattern of writing health-check log entries back to the source repo via auto-commit
  - Module entry point: `python -m ops_engine.modules.health_monitor --config <cfg> --org <name>`
  - Consumer: `Capacium/capacium-ops` (PR #4 — replaces inline shell + commit-back)
- **MigrationRunner** (CORE-007): Forward-only SQL migration runner
  - Discovers `*.sql` files in a directory, ordered lexicographically
  - Tracks applied migrations + checksums in `schema_migrations` table
  - Detects checksum drift (file edited after apply) → hard fail
  - Safely handles `CREATE INDEX CONCURRENTLY` (each statement outside the migration transaction)
  - Pluggable adapter (`[postgres]` extra ships psycopg2 fakeable adapter; SQLite trivially addable)
  - Test suite: apply-pending, checksum-mismatch, concurrent-index, idempotent, lock-timeout
  - Consumer: `Capacium/capacium-ops` PR #5 wires this for `capacium-exchange`
- **Module-side config loader** (`ops_engine.config_loader`): validates per-module section in consumer's `config.yml`

### Changed

- `README.md`: lists both new modules in the Modules table

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
