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

`schema` is `2`. Schema 2 adds the `methods` array: for the *methods* this
contract guarantees on its public classes, each entry records the ordered
positional-parameter names (`args`), the ordered keyword-only-parameter names
(`kwargs`), which of those parameters are *required* versus defaulted
(`required_args` / `required_kwargs`), and the method *kind* (`kind`: instance,
`classmethod`, or `staticmethod`, each sync or `async_`). `tests/test_public_surface.py`
derives these same facts from the class in source via `ast` and compares them
to this array, so a change in the parameter-NAME lists, in required-ness, or in
the method kind — with no matching declaration edit — fails CI.

That is the *whole* of what the gate checks. It does **not** gate: the
default *values*, type annotations, method *decorators* other than
`staticmethod`/`classmethod`, `*args`/`**kwargs` (none of the gated methods use
them), or the method **body**. In particular the semantic promises elsewhere in
this file — the case-sensitive double match, the check order, and the guarantee
that no network call happens before the double match — are **contract prose that
no machine check covers**; a reader who trusts CI to catch a change to those
promises is trusting something CI does not look at. The `methods` array does
**not** cover every method in the package — it covers only the
mirror-destination methods named in prose below, the ones whose semantics are
promised. Other methods on public classes are not signature-guarded here.

```json
{
  "schema": 2,
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
  ],
  "methods": [
    {
      "class": "MirrorHandler",
      "module": "ops_engine.modules.mirror",
      "name": "resolve_destination",
      "kind": "staticmethod",
      "args": [],
      "required_args": [],
      "kwargs": ["gh_repo_owner", "gh_repo"],
      "required_kwargs": []
    },
    {
      "class": "MirrorHandler",
      "module": "ops_engine.modules.mirror",
      "name": "prove_destination",
      "kind": "async_staticmethod",
      "args": ["destination"],
      "required_args": ["destination"],
      "kwargs": ["token", "api_base"],
      "required_kwargs": []
    }
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

## Mirror destination resolution (OME-012)

`MirrorHandler` (a `contract` name above) gains two additive methods that
together form the mirror-destination resolution contract. They are additive
methods on an existing public class, so they do not change the exports table
above; a consumer that pins the current version is unaffected until it chooses
to call them.

The mirror destination is resolved from **two** Actions variables — the two
halves of one contract, not two sources of one value:

- `GH_REPO_OWNER` — **ORG scope**, the GitHub owner that owns the mirror
  (e.g. `Capacium`).
- `GH_REPO` — **REPO scope**, the full `owner/repo` destination
  (e.g. `Capacium/capacium`).

**Both are required.** There is no precedence between them and no fallback: an
unset variable is a hard refusal, never a computed candidate.

- `MirrorHandler.resolve_destination(*, gh_repo_owner, gh_repo)` maps the two
  variables to their contract halves and returns a
  `MirrorDestinationResolution` (`destination`, `source`). It refuses — before
  any network call — in this order: `gh_repo_owner` unset (naming the ORG-scope
  variable), `gh_repo` unset (naming the REPO-scope variable), then a
  **case-sensitive double match**: `gh_repo`'s owner prefix must equal
  `gh_repo_owner` exactly (no lower/title/slug; owner and destination are used
  verbatim, whitespace-stripped only). On success `source` is `"double match"`.

- `MirrorHandler.prove_destination(destination, *, token, api_base)` proves the
  destination with two independent proofs before any push: **EXISTS**
  (`git ls-remote` proves the repository exists and is readable) and
  **IS OURS** (the repository's `permissions.push` for the authenticated token
  is true — reachability is not ownership). Neither proof creates a repository
  under any outcome.

The methods above implement the deployed preflight guard — the
"Preflight — resolve, double-match, verify, and vouch for the destination" step
of the canonical `.forgejo/workflows/mirror.yml` — which is the source of truth
for this contract. The check order matches it exactly: presence, double match,
existence, then permission.

`MirrorDestinationResolution` and `MirrorDestinationError` are names in the
internal submodule `ops_engine.modules.mirror` and are therefore unpromised by
this contract, exactly like the other submodule names. The promised surface is
the two methods on `MirrorHandler`; the exception a layover must handle is
`ops_engine.modules.mirror.MirrorDestinationError`.

## Variable-name constants

`MIRROR_OWNER_VARIABLE` and `MIRROR_REPO_VARIABLE` live in the unpromised
submodule `ops_engine.modules.mirror`. They hold the strings `GH_REPO_OWNER` and
`GH_REPO` — the *single declaration* of the variable names — and
`scripts/mirror-destination-audit.py` imports them rather than restating the
strings (restatement is how the contract drifted). They are
**unpromised-but-internally-consumed**: they are not part of the public surface
this contract guarantees, and a consumer outside this repository must not rely
on them. Where the public methods above name a variable in their error text,
that text is the human-facing contract; the constants are an implementation
detail that the in-repo audit consumes so the strings stay declared once. They
may change or move in a minor bump, and the audit (which imports them) moves
with them.

## Test enforcement

`tests/test_public_surface.py` asserts, at CI time, that this declaration and
`ops_engine.__all__` agree: the set of names is identical and every name is
classified `contract`. It also derives, for each entry in the `methods` array,
the ordered positional-parameter names, the ordered keyword-only-parameter
names, which parameters are required versus defaulted, and the method kind
(instance/`classmethod`/`staticmethod`, sync/async) from the class in source
via `ast`, and compares them to this array — so a stale declaration that
disagrees with the code on any of those specific facts fails the suite before
any release. Signature dimensions **not** compared this way — defaults' values,
type annotations, `*args`/`**kwargs`, and the method body — can change without
this test noticing, and the semantic promises in prose above (case-sensitivity,
check order, "no network call before the double match") are likewise **not
machine-checked** anywhere.

## Compatibility note

The `MirrorHandler` resolution contract was corrected in this revision. The
previous wording (an `override`-wins precedence model with a `gated fallback`)
described a retired design and never shipped: neither `resolve_destination` nor
`prove_destination` is present in `v3.0.0`
(`git show v3.0.0:src/ops_engine/modules/mirror.py | grep -c resolve_destination`
is `0`, and the same for `CONTRACT.md`). This is the correction of an
unreleased declaration, not a compatibility event: no layover can pin either
method against a released version.
