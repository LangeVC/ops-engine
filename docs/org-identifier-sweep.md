# Org Identifier Sweep — Repo Reference Resolution

Every repo reference in a layover `config.yml` must resolve through the
**canonical org key** — the Forgejo `lower_name`, always lowercase. This sweep
enumerates every surface in a layover config that names a repository and
declares how each reference resolves. A repo reference whose org portion is
not a known canonical org fails the **config resolution** with a named error,
never a dispatch against the forge API.

## Background

FFR-200-1 established that the canonical org key equals the Forgejo
`lower_name` (`src/ops_engine/config_loader.py` — `canonical_org_key`,
`OpsEngineConfig.get_repo_config`). Display identifiers — `full_name`,
`login`, `username`, `forgejo.display_name`, `github.login` — are data stored
under the key, never the key itself. This sweep applies that rule to every
repo reference a layover config can carry.

## What a repo reference is

| Surface | Shape | Resolution |
|---------|-------|------------|
| `orgs.<key>` | canonical org key | the key is canonical by construction (`lower_name`) |
| `orgs.<key>.repositories.<name>` | repo name | repo name stays exact; the org part is the canonical key |
| `orgs.<key>.repositories.<name>.dependency_triggers[].target_repo` | `org/repo` | org portion resolved against the canonical keys; repo portion passed through |

Two reference forms carry an org portion that must resolve through the
canonical key path:

1. a reference written with display case (e.g. `LangeVC/ops-engine`) resolves
   to the config stored under the canonical key `langevc`;
2. a reference keyed on a free-form display name
   (`Lange Ventures & Consulting/ops-engine`) does **not** resolve — the
   display name is not a repo-addressable org identifier and must never be
   used as a key.

## Failure rule

A configured target whose org portion does not resolve to a known canonical
org raises `ConfigSectionError` (section `orgs`) from the config resolution —
**before** any dispatch is issued against the forge. The unknown org is caught
at load/resolution time, never by a failed `POST /repos/<org>/<repo>/dispatches`.

## Machine-readable declaration

The single source of truth for the sweep is the JSON block below. The org set
uses the same example orgs as the FFR-200-1 tests (`langevc`, `fusionaize`).

```json
{
  "schema": 1,
  "package": "ops_engine",
  "rule": "every repo reference resolves through the canonical org key (Forgejo lower_name); a target that does not resolve to a known canonical org fails the config resolution, not the dispatch",
  "reference_surfaces": [
    {"field": "orgs", "kind": "canonical-key"},
    {"field": "repositories", "kind": "repo-name"},
    {"field": "dependency_triggers[].target_repo", "kind": "org/repo"}
  ],
  "orgs": [
    {
      "canonical_key": "langevc",
      "forgejo_display_name": "Lange Ventures & Consulting",
      "github_login": "LangeVC",
      "repos": ["ops-engine", "faigrid"],
      "targets": ["fusionaize/faigrid"]
    },
    {
      "canonical_key": "fusionaize",
      "forgejo_display_name": "fusionAIze",
      "github_login": "fusionAIze",
      "repos": ["faigrid"],
      "targets": ["langevc/ops-engine"]
    }
  ]
}
```

## Five layover configs — per-file sweep findings

The rule above is swept against every layover configuration in production: the
five org-layover `config.yml` files checked out under `langevc/` on the Forgejo
canonical host. This section lists, per file, the org key it declares and every
repo reference it carries that does **not** resolve through the canonical key
path. A finding is any repo address whose org portion is a display identifier
(`github.login` or `forgejo.display_name` casing) rather than the Forgejo
`lower_name`, or a top-level org key that is not already its canonical `lower_name`.

Measurements taken 2026-08-23.

| Layover `config.yml` | Declared org key | Canonical? | Non-canonical repo references |
|----------------------|------------------|------------|-------------------------------|
| `lvc-ops` | `LangeVC` | no → `langevc` | `github` mirror strings are display data, not repo refs; no `target_repo` refs |
| `capacium-ops` | `Capacium` | no → `capacium` | 8 `target_repo` refs keyed on display-cased org `Capacium/…` |
| `elementeer-ops` | `elementeer` | yes | none — all targets already `elementeer/…` |
| `fusionaize-ops` | `fusionaize` | yes | 6 `target_repo` refs keyed on display-cased org `fusionAIze/…` |
| `skillweave-ops` | `SkillWeave` | no → `skillweave` | no `target_repo` refs; a display-cased `mirror_url` host only |

### Findings details

- **`lvc-ops`** — top-level key `LangeVC` is the GitHub login, not the canonical
  `langevc`. It currently carries no `dependency_triggers`; the `mirror.github`
  and `github_name` values are mirror/delivery data, not repo references subject
  to canonical resolution. Rekey the org onto `langevc` (FFR-200-2)
  `migrate-org-keys.py`).
- **`capacium-ops`** — top-level key `Capacium`: display-cased, not canonical
  `capacium`. All 8 `dependency_triggers[].target_repo` values repeat that
  display-cased org (`Capacium/capacium`, `Capacium/capacium-exchange`, …).
  Each is a repo reference that must key on `capacium`.
- **`elementeer-ops`** — top-level key `elementeer` is already canonical; all
  `target_repo` values (`elementeer/elementeer-addon-voxel`,
  `elementeer/elementeer-mcp`) are already canonical. No findings.
- **`fusionaize-ops`** — top-level key `fusionaize` is already canonical, but 6
  `target_repo` values still carry the display-case GitHub login as the org
  portion: `fusionAIze/faiops-browser`, `fusionAIze/faiops-cli`, `fusionAIze/faios`,
  `fusionAIze/fusionaize-sdk` (×2), `fusionAIze/homebrew-tap`. Each must key on
  `fusionaize`. One target (`elementeer/core`) is already canonical.
- **`skillweave-ops`** — top-level key `SkillWeave`: display-cased, not canonical
  `skillweave`. No `target_repo` refs; its only org-addressable string is a
  display-cased `mirror_url` host (`github.com/typelicious/…`), which is mirror
  delivery data, not a repo reference.

### Machine-readable sweep declaration

```json
{
  "schema": 1,
  "package": "ops_engine",
  "measured": "2026-08-23",
  "layovers": [
    {
      "config": "lvc-ops",
      "declared_org_key": "LangeVC",
      "canonical_org_key": "langevc",
      "findings": []
    },
    {
      "config": "capacium-ops",
      "declared_org_key": "Capacium",
      "canonical_org_key": "capacium",
      "findings": [
        "Capacium/homebrew-tap-capacium",
        "Capacium/capacium-action-validate",
        "Capacium/capacium-exchange",
        "Capacium/capacium-crawler",
        "Capacium/capacium",
        "Capacium/capacium-models"
      ]
    },
    {
      "config": "elementeer-ops",
      "declared_org_key": "elementeer",
      "canonical_org_key": "elementeer",
      "findings": []
    },
    {
      "config": "fusionaize-ops",
      "declared_org_key": "fusionaize",
      "canonical_org_key": "fusionaize",
      "findings": [
        "fusionAIze/faiops-browser",
        "fusionAIze/faiops-cli",
        "fusionAIze/faios",
        "fusionAIze/fusionaize-sdk",
        "fusionAIze/homebrew-tap"
      ]
    },
    {
      "config": "skillweave-ops",
      "declared_org_key": "SkillWeave",
      "canonical_org_key": "skillweave",
      "findings": []
    }
  ]
}
```
