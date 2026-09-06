# Public Surface Contract

The public surface of `ops-engine` is everything imported as a bare name from
the top-level package:

```python
from ops_engine import <name>
```

As of this contract, `ops_engine.__all__` declares 35 names. Every name is
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

By the same line, a model declared `contract: true` promises the **name**, not
the **shape**. `MirrorConfig` is one of the 35 exports and is classified
`contract: true` by name only: the `methods` array above gates the
mirror-destination methods on `MirrorHandler`, not the fields of any model. A
consumer may rely on the name `MirrorConfig` remaining importable and classified
`contract`, but its fields — `github`, `visibility`, `github_name`, and the rest
— are documented in prose (here and in `config_loader.py`) and are not
machine-checked. This answers, rather than silences, the gap CFG-001 named:
field-level gating of a model's shape would be a schema change (schema 3), out
of scope for this revision.

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
    {"name": "Destination",                   "kind": "class",    "contract": true},
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
      "kwargs": ["config", "gh_repo_owner", "gh_repo"],
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

The mirror destination is resolved from **two** sources in a fixed precedence,
listed first to last:

1. **Config (primary).** `MirrorConfig.github` (declared by CFG-001) is the
   Layer-2 destination — a single `owner/name` string written by the layover
   config. When non-empty it resolves the destination verbatim; its owner is
   its own prefix. On success `source` is `"config"`.
2. **Variables (deprecated override).** The two Actions variables below are
   consulted only when the config carried no destination. On success `source`
   is `"double match"`.

**Precedence: the config wins.** When both a non-empty `config.github` and the
variables are supplied, `config.github` is used. The variables are deprecated
(removed in 4.0.0, DEC-003), and a stale variable must not silently override a
correct config — otherwise the mapping would still live in the variable store
this feature exists to retire.

The deprecated variable path resolves from **two** Actions variables — the two
halves of one contract, not two sources of one value:

- `GH_REPO_OWNER` — **ORG scope**, the GitHub owner that owns the mirror
  (e.g. `Capacium`).
- `GH_REPO` — **REPO scope**, the full `owner/repo` destination
  (e.g. `Capacium/capacium`).

**Both are required** on the variable path. There is no precedence between them
and no fallback: an unset variable is a hard refusal, never a computed
candidate.

- `MirrorHandler.resolve_destination(*, config, gh_repo_owner, gh_repo)`
  resolves the destination. It refuses — before any network call — in this
  order: the config path is tried first (`config.github` non-empty), then the
  variable path: `gh_repo_owner` unset (naming the ORG-scope variable),
  `gh_repo` unset (naming the REPO-scope variable), then a **case-sensitive
  double match**: `gh_repo`'s owner prefix must equal `gh_repo_owner` exactly
  (no lower/title/slug; owner and destination are used verbatim,
  whitespace-stripped only). The config destination is likewise used verbatim
  and case-sensitively — the awkward corpus (`elementeer`, `fusionAIze`,
  `Veeona-AI`) resolves exactly, never re-cased. On success `source` is
  `"config"` (config path) or `"double match"` (variable path).

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

## Destination model (DST-001)

`Destination` (a `contract` name above) is the canonical form a repository
publishes its destinations in. The forge is a **value**, not a key name:

```yaml
destinations:
  - forge: github        # github | forgejo | gitlab | local
    repo: LangeVC/ops-engine
    role: mirror         # mirror | release | replica
    visibility: public
```

`Destination` fields: `forge` (the forge, default ``"github"``), `repo` (the
``owner/name`` or forge-specific destination string, required), `role`
(``"mirror"`` | ``"release"`` | ``"replica"``, default ``"mirror"``), and
`visibility` (``"public"`` | ``"private"`` | ``""``). `RepoConfig.destinations`
holds the list, and `RepoConfig.resolve_destinations()` returns it with the
deprecated mirror aliases folded in.

The three measured mirror shapes migrate as follows:

- **`mirror.github` + `mirror.visibility`** (lvc-ops) — resolves to one
  `Destination(forge="github", repo=<github>, role="mirror", visibility=<visibility>)`.
- **`mirror_url` + `primary_forge`** (elementeer-ops, skillweave-ops) — resolves
  to one `Destination(forge=<primary_forge>, repo=<mirror_url>, role="mirror")`.
- **absent `mirror` section** (capacium-ops, fusionaize-ops) — resolves to an
  empty list: the deliberate "unmirrored" case, not an error.

The deprecated aliases `mirror.github`, `mirror.visibility`, `mirror_url` and
`mirror.primary_forge` remain on `MirrorConfig` only as aliases. Resolving any
non-empty alias through `resolve_destinations()` emits a `DeprecationWarning`
naming the removal version (4.0.0, DEC-003). `resolve_destinations` is an
instance method on `RepoConfig`; it is not a network call and holds no
organisation knowledge, deferring to DST-003's forge-neutral `resolve_destinations`
entry point for the pure resolver (see "Destination resolver (DST-003)" below).

## Layer 3 — `.ops.yaml` (DST-002)

Layer 3 is a per-repository override file named `.ops.yaml`, living at the
repository root. It carries a `RepoConfig`-shaped mapping — the same shape as
one `repositories.<repo>` entry of the Layer-2 org `config.yml`.

`load_ops_yaml(repo_dir)` reads it. The loader and `OpsYamlError` live in the
unpromised submodule `ops_engine.config_loader`; neither is added to the public
surface above, so neither is a `contract` name in the machine-readable
declaration.

**Precedence — Layer 3 overrides Layer 2, field by field.** For each top-level
`RepoConfig` field the `.ops.yaml` explicitly sets, the Layer-3 value wins; a
field it leaves unset keeps the Layer-2 value.

**A Layer-3 list REPLACES the Layer-2 list, never extends it.** This is true
for `destinations`, `workflow_dispatches` and `dependency_triggers`. The reason
is that a list field is a source of truth for a *set* the repo has, not an
accumulator: extension would make the resolved set the union of two files, which
a reader cannot compute without holding both files in hand, and would silently
carry a Layer-2 destination the repo author believed they had overridden away.
Replacement keeps every field equal to exactly what its most-local declaration
stated. This decision is sealed by
`tests/test_layer3_overrides.py::test_layer3_list_replaces_layer2`, which fails
if the replace-vs-extend behaviour changes.

**An absent `.ops.yaml` is the normal case.** `load_ops_yaml` returns `None`;
the caller keeps the Layer-2 config unchanged. Most repositories carry no
Layer-3 file.

**A malformed `.ops.yaml` is a named refusal, never a silent fallback.**
`load_ops_yaml` raises `OpsYamlError` naming the exact file. A silent fallback
to Layer 2 is forbidden because an author who wrote a broken override must be
told, not left believing the override is in force.

`RepoConfig.merge_layer3(layer3)` applies a loaded Layer-3 `RepoConfig` over a
Layer-2 one, implementing the precedence above.

## Destination resolver (DST-003)

`resolve_destinations(config, repo, *, repo_dir=None)` is the Layer-1
destinations entry point: the pure resolver that lets a cross-org operator tool
resolve a repository's destinations without a layer break. It lives in the
unpromised submodule `ops_engine.modules.mirror` and is **not** a `contract`
name (it is absent from the exports table above, exactly like the adjacent
`MirrorDestinationResolution` and `MirrorDestinationError` names); it is
documented here as the promised behaviour the operator tools rely on.

- `config` is a typed `OpsEngineConfig` (Layer 2, already loaded). The org set
  and the config object arrive at the call site; the function walks nothing and
  holds no org-to-config mapping.
- `repo` is the `org/repo` selector, split verbatim (case-sensitive, `/` split
  once) into the org key and repo name.
- The function performs **zero network calls** and imports no forge adapter. It
  only reads the caller-supplied config object, the `repo` selector, and — when
  `repo_dir` is supplied — a Layer-3 `.ops.yaml` at that directory.

Resolution order, delegating to the config layer so the DST-002 precedence is
implemented once:

1. `config.get_repo_config(org, repo_name)` resolves the Layer-2 `RepoConfig`.
2. If `repo_dir` is supplied, `load_ops_yaml(repo_dir)` is consulted and, when a
   `.ops.yaml` is present, merged over the Layer-2 config via `merge_layer3`
   (field-by-field override; lists replace). An absent `.ops.yaml` is the normal
   case and changes nothing.
3. `RepoConfig.resolve_destinations()` folds the destination list together with
   the deprecated mirror aliases into the final `Destination` list.

The v3.1.0 double match still applies to any destination whose forge declares
an owner form. That equivalence lives in `MirrorHandler.resolve_destination`
(`ops_engine.modules.mirror`) — the forge-neutral resolver whose case-sensitive
double match (config path and the deprecated two-variable path) is unchanged and
simply takes its inputs from whatever supplies them. `resolve_destinations` does
not re-implement that check; it returns `Destination` objects whose
`forge`/`repo` values are the inputs `resolve_destination` then verifies
verbatim, case-sensitively, before any network call.

## Adapter factory (ADP-002)

The adapter factory lives in the unpromised submodule `ops_engine.adapters.factory`
and is **not** a `contract` name (it is absent from the exports table above,
exactly like the `resolve_destinations` entry point it completes). It is the
second half of the Layer-1 destinations seam: `resolve_destinations` turns a
config object into a `Destination` list; the factory turns that list into
constructed adapters.

- `adapter_for(destination, *, token="", webhook_secret="", base_url="")` maps
  one `Destination` to the matching `ForgeAdapter` by the `forge` value:
  `"github"` yields `GithubAdapter`, `"forgejo"` yields `ForgejoAdapter`.
  `token` and `webhook_secret` are caller-supplied credentials; `base_url` is the
  Forgejo instance address and is caller input (unused for GitHub). The factory
  holds a forge-name-to-adapter mapping and nothing else: no organisation name is
  held and no network call is made — nothing is fetched, discovered, or hardcoded.

- An unrecognised `forge` value raises `UnknownForgeError` naming the value and
  the supported set. This is a refusal, never a fallback: the factory will not
  silently produce a GitHub adapter for a destination that named another forge.

- `adapters_for(destinations, *, ...)` maps a destination list to an adapter
  list. An empty destination list is the normal, deliberate "unmirrored" case and
  maps to an empty adapter list — never a crash.

`UnknownForgeError` is a name in the internal submodule `ops_engine.adapters.factory`
and is therefore unpromised by this contract, like the other submodule names; the
behaviour above is documented as the promised semantics the release module
(ADP-003) relies on.

## Variable-name constants — deprecated

`MIRROR_OWNER_VARIABLE` and `MIRROR_REPO_VARIABLE` live in the unpromised
submodule `ops_engine.modules.mirror`. They hold the strings `GH_REPO_OWNER` and
`GH_REPO`, the single declaration of the variable names. As of DST-004 no
operator tool imports them: `scripts/mirror-destination-audit.py` and
`scripts/mirror-destination-propose.py` take their org set as repeated `--config`
paths and resolve each repository's mirror destination from the config layer via
`resolve_destinations`, so the audit no longer restates the strings. They are
**unpromised-but-internally-consumed**: they are not part of the public surface
this contract guarantees, and a consumer outside this repository must not rely
on them. Where the public methods above name a variable in their error text,
that text is the human-facing contract; the constants are the implementation
detail that keeps those method refusal messages truthful.

**Deprecated in 3.2.0, removed in 4.0.0 (DEC-003).** The two-variable path these
constants name is replaced by the config path: the Layer-2 `MirrorConfig.github`
field declared by CFG-001 and resolved by `MirrorHandler.resolve_destination`
(see "Mirror destination resolution" above). The variables remain only as a
*deprecated override*. **Until removal in 4.0.0, the variable path requires a
Forgejo Actions variable store:** `GH_REPO_OWNER` (ORG scope) and `GH_REPO`
(REPO scope) are Forgejo Actions variables, and that path has no config-only
equivalent. A consumer that still resolves a destination through these variables
therefore depends on a Forgejo Actions variable store; the config path carries
no such dependency. The constants may change or move in a minor bump, and the
public methods' error text moves with them; at 4.0.0 the constants are removed.

## Release notes address an external reader (REL-011)

The release description a tag posts (the CHANGELOG entry for that version,
sliced out by the notes step of `.forgejo/workflows/forgejo-release.yml`) is a
document addressed at an **external** reader: operators of other repositories,
not the team that wrote the release. `scripts/release_notes_audience_gate.py`
enforces that audience. It reads the extracted notes and refuses, with a named
error that quotes each offending token and the line it appears on, any note
that carries an internal ticket reference whose prefix the caller has declared
via `--ticket-prefixes`. The refusal exits non-zero and the workflow runs the
gate on the extracted notes **before** any release object is created, so a
failing gate fails the release run.

**The engine classifies no prefix by itself.** The code-and-number shape
`[A-Z]{2,5}-[0-9]+` is shared by an internal ticket reference and by
identifiers the external reader legitimately needs — a CVE advisory id, an RFC
or ISO number, PEP-8, gRPC, and ordinary technical prose (`UTF-8`, `SHA-256`,
`TLS-1.3`, `HTTP-2`, `AES-256`). The shape alone cannot tell an internal project
code from an external identifier, and no deny-list of universal prefixes is
ever sound, because that set is open-ended (`UTF`, `SHA`, `TLS`, `HTTP`, `SSL`,
`AES`, `RSA`, `IEEE`, `PNG`, `JPEG`, ...). So the gate does not attempt the
classification: a shape match is refused only when its prefix is among the
prefixes the caller supplies as `--ticket-prefixes`.

`--ticket-prefixes` is optional and supplies the organisation's own tracker
prefixes, one per line, each an uppercase `[A-Z]{2,5}` token. ops-engine is the
template and ships **no** organisation vocabulary, so with no `--ticket-prefixes`
and no `--forbid-file` the gate refuses **nothing** — an organisation that
supplies no vocabulary gets no vocabulary check. That is the correct default,
not a hole. This repository's own release workflow *does* supply it: because
ops-engine is a LangeVC repository, `.forgejo/workflows/forgejo-release.yml`
declares LangeVC's tracker prefixes (`LVC`, `OME`, `CORE`, `LNF`, `DST`, `REL`,
`CFG`, `FFR`) and this repository's own notes therefore stay gated. A layover
that adopts the workflow adapts that prefix list to its own organisation; the
vocabulary always arrives from the workflow (the config layer), never from the
engine. A `--ticket-prefixes` file that is NAMED but missing or malformed (a
line that is not one uppercase 2-5 letter token) is a named refusal, never a
silent skip that would release without the tracker prefixes the organisation
chose.

`--forbid-file` is optional and supplies organisation-supplied *withheld*
vocabulary terms, one per line — product names and project codenames that must
not reach an external reader — alongside the prefix set. ops-engine ships no
such terms, so the release workflow never passes the flag. `--forbid-file` and
`--ticket-prefixes` are independent: an organisation may run either alone, both
together, or neither. A forbid file that is present but malformed (a line that
is not a single whitespace-free term, or a path that does not resolve) is a
named refusal (`ForbiddenVocabularyError`), never a silent skip that would
release without the vocabulary the organisation chose.

The gate is **stdlib-only** and never imports `ops_engine` (REL-006): the bare
release runner carries neither yaml nor pydantic, so nothing outside the
standard library may execute there, and the gate's own tests likewise import
nothing outside the standard library.

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
