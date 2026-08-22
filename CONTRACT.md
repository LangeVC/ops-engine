# Public Surface Contract

The public surface of `ops-engine` is everything imported as a bare name from
the top-level package:

```python
from ops_engine import <name>
```

As of this contract, `ops_engine.__all__` declares 34 names. Every name is
classified below as either **contract** (consumers may rely on it) or
**internal** (exposed only because Python has no hard privacy; not a promise).

## Machine-readable declaration

The single source of truth for the classification is the `contract` field in
this table. `kind` describes the shape of the name (`class`, `typedef`, or
`function`); it records intent and is not itself versioned.

```json
{
  "schema": 1,
  "package": "ops_engine",
  "semver": "SemVer 2.0.0",
  "exports": [
    {"name": "QueueManager",                  "kind": "class",    "contract": true},
    {"name": "QueueMetrics",                  "kind": "class",    "contract": true},
    {"name": "EventDeduplicator",             "kind": "class",    "contract": true},
    {"name": "OpsEngineConfig",               "kind": "class",    "contract": true},
    {"name": "OrgConfig",                     "kind": "class",    "contract": true},
    {"name": "RepoConfig",                    "kind": "class",    "contract": true},
    {"name": "AutoTriageConfig",              "kind": "class",    "contract": true},
    {"name": "StaleManagementConfig",         "kind": "class",    "contract": true},
    {"name": "WorkflowDispatchConfig",        "kind": "class",    "contract": true},
    {"name": "DependencyTriggerConfig",       "kind": "class",    "contract": true},
    {"name": "ReleaseConfig",                 "kind": "class",    "contract": true},
    {"name": "MergeConfig",                   "kind": "class",    "contract": true},
    {"name": "MirrorConfig",                  "kind": "class",    "contract": true},
    {"name": "NotificationConfig",            "kind": "class",    "contract": true},
    {"name": "NotificationChannel",           "kind": "class",    "contract": true},
    {"name": "MigrationSourceConfig",         "kind": "class",    "contract": true},
    {"name": "MigrationTargetConfig",         "kind": "class",    "contract": true},
    {"name": "TriageHandler",                 "kind": "class",    "contract": true},
    {"name": "DependencyTriggerHandler",      "kind": "class",    "contract": true},
    {"name": "StaleManager",                  "kind": "class",    "contract": true},
    {"name": "CronDispatcher",                "kind": "class",    "contract": true},
    {"name": "ReleaseHandler",                "kind": "class",    "contract": true},
    {"name": "MergeHandler",                  "kind": "class",    "contract": true},
    {"name": "MirrorHandler",                 "kind": "class",    "contract": true},
    {"name": "NotificationHandler",           "kind": "class",    "contract": true},
    {"name": "MigrationRunner",               "kind": "class",    "contract": true},
    {"name": "MigrationSource",               "kind": "class",    "contract": true},
    {"name": "LocalDirSource",                "kind": "class",    "contract": true},
    {"name": "GitRepoSource",                 "kind": "class",    "contract": true},
    {"name": "MigrationFile",                 "kind": "class",    "contract": true},
    {"name": "CheckResult",                   "kind": "class",    "contract": true},
    {"name": "ApplyResult",                   "kind": "class",    "contract": true},
    {"name": "runner_from_config",            "kind": "function", "contract": true},
    {"name": "ChangelogParser",               "kind": "class",    "contract": true}
  ]
}
```

Every name in `ops_engine.__all__` is classified `contract`. There are
currently **no internal names** in `__all__`; the internal surface lives in
submodules (`ops_engine.adapters.*`, `ops_engine.core.*`,
`ops_engine.modules.*`, `ops_engine.utils.*`) and is **not** covered by this
contract. In particular:

- `ForgeAdapter`, `GithubAdapter`, `ForgejoAdapter` (in
  `ops_engine.adapters.*`) are **internal** even though `README.md` shows them
  in layover examples — they are imported from their submodule paths, not from
  the top-level package, and are therefore not part of the guaranteed public
  surface.
- The module-side loader `ops_engine.config_loader` is **internal** to this
  dispatch and may not be modified or promised by it.

## Unpromised names

A name is **promised** only if it appears in the machine-readable declaration
above. A name **absent** from that declaration is **unpromised**: it carries no
guarantee, however it happens to be reachable. For example, importing a
submodule as a bare name (`from ops_engine import config_loader`) succeeds at
runtime because Python resolves the submodule, but `config_loader` is absent
from the declaration and is therefore unpromised. The same holds for the other
submodule names reachable as bare names — `adapters`, `core`, `modules`, and
`utils` — and for the adapter classes `ForgeAdapter`, `GithubAdapter`, and
`ForgejoAdapter`.

A consumer may rely on a name only when it is both listed in
`ops_engine.__all__` and classified `contract` here. Anything else is
unpromised and may change or vanish in a minor bump.

## What a version bump may change

Versioning follows [Semantic Versioning 2.0.0](https://semver.org/). Applied
to this public surface:

- A **major** bump may change any contract, in any direction: rename or remove
  a `contract` name, move a public name into a submodule, add a required
  parameter, remove a constructor argument, narrow an accepted type, or change
  a default that an existing consumer relies on.
- A **minor** bump may change the internal surface freely, and may extend the
  contract **additively only**: add a new `contract` name, add a new
  (keyword, optional-before-positional-safe) parameter with a backwards
  compatible default, add a field to a previously-sealed config model, or add
  a new optional method to a public class. A minor bump may **not** remove,
  rename, or otherwise break any existing `contract` name, nor change the type
  of an existing parameter, field, or return value.

## Test enforcement

`tests/test_public_surface.py` asserts, at CI time, that this declaration and
`ops_engine.__all__` agree: the set of names is identical and every name is
classified `contract`. A drift between the code and this document fails the
suite before any release.
