# Layover Repin Log

Records each repin of the layover version pins against `ops-engine`, with what
was actually changed and where it was measured.

## What a consumer needs to know which release carries a contract

A version pin (`@v3.0.0`) says which tag a layover resolves. It does **not**
say what that tag's public surface is. To know whether a given contract name or
method is available on a pin, a consumer needs three records, in this order:

1. **Which release introduced the contract.** `CONTRACT.md` documents the
   current surface but not the release boundary; the release boundary lives in
   `CHANGELOG.md`, one entry per release. A consumer reads the entry for the
   release it is pinned to and confirms the contract name is listed there.
2. **Whether that release is the one its pin points at.** `pyproject.toml`
   (`version`) is the source of truth in `.version.yaml`; a `git tag
   --contains <contract-commit>` answers "is this contract commit inside any
   tag yet". If the answer is empty, the contract exists on a branch only and
   no released version carries it.
3. **When an unreleased contract will be pinnable.** An "Unreleased" section at
   the top of `CHANGELOG.md` names what is not yet in any release. Each entry
   states where the change actually lives (a branch not yet merged, or master
   not yet tagged), because a change can be unreleased for two different
   reasons. A consumer that needs it waits for the release that folds that
   section into a numbered heading; until then there is no pinnable version and
   the requirement must not be dispatched. If the entry says the change is on
   an unmerged branch, the merge to `master` is a prerequisite to any release
   carrying it — the "first pinnable version" is the release cut *after* that
   merge, not the next release of any branch that lacks the code.

## 2026-09-03 — mirror-destination contract exists, but not pinnable (OME-002)

The mirror-destination resolution contract (`MirrorHandler.resolve_destination`
and `MirrorHandler.prove_destination`, commit `824bbf1`) exists on branch
`feature/OME-002-resolution-contract`. Three facts a consumer asking "can I pin
something that has the destination contract?" needs:

| question | answer |
|---|---|
| does the contract exist | yes — branch `feature/OME-002-resolution-contract`, commit `824bbf1` |
| is it on `master` | no — `git merge-base --is-ancestor 824bbf1 master` is false |
| is it in a release | no — `pyproject.toml` is `3.0.0`, tag `v3.0.0` (`21d003a`) predates it; `git tag --contains 824bbf1` is empty |

So there is **nothing pinnable today**: no tag carries these methods. What has
to happen first, in order:

1. `feature/OME-002-resolution-contract` is merged to `master`.
2. A release is cut from `master` after that merge (bumping the version past
   `3.0.0`, which is taken).

The release that follows step 2 is the first pinnable version. Until then a
layover that needs destination resolution must not repin at all — not to
`master`, and to no tag, because none exists that carries the contract (see the
stable ref in `docs/version-sync.md`). This is the honest answer to "which
release first carries it": none yet, and the two steps above are what must
happen first.

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
