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
