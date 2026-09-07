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

### Release destination as a committed file (ADP-008)

`load_ops_yaml` has exactly one consumer and that consumer is the release
workflow: `.forgejo/workflows/forgejo-release.yml` reads the committed `.ops.yaml`
at the repository root and resolves **both** the canonical Forgejo destination
and the mirror from it. No destination, forge repository, or API host is a
literal in the workflow layer, and no `vars.*` Actions variable is read. This
reverses the `vars.RELEASE_DESTINATIONS` regression (ADP-004), which moved the
destination out of the config layer into one CI system's variable store, against
the standing operator decision LVC-247/248 exists to implement.

The dangerous half of that regression — an unset variable resolving to `[]` and
a release publishing to the canonical forge only while reporting success — is
closed by named refusal in the workflow: a **missing** `.ops.yaml` is refused as
`MissingOpsYamlError`, an **empty** destinations list as
`MissingReleaseDestinationsError`, and a **malformed** file already refuses as
`OpsYamlError` from `load_ops_yaml` itself. None of these exits zero. The names
are workflow-layer strings, not new public surface: `load_ops_yaml`,
`OpsYamlError`, and `RepoConfig` remain the unpromised-but-documented names of
`ops_engine.config_loader` described above.

The **git mirror** is the other consumer of that same destination list, and it
was the next place to bypass the config layer. A mirror's push remote is a
destination like any release target, so it is read from the committed `.ops.yaml`
and never restated as a literal in the workflow layer. `.forgejo/workflows/mirror.yml`
resolves the github destination (`destinations` entries whose `forge` is
`github`) out of `.ops.yaml` and constructs its push remote from that value plus
the `GH_MIRROR_TOKEN` secret; a missing github destination is a named refusal
(`MirrorDestinationBoundaryError`), never a silent Forgejo-only mirror. The
resolver is a strict line-scan that reads only the rigid `destinations` list the
committed `.ops.yaml` declares; it does not parse YAML (REL-006/REL-010 keep
yaml and pydantic off this bare runner). It strips scalar quoting around a value
— `repo: "org/repo"` resolves to `org/repo` exactly as `load_ops_yaml` does —
and refuses by name any value still carrying syntax it cannot parse (an inline
comment), rather than emit raw bytes (quotes, a comment) into a push remote.
That refusal-first property is what keeps a file the real loader accepts AND
this scanner does not misread from silently choosing a remote a reviewer never
saw.

### Destination boundary (ADP-009)

The destination must come from the config layer; this section fixes *where a
workflow under `.forgejo/` may and may not take it from*. These two shapes are
refused, because each took a destination out of the config layer and back into a
place a person edits without a review or a single source of truth:

- **a user-defined CI variable as a destination.** `vars.<NAME>` is the CI
  system's repo-/org-level *variable store* — user-managed, unreviewed data. A
  destination rendered into `vars.RELEASE_DESTINATIONS` and read back out (the
  pre-ADP-008 regression, ADP-004) carried the destination exactly there.
- **a hardcoded repository or API host.** A destination a release or mirror
  reaches is named by value instead of read from `.ops.yaml`. The gate refuses
  the forged-destination literal shapes the workflows carry:
  - `HOST/OWNER/REPO[.git]` in an `http(s)://` remote or URL;
  - the scp git remote `git@HOST:OWNER/REPO[.git]`;
  - a forge API host (`api.github.com`, `uploads.github.com`) named by value —
    the forge identity half of a split destination, which the pre-ADP-004
    release workflow set as `GH_API` and concatenated with `GH_REPO` into its
    request. The register is refused at its host, because the OWNER/REPO half
    travels beside it and no single line carries both.
  The pre-ADP-008 release workflow reached for the Actions variable; the mirror
  workflow hardcoded `github.com/LangeVC/ops-engine` as its push remote; the
  pre-ADP-004 workflow hardcoded `api.github.com` + `LangeVC/ops-engine`. All
  are the shape refused here.

What is **not** refused, because it is not in either shape:

- **Secrets are not destinations.** `secrets.<NAME>` carries a *credential* (a
  token), never the repository it authenticates to. The token stays in the
  credential store; only the repository it pushes to moves into the config
  layer. Refusing `secrets.GH_MIRROR_TOKEN` would be refusing nothing to do with
  a destination and would be switched off within a fortnight.
- **Forgejo-provided event context is not a user variable store.**
  `github.repository`, `github.ref_name`, `github.server_url` identify the run
  the runner was given; they are the *event*, not user-managed variables, and a
  destination workflow is allowed to read them. The distinction this line draws
  is deliberately between `secrets.*`/`github.*` (permitted inputs) and
  `vars.*` (the user variable store a destination must not ride in).
- **tool-fetch suppliers.** A destination workflow still downloads tool bytes
  — `actions/checkout` fetches a released action, `google/osv-scanner` fetches a
  released binary. Those suppliers are not a release or mirror the workflow
  produces, and the handful are whitelisted as committed tuples in the gate.
- **comment text is documentation, not a destination.** A forge host or
  repository that appears only in a YAML comment is a note *about* the
  workflow, never a reach the workflow executes, so the gate skips the comment
  portion of each line (everything from a `#` that is outside a `${{ }}`
  expression or a quoted scalar and sits at the start of the line or after
  whitespace) before any rule runs. `mirror.yml`'s resolver already skips `#`
  comment lines the same way; two readers of one tree must agree on what a
  comment is, and a gate that failed on documentation text would be switched
  off within a fortnight. Skipping a comment cannot hide a live destination:
  a destination inside `${{ }}`, inside a quoted URL, or as an unquoted
  literal is still scanned even on a line that also carries a comment, because
  only the comment portion is removed.

`scripts/ci_variable_boundary_gate.py` enforces the boundary. It is **stdlib-only**
and never imports `ops_engine` (REL-006). It scans every workflow under
`.forgejo/` and refuses any file naming either refused shape, reporting the
file, the 1-based line, and the offending token. It is wired into
`.forgejo/workflows/release-gate.yml`, which runs it over the tree before the
version gate, so a workflow that reintroduces a destination by value fails the
release gate build at tag time — before `forgejo-release.yml`'s release step can
publish to a destination nobody reviewed. `mirror.yml` both reads its destination
from the config layer and is covered by the same gate.

The destination-host register the gate refuses against ships only its
**universal** set — `github.com`, `www.github.com`, `gitlab.com`, `codeberg.org`,
`git.sr.ht`, the public forges any adopter recognises. A self-hosted instance is
not in that set: an organisation's own forge host is organisation knowledge and
arrives from the config layer via the `--dest-hosts` file, exactly as the
organisation names arrive via `--org-vocab` and the CI variable names via
`--ci-env` on the `src/` side (see the Layer-1 boundary below). With no
`--dest-hosts` file the gate refuses only the universal five; an organisation
that declares no forge host gets no check for one. The release gate derives the
**bare** forge host from `github.server_url` — Forgejo-provided event context
that names this instance — stripping the scheme and any port, path, query or
fragment, and feeds it through `--dest-hosts`, so this repository's own
workflows stay gated on its private forge while neither the engine nor the gate
nor the workflow names a LangeVC host by value. A `server_url` that does not
reduce to a bare host (an empty or otherwise unparseable value) makes the
derivation refuse by name rather than silently disable the destination gate: the
step fails, never passes with an empty host set.

## Layer-1 boundary (ADP-010)

Layer 1 — the engine's own `src/` tree — knows no organisation and no CI
system. Two shapes are refused wherever they appear as a **live value in
executing Python**, each a form of Layer-1 knowledge that belongs in the calling
layer instead:

- **an organisation name as a literal.** A string constant whose value equals an
  organisation term the caller declares. The historical case is the rate-limit
  decorator `@track_rate_limit(namespace="capacium-ops")` in
  `ops_engine.modules.health_monitor`: the namespace was an organisation name
  baked into executing code. It must arrive as configuration or an argument —
  `HealthMonitor.run` now takes `repo`, `token` and `namespace` as caller-supplied
  arguments, and the CLI carries `--repo`, `--token` and `--rate-limit-namespace`.
  The module reads no CI environment variable and names no organisation by value.

- **a direct CI-environment read.** `os.environ.get("NAME")` (or
  `os.getenv("NAME")`) with a literal variable name the caller declares. The
  historical case is `os.environ.get("GITHUB_REPOSITORY")` (and `GITHUB_TOKEN`)
  in `health_monitor.py`: the engine reaching into one CI system's variable
  store. The value must instead arrive from the workflow that invokes the
  engine; a read whose variable name comes from configuration
  (`os.environ.get(cfg.var_name)`) is not a literal and is not refused.

The boundary is enforced by `scripts/ci_variable_boundary_gate.py`, the same
stdlib-only script that guards the destination boundary (REL-006; it imports
only the standard library, `ast` included, and never imports `ops_engine`).
Given `--py-dir src`, it parses every `.py` file with `ast` and refuses either
shape, naming the file and the 1-based line. The distinction between a
documentation mention and a live value is made by **parsing, not pattern**: a
docstring is a node in the parse tree (the first `Expr` holding a `Constant`
string of a module, class, or function body) and its value is skipped, and a
comment never reaches the AST. The same organisation name therefore passes
inside a docstring or comment and is refused as a live value.

The vocabulary is supplied from outside, never shipped in the gate
(REL-011's principle applied to the engine). `--org-vocab` is a file of
organisation names (one per line) and `--ci-env` a file of CI environment
variable names; with neither, the scan refuses nothing — an adopting engine
supplies the vocabulary that matches its own ecosystem. The release gate
(`.forgejo/workflows/release-gate.yml`) supplies both for LangeVC, so this
engine's `src/` stays gated the way this repository's release notes stay gated:
the vocabulary arrives from the workflow (the config layer), never from the
engine. The same principle governs the destination-host set (see "Destination
boundary" above): `--dest-hosts` is the file of organisation forge hosts, kept
out of the gate and supplied by the config layer, mirroring `--org-vocab` and
`--ci-env` here.

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

### Webhook secret boundary (ADP-006)

The `webhook_secret` is a credential read in exactly one place per adapter — the
HMAC check inside `parse_webhook` — never in the constructor. An adapter therefore
constructs with an empty `webhook_secret` (the factory's and release module's own
default) and can run the publication path, which never parses a webhook, without a
secret. The ingress guard is NOT weakened: `parse_webhook` on an adapter whose
`webhook_secret` is empty raises `ValueError` naming the missing secret, so an
unsigned ingress still refuses every payload. That refusal lives where the secret
is used, not where it is not.

### A credential per destination (ADP-011)

The factory also serves a credential **per destination**, keyed by forge value,
so a consumer with two destinations carrying two distinct credentials no longer
has to bypass the factory and hand-build adapters inline. The release workflow's
only real shape is two destinations with two *different* tokens — the Forgejo
runner token and the GitHub mirror token — which the single-token form ADP-002
shipped could not express.

- `Credential` (in `ops_engine.adapters.factory`) is an immutable value holding
  one forge's `token` and an optional `webhook_secret`. Both are caller input.
  The factory forwards them to the matching adapter and retains nothing: a
  `Credential` is passed into the call, not state the factory stores or logs.
  A credential is **not** a destination — the token stays in the caller's secret
  store; the factory learns only that a destination's forge HAS a credential in
  the mapping, never anything about the token's value beyond forwarding it.

- `adapters_for(destinations, *, credentials=...)` accepts a mapping from forge
  value (`"github"` / `"forgejo"`) to a `Credential`. Each destination is served
  only its own forge's credential, so two destinations with two distinct tokens
  are constructed in one call. When `credentials` is omitted, the legacy shared
  `token`/`webhook_secret` keywords still apply — every destination receives the
  same credential, exactly as before, so the previous form is unchanged.

- A destination whose forge has **no** entry in the `credentials` mapping raises
  `MissingCredentialError` naming the destination — a named refusal, never a
  silent unauthenticated call that would construct an adapter with an empty
  token. An unrecognised forge value still raises `UnknownForgeError` exactly as
  before, whichever credential shape supplies it.

`Credential` and `MissingCredentialError` are names in the internal submodule
`ops_engine.adapters.factory` and are therefore unpromised by this contract,
like the other submodule names; the behaviour above is documented as the
promised semantics the release workflow relies on. The release workflow
(`.forgejo/workflows/forgejo-release.yml`) is the consumer that exercised only
its two-different-token shape, which is why it previously hand-built each
adapter; it now delegates adapter construction to the factory's
credential-per-destination form.

## Release publication to every destination (ADP-003)

`ReleaseHandler.publish_release(...)` is the release module's publication entry
point: it turns a tag, release notes and a directory of built artifacts into a
release, created and asset-attached on **every** destination resolved from
config. It is an async `staticmethod` on the public `ReleaseHandler` class; the
method itself is additive and is not listed in the `methods` array above, which
covers only the mirror-destination methods whose semantics are signature-guarded.

One build, published outward; nothing re-downloaded (REL-010). The inputs are a
tag, release notes, and a directory of built artifacts on disk. The destinations
come from the config layer via `resolve_destinations` (DST-003), each destination
is turned into an adapter via the factory `adapters_for` (ADP-002), and for each
destination the release is created (with the name rendered from the configured
`name_template`) and every artifact in the directory is attached via
`upload_release_asset` (ADP-001). The artifacts' bytes are read from the given
directory — nothing is downloaded.

A destination that fails mid-publication is recorded, and the remaining
destinations still run: a partial publication is a **reported state**, not an
exception that vanishes and not a silent success. The return value is a
`PublicationReport` (`ops_engine.modules.release`) whose `published` /
`failed` lists name each destination as `forge:repo`, and whose `to_dict()`
serializes that state to a plain dict so a cockpit (or an operator) can see
that a mirror is behind without parsing log lines.

## What the published artifacts carry (ADP-005 / ADP-012)

The source distribution is governed by an explicit allowlist
(`[tool.hatch.build.targets.sdist] only-include = ["src/ops_engine"]` in
`pyproject.toml`), not by hatchling's default whole-tree VCS sweep — the sweep
ships `.forgejo/` in every release and, through the same gap, once shipped
roughly seven hundred virtual-environment files (REL-008). The allowlist is a
**holdover of a deliberate decision**, not an accident. What follows sets down,
as decisions, the three exclusions the allowlist produces as a side effect, so
`tests/test_sdist_contents.py` guards what was *decided* and not merely what the
allowlist happens to admit.

The **shipped top-level set is fixed** and asserted exactly two ways (extra and
missing both fail the contents suite):

- the package tree, `src/ops_engine/` — the source a consumer rebuilds a wheel
  from;
- the four core metadata files hatchling force-includes with it: `pyproject.toml`,
  `README.md`, `LICENSE` and — admitted by hatchling's VCS-force-include, not by
  the allowlist's rule — `.gitignore` (plus `PKG-INFO`, generated at build time).

The wheel target carries exactly the same one package
(`[tool.hatch.build.targets.wheel] packages = ["src/ops_engine"]`), so the sdist
and the wheel package the same tree; the sdist is the *source* form of that same
wheel, not a second product.

### Decision 1 — the test suite and developer tooling do not ship (setup is a checkout step)

`tests/`, `scripts/`, `docs/`, `examples/` and the repository's CI under `.forgejo/`
are **deliberately absent** from the source distribution. The sdist is a
source distribution of one Python package: it carries nothing a consumer of that
package does not need to rebuild it. The test suite that verifies an Apache-2.0
distribution is **repository** content, consumed by running `python3 -m pytest`
against a **source checkout** — the place `[project.optional-dependencies] dev`
(pytest, pytest-asyncio, respx, ruff) targets. A maintainer of a distribution
therefore runs the suite in CI over the checkout that built the artifact — the
release gate at `.forgejo/workflows/release-gate.yml` and the engine's own strict
contents tests (`tests/test_sdist_contents.py`) are that check — not from within
an unpacked sdist. Because the sdist ships neither the tests nor those fixtures,
the `dev` extra is scoped to a checkout and is documented as such in `pyproject.toml`;
installing `ops-engine[dev]` from the published artifact gives the tooling but not
the tests, which is why no prose here or in the README suggests the artifact can
self-verify. Verification of the published artifact happens over the git tree the
artifact was built from.

### Decision 2 — `constraints.txt` does not travel, so reproducibility is **from the checkout**

`constraints.txt` pins the *build* resolution (`uv pip compile pyproject.toml`).
It is a repository commit that the release workflow installs into its isolated
build venv **before** building, together with a fixed `SOURCE_DATE_EPOCH`. It does
**not** ship in the sdist (the allowlist admits only `src/ops_engine` and the
force-included metadata). Consequently the byte-identical reproducibility the
release workflow produces — two builds of one tag, identical resolution, identical
archives — holds **of the checkout**, never **of the artifact**: a consumer who
unpacks the sdist does not receive the file that pins the build set, so two
rebuilds of that sdist by two people at two different times are byte-identical
only if they happen to install the same build dependencies. Building a wheel
*from the sdist alone* is guaranteed and reproduces the wheel exactly **for the
same hatchling and build inputs**; what is **not** guaranteed from the sdist alone
is the cross-environment byte identity the release's committed `constraints.txt`
provides. Every statement of reproducibility in this file and in `README.md`
therefore carries the **from the source checkout** qualifier, and no statement
claims reproducible-from-artifact.

### Decision 3 — `.gitignore` ships, admitted by hatchling, not by any allowlist rule

`.gitignore` is present in the sdist because hatchling force-includes the VCS
exclusion file alongside `pyproject.toml`, `README.md`, `LICENSE` and `PKG-INFO`
regardless of the allowlist — it is not a member any `only-include` rule admits.
It is harmless (it excludes nothing a consumer wants) and it is **kept**: encoding
an `exclude` to strip it would fight hatchling's force-include for no consumer
benefit. The contents test names it in the decided set alongside the other
force-included metadata, so its presence is asserted, not assumed.

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
