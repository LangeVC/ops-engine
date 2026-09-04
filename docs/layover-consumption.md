# Layover Consumption Declaration

Each org layover consumes `ops-engine`. A version pin (`@v3.0.0`) is not
checkable on its own: it does not say *what* the layover uses. This document
declares, per layover, the **contract names** it consumes — resolution of the
names listed in `CONTRACT.md` — placed next to the pin that the layover holds
in its own `pyproject.toml`.

A layover consumes a name when its source imports or refers to that name.
Names imported from a submodule path (`ops_engine.adapters.*`,
`ops_engine.core.*`, `ops_engine.modules.*`, `ops_engine.utils.*`) are
internal and are **not** consumed contract names; only the bare contract
names from `ops_engine.__all__` are declared here.

## Machine-readable declaration

The single source of truth for this consumption is the JSON block below. Each
layover entry carries its own `pin` (the version it resolves in
`pyproject.toml`) and the `consumes` list of contract names it relies on.

```json
{
  "schema": 1,
  "package": "ops_engine",
  "layovers": [
    {
      "name": "lvc-ops",
      "pin": "3.0.0",
      "extra": null,
      "consumes": [
        "OpsEngineConfig",
        "QueueManager",
        "EventDeduplicator",
        "StaleManager",
        "CronDispatcher",
        "TriageHandler",
        "DependencyTriggerHandler",
        "ReleaseHandler",
        "MergeHandler",
        "MirrorHandler",
        "NotificationHandler"
      ]
    },
    {
      "name": "capacium-ops",
      "pin": "3.0.0",
      "extra": "postgres",
      "consumes": [
        "OpsEngineConfig",
        "QueueManager",
        "EventDeduplicator",
        "StaleManager",
        "CronDispatcher",
        "TriageHandler",
        "DependencyTriggerHandler",
        "ReleaseHandler",
        "MergeHandler",
        "NotificationHandler",
        "MigrationRunner",
        "MigrationTargetConfig",
        "ApplyResult",
        "runner_from_config"
      ]
    },
    {
      "name": "elementeer-ops",
      "pin": "3.0.0",
      "extra": null,
      "consumes": [
        "OpsEngineConfig",
        "QueueManager",
        "EventDeduplicator",
        "StaleManager",
        "CronDispatcher",
        "TriageHandler",
        "DependencyTriggerHandler",
        "ReleaseHandler",
        "MergeHandler",
        "MirrorHandler",
        "NotificationHandler"
      ]
    },
    {
      "name": "fusionaize-ops",
      "pin": "3.0.0",
      "extra": null,
      "consumes": [
        "OpsEngineConfig",
        "QueueManager",
        "EventDeduplicator",
        "StaleManager",
        "CronDispatcher",
        "TriageHandler",
        "DependencyTriggerHandler",
        "ReleaseHandler",
        "MergeHandler",
        "MirrorHandler",
        "NotificationHandler"
      ]
    },
    {
      "name": "skillweave-ops",
      "pin": "3.0.0",
      "extra": null,
      "consumes": [
        "OpsEngineConfig",
        "QueueManager",
        "EventDeduplicator",
        "StaleManager",
        "CronDispatcher",
        "TriageHandler",
        "DependencyTriggerHandler",
        "ReleaseHandler",
        "MergeHandler",
        "MirrorHandler",
        "NotificationHandler"
      ]
    }
  ]
}
```

## Reading

- `capacium-ops` is the only layover consuming the migration runner
  (`MigrationRunner`, `MigrationTargetConfig`, `ApplyResult`,
  `runner_from_config`); its `[postgres]` extra in `pyproject.toml` is the
  migration-runner target and is intended, not drift.
- `capacium-ops` does **not** consume `MirrorHandler`; the other four layovers
  do.
- Pins measured from each layover's `pyproject.toml` dependency line on
  2026-08-23.
- Re-measured on 2026-09-04: all five layovers now resolve `ops-engine` at
  `@v3.0.0`; the declaration previously carried the stale `2.2.0` (CFG-005). The
  pins in this document are the values read from each layover's own
  `pyproject.toml`, cross-checked by `tests/test_pin_drift_check.py`.
