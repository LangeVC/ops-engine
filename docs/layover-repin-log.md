# Layover Repin Log

Records each repin of the layover version pins against `ops-engine`, with what
was actually changed and where it was measured.

## 2026-08-23 — repin to v3.0.0

Five layovers moved to `ops-engine v3.0.0` (tag `6c73e77`). The org key was
migrated in the same change, because v3.0.0 requires it: a display-case key
makes `get_repo_config` return nothing, so the handler never runs. That is the
defect that made the faigrid v1.8.0 release produce no release object.

The identity rule the migration follows:

```
              Forgejo (canonical)   GitHub (brand, customer-facing)
langevc       langevc               LangeVC
fusionaize    fusionaize            fusionAIze
capacium      capacium              Capacium
skillweave    skillweave            SkillWeave
elementeer    elementeer            elementeer
```

The Forgejo spelling is the config key. The GitHub spelling stays where it
already lived — mirror targets and `mirror_url` entries were not touched.

| layover | org key | pin before | pin after |
|---|---|---|---|
| `lvc-ops` | `LangeVC` → `langevc` | `v2.0.0` | `v3.0.0` |
| `capacium-ops` | `Capacium` → `capacium` | `v2.1.2` | `v3.0.0` |
| `elementeer-ops` | `elementeer` (already canonical) | `v2.0.0` | `v3.0.0` |
| `fusionaize-ops` | `fusionAIze` → `fusionaize` | `v2.0.0` | `v3.0.0` |
| `skillweave-ops` | `SkillWeave` → `skillweave` | `v2.0.0` | `v3.0.0` |

`capacium-ops` keeps its `[postgres]` extra; it is the migration-runner target
and is not drift.

Verified on each layover's default branch on the forge after merge, not in a
local checkout.

### Correction to this file's previous version

The entry this replaces claimed five layovers had been repinned to `2.2.0`. No
pyproject pin had been changed anywhere: that lane worked in `ops-engine`, and
the pins live in the layovers. `2.2.0` also never existed as a tag — the pins
resolve `@vX.Y.Z` against GitHub, so a merge would have broken all five.

The lesson is recorded rather than quietly overwritten: a lane that cannot
reach the file it describes can still describe it. A repin log must name the
commit in the repository whose pin changed, or it is a plan and not a log.

### Not covered here

Whether a running service was upgraded to v3.0.0 is a deployment question and
is not recorded by this file. `get_repo_config` running against v3.0.0 in
production requires the service to be redeployed; the pin only states what the
next install resolves.
