# Mirror destination inventory — which repositories the organisation rule does not cover

Task: OME-003 audit, retrofitted to the OME-012 two-variable contract. Count the
repositories on both forges and classify every canonical (Forgejo) repository by
how the mirror-destination contract treats it. Corrects the never-counted "about
seventy" figure.

Measured: 2026-09-03, by reading both forges read-only (Forgejo
`git.langevc.com` canonical + GitHub mirror orgs) with an admin/owner token.
Tool: `scripts/mirror-destination-audit.py` in this repository. The script
changes neither forge and writes only its own report.

Reference decisions this audit builds on, not re-derives:
- The mirror destination is a **two-variable contract** (OME-012), not an org
  compose plus a repo override. The two halves carry no precedence between them:

      GH_REPO_OWNER  ORG scope   the GitHub owner            e.g. `Capacium`
      GH_REPO        REPO scope  the full owner/name         e.g. `Capacium/capacium`

  BOTH are required. A repository's `GH_REPO` is the destination **verbatim**
  (whitespace stripped only), and its owner prefix must equal the org's
  `GH_REPO_OWNER` in a **case-sensitive double match decided before any GitHub
  request**. Mismatch or an unset half refuses; that repository will never
  mirror. There is **no compose** of `GH_REPO_OWNER + repository name` — an unset
  `GH_REPO` is a hard refusal, never a computed candidate.
- OME-011 built the propose tool (`mirror-destination-propose.py`) that writes
  each repo's `GH_REPO` pairing; it has proposed, but the operator has not yet
  `--apply --confirm`-ed it. This inventory describes the world **as it is
  today**: only the two repositories that already carry a `GH_REPO` are
  presently configured to mirror.

## Method

Forgejo is the single source of truth: a push there mirrors a ref to GitHub. For
each canonical repository the audit reads the org-scope `GH_REPO_OWNER` and the
repo-scope `GH_REPO` (both live, not hardcoded), applies the double match and the
reachability test, and places the repository in exactly one of five classes:

  1. resolves and reachable        — double match OK, GitHub carries the name
  2. resolves but unreachable      — double match OK, GitHub lacks the name
  3. needs an exception            — the two forges disagree on the name
  4. not mirrored by policy        — no mirror workflow on the canonical repo
  5. refused before any GitHub request — an unset half or a double-match mismatch

The GitHub mirror owner is read live from the org-scope `GH_REPO_OWNER`; the
`--map` flag is an explicit override only. `*-planning` / `*-internal-docs` is
the only naming basis, and it is applied only as *evidence* next to a measured
absence of a mirror workflow, never as a list that overrides measurement.

Class 4 is never assigned on a hardcoded list: a repository is class 4 because
measured evidence shows it is not pushed by a mirror workflow on the canonical
side, and (where the marker applies) because the rollout records
`*-planning` / `*-internal-docs` as never public. A repository that could not be
read would be **UNMEASURED**, not silently class 4; no such repository was found
in this run.

## Real total

**78** canonical repositories across the six mirroring organisations on the
canonical Forgejo (below). GitHub holds more rows than 78 because the mirror
host orgs (`LangeVC`, …) carry `.github` and other long-lived public repos and,
for the skillweave mirror, the GitHub owner is `LangeVC` (skillweave has no
GitHub org of its own), so both the five langevc and four skillweave canonical
repos land under one owner. Two GitHub orgs added (`Vamerli`: 3 repos) have no
canonical twin on this canonical forge and are not part of the mirror rule.

The "about seventy" estimate was not counting this population — no single number
fits it:
- the 49-repo drift table + 2 skipped in `ops-002-rollout-inventory.md` = 51
  (those were only the *already-workflowed* repos, not the census);
- the GitHub host count (≈88 incl. `.github`/non-mirror repos) is higher still.

The count that matters and that was previously never established is the canonical
side: **78**. Under the two-variable contract, only **2** are presently
configured (class 1 — the two repositories that carry a `GH_REPO`); **60** carry
a mirror workflow but no `GH_REPO` and so refuse at the preflight (class 5); the
other **16** are deliberately not workflow-mirrored from the canonical forge
(class 4).

## Counts per class

| class | meaning | count |
|-------|---------|-------|
| 1 | resolves and reachable — double match OK, GitHub carries the name | 2 |
| 2 | resolves but unreachable — double match OK, GitHub lacks the name | 0 |
| 3 | needs an exception — the two forges disagree on the name | 0 |
| 4 | not mirrored by policy (deliberately) | 16 |
| 5 | refused before any GitHub request — unset half or double-match mismatch | 60 |

## Per org

| canonical org | GitHub mirror owner | class 1 | class 2 | class 3 | class 4 | class 5 |
|---------------|---------------------|---------|---------|---------|---------|---------|
| langevc | LangeVC | 0 | 0 | 0 | 1 | 8 |
| capacium | Capacium | 1 | 0 | 0 | 2 | 21 |
| elementeer | elementeer | 0 | 0 | 0 | 1 | 9 |
| fusionaize | fusionAIze | 0 | 0 | 0 | 0 | 18 |
| skillweave | LangeVC | 1 | 0 | 0 | 4 | 4 |
| veeona | Veeona-AI | 0 | 0 | 0 | 8 | 0 |

The owners are read verbatim from each org's `GH_REPO_OWNER` and are **not**
derivable from the org name: `elementeer` stays lowercase, `capacium` is
capitalised, `veeona` becomes `Veeona-AI`. The double match is case-sensitive,
so any normalisation of these values would break the very check the contract is
built on.

## Class 1 — resolves and reachable

Only two repositories in the entire corpus carry a `GH_REPO` (the full
`owner/name` destination), and both double-match their org's `GH_REPO_OWNER`:

| canonical (forgejo) | class | destination | note |
|---------------------|-------|-------------|------|
| capacium/capacium | 1 | `Capacium/capacium` | `GH_REPO` = `Capacium/capacium`, owner prefix matches `GH_REPO_OWNER` = `Capacium` |
| skillweave/skillweave | 1 | `LangeVC/skillweave` | `GH_REPO` = `LangeVC/skillweave`, owner prefix matches `GH_REPO_OWNER` = `LangeVC` |

These are the two repositories the pre-012 defect (recomposing the full
`GH_REPO` back onto the owner, yielding `Capacium/Capacium/capacium` and
`LangeVC/LangeVC/skillweave`) misclassified as unreachable. The audit now uses
the `GH_REPO` value verbatim; a recomposed string is unreachable in any output.

## Class 5 — refused before any GitHub request

A repository is class 5 when the workflow would stop at the preflight and never
ask GitHub: an unset `GH_REPO_OWNER`, an unset `GH_REPO`, or a `GH_REPO` whose
owner prefix disagrees with `GH_REPO_OWNER` (case-sensitive). All 60 class-5
repositories in this run are "`GH_REPO` unset at repo scope" — the parity between
the two forges is intact, but `mirror-destination-propose.py` has proposed the
pairings without them having been applied. No live repository currently carries a
double-match **mismatch** (the disagreement sub-case is exercised only by
`--selftest`, which proves it is reported as its own class-5 outcome and is
decided without a GitHub request).

| canonical org | repo | basis |
|---------------|------|-------|
| capacium | capacium-action-publish, capacium-action-validate, capacium-admin, capacium-app, capacium-bridge, capacium-bridge-tests, capacium-crawler, capacium-docs, capacium-exchange, capacium-github-app, capacium-internal-docs, capacium-marketplace-tui, capacium-mcp, capacium-models, capacium-ops, capacium-spec, capacium-test-lab, capacium-web, claude-code-capacium, jetbrains-capacium, vscode-capacium | `GH_REPO` unset at repo scope |
| elementeer | elementeer, elementeer-addon-voxel, elementeer-bridge, elementeer-docs, elementeer-internal-docs, elementeer-mcp, elementeer-ops, elementeer-planning, elementeer-pro | `GH_REPO` unset at repo scope |
| fusionaize | faifabric, faigate, faigrid, failens, faiops-browser, faiops-cli, faios, faisignal, faistudio, fusionaize-docs, fusionaize-internal-docs, fusionaize-metadata, fusionaize-metadata-public, fusionaize-ops, fusionaize-planning, fusionaize-project-template, fusionaize-sdk, grok-api-hook | `GH_REPO` unset at repo scope |
| langevc | agent-test-env, envctl, lvc-docs, lvc-internal-docs, lvc-ops, ops-engine, txt-humanizer, wp-test-env | `GH_REPO` unset at repo scope |
| skillweave | skillweave-docs, skillweave-internal-docs, skillweave-ops, skillweave-sdk | `GH_REPO` unset at repo scope |

This is the principal finding of the retrofitted audit: the two-variable
contract reports, honestly, that 60 of 78 canonical repositories are not yet
configured to mirror, even though they all carry a mirror workflow. The propose
tool's view of the same corpus ("proposed, paired") is a plan, not the
as-measured state; the audit and the propose tool are not describing the same
moment, and that disagreement is stated here rather than smoothed over.

## Class 3 — the two forges disagree on a name

None at measurement time. The former class-3 specimen `langevc/txt-humanizer`
(Forgejo `txt-humanizer` vs GitHub `txtHumanizer`) is now class 5: it carries no
`GH_REPO`, so the workflow refuses on the unset half before it ever gets to a
name comparison. The class-3 drift machinery (`_slug` fold over spelling
variants) remains in the audit and is exercised by `--selftest`, but no live
repository currently reaches it, because reaching it requires a `GH_REPO` that
double-matches yet names a spelling-variant.

## Class 4 — deliberately not workflow-mirrored (policy)

Basis (measured, not a hardcoded list): no `.forgejo/workflows/mirror.yml` on
the canonical default branch at measurement time, and — where applicable — the
name carries the `*-planning` / `*-internal-docs` marker the rollout records as
"always private / never public on GitHub". The veeona org's GitHub side is
entirely private by design, which is why the whole org reads class 4.

| canonical org | repo | basis |
|---------------|------|-------|
| langevc | lvc-planning | no mirror workflow; `*-planning` marker |
| capacium | capacium-planning | no mirror workflow; `*-planning` marker |
| capacium | capacium-install-policy | no mirror workflow measured |
| elementeer | elementeer-specs | no mirror workflow measured |
| skillweave | skillweave-planning | no mirror workflow; `*-planning` marker |
| skillweave | skillweave-test-lab | no mirror workflow measured |
| skillweave | skillweave-profiles | no mirror workflow measured |
| skillweave | skillweave-packs-pro | no mirror workflow measured |
| veeona | veeona, veeona-agents, veeona-docs, veeona-internal-docs, veeona-media, veeona-ops, veeona-planning, veeona-test-lab | org has no canonical mirror workflow; host GitHub org (`Veeona-AI`) is private by design |

Note the internal-docs / planning-named repositories that DO carry a mirror
workflow are class 5 (e.g. across elementeer / fusionaize); they carry a
workflow but no `GH_REPO`. Class 4 applies only where no mirror workflow exists
— not to every `*-internal-docs` name.

## Class 2 — resolves but unreachable

None. No repository currently carries a `GH_REPO` that double-matches and yet
names a destination GitHub does not carry. The pre-012 "composes but unreachable"
case no longer exists under the two-variable contract, because there is no
compose: an unset `GH_REPO` is class 5 (refused), not class 2 (composed but
absent).

## Full row table

Produced verbatim by the audit tool against the live forges; destination is the
verbatim `GH_REPO` (class 1) or the refusal reason (class 5).

| org | repo | class | private | destination / note |
|-----|------|-------|---------|--------------------|
| capacium | capacium | 1 | true | Capacium/capacium |
| capacium | capacium-action-publish | 5 | false | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | capacium-action-validate | 5 | false | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | capacium-admin | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | capacium-app | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | capacium-bridge | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | capacium-bridge-tests | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | capacium-crawler | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | capacium-docs | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | capacium-exchange | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | capacium-github-app | 5 | false | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | capacium-install-policy | 4 | true | no mirror workflow measured |
| capacium | capacium-internal-docs | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | capacium-marketplace-tui | 5 | false | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | capacium-mcp | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | capacium-models | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | capacium-ops | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | capacium-planning | 4 | true | no workflow; policy marker |
| capacium | capacium-spec | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | capacium-test-lab | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | capacium-web | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | claude-code-capacium | 5 | false | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | jetbrains-capacium | 5 | false | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| capacium | vscode-capacium | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| elementeer | elementeer | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| elementeer | elementeer-addon-voxel | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| elementeer | elementeer-bridge | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| elementeer | elementeer-docs | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| elementeer | elementeer-internal-docs | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| elementeer | elementeer-mcp | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| elementeer | elementeer-ops | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| elementeer | elementeer-planning | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| elementeer | elementeer-pro | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| elementeer | elementeer-specs | 4 | false | no mirror workflow measured |
| fusionaize | faifabric | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| fusionaize | faigate | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| fusionaize | faigrid | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| fusionaize | failens | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| fusionaize | faiops-browser | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| fusionaize | faiops-cli | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| fusionaize | faios | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| fusionaize | faisignal | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| fusionaize | faistudio | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| fusionaize | fusionaize-docs | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| fusionaize | fusionaize-internal-docs | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| fusionaize | fusionaize-metadata | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| fusionaize | fusionaize-metadata-public | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| fusionaize | fusionaize-ops | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| fusionaize | fusionaize-planning | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| fusionaize | fusionaize-project-template | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| fusionaize | fusionaize-sdk | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| fusionaize | grok-api-hook | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| langevc | agent-test-env | 5 | false | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| langevc | envctl | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| langevc | lvc-docs | 5 | false | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| langevc | lvc-internal-docs | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| langevc | lvc-ops | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| langevc | lvc-planning | 4 | true | no workflow; policy marker |
| langevc | ops-engine | 5 | false | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| langevc | txt-humanizer | 5 | false | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| langevc | wp-test-env | 5 | false | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| skillweave | skillweave | 1 | true | LangeVC/skillweave |
| skillweave | skillweave-docs | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| skillweave | skillweave-internal-docs | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| skillweave | skillweave-ops | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| skillweave | skillweave-packs-pro | 4 | true | no mirror workflow measured |
| skillweave | skillweave-planning | 4 | true | no workflow; policy marker |
| skillweave | skillweave-profiles | 4 | true | no mirror workflow measured |
| skillweave | skillweave-sdk | 5 | true | GH_REPO unset at repo scope (workflow refuses before any GitHub request) |
| skillweave | skillweave-test-lab | 4 | false | no mirror workflow measured |
| veeona | veeona | 4 | true | org without canonical mirror workflow; github side private by design |
| veeona | veeona-agents | 4 | true | same |
| veeona | veeona-docs | 4 | true | same |
| veeona | veeona-internal-docs | 4 | true | same |
| veeona | veeona-media | 4 | true | same |
| veeona | veeona-ops | 4 | true | same |
| veeona | veeona-planning | 4 | true | same |
| veeona | veeona-test-lab | 4 | true | same |

## Re-running

```
python3 scripts/mirror-destination-audit.py \
  --orgs capacium,elementeer,fusionaize,langevc,skillweave,veeona \
  [--out PATH]
```

The GitHub mirror owner for each org is read live from the org-scope
`GH_REPO_OWNER`, so no `--map` is required (a `--map` entry is an explicit
override only). Credentials via `FORGEJO_TOKEN`+`GITHUB_TOKEN`. `--selftest`
runs the planted proofs with no forge read: the class-3 spelling-drift
mismatch, the verbatim full-destination classification (no recomposition), the
case-sensitive double-match refusal (class 5, decided without any GitHub
request) on `elementeer`/`fusionAIze`/`Veeona-AI`, and the unset-half refusals.
A read failure (401/403/5xx, network) is reported UNMEASURED and is never
confused with an absent workflow or an unset variable (HTTP 404 is the
genuine-absence outcome); a repo the token cannot read is UNMEASURED, never
silently class 4, and a wholly denied forge is a clean error exit, not a
mid-report crash.
