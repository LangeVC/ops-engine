# Mirror destination inventory — which repositories the organisation rule does not cover

Task: OME-003 audit. Count the repositories on both forges and classify every
canonical (Forgejo) repository by how the organisation mirror-destination rule
treats it. Corrects the never-counted "about seventy" figure.

Measured: 2026-09-03, by reading both forges read-only (Forgejo
`git.langevc.com` canonical + GitHub mirror orgs) with an admin/owner token.
Tool: `scripts/mirror-destination-audit.py` in this repository. The script
changes neither forge and writes only its own report.

Reference decisions this audit builds on, not re-derives:
- Repeated repo- and org-scope variable injection is live and repo-scope wins
  (runs 38384 / 38386 / 38407); it is a platform rule on this Forgejo instance,
  so it holds for every organisation.
- OME-002 built the resolution contract (`resolve_destination` / `prove_destination`)
  on `feature/OME-002-resolution-contract`. It has no production caller yet and is
  in no released tag. This inventory describes the world **as it is today**, not
  as it will be once that contract is wired.

## Method

Forgejo is the single source of truth: a push there mirrors a ref to GitHub. For
each canonical repository the audit resolves the mirror destination the org rule
would produce — the org's GitHub login `owner/<repo>`, overridden by any repo
variable `GH_REPOSITORY` — and decides which of four classes it is in. GitHub
login-to-canonal-org (the compose owner) and the GitHub visible names are read
live, not hardcoded. `*-planning` / `*-internal-docs` is the only naming basis,
and it is applied only as *evidence* next to a measured absence of a mirror
workflow, never as a list that overrides measurement.

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
for the skillweave mirror, the GitHub login is `LangeVC` (skillweave has no
GitHub org of its own), so both the five langevc + four skillweave canonical
repos land under one host. Two GitHub orgs added (`Vamerli`: 3 repos) have no
canonical twin on this canonical forge and are not part of the mirror rule.

The "about seventy" estimate was not counting this population — no single number
fits it:
- the 49-repo drift table + 2 skipped in `ops-002-rollout-inventory.md` = 51
  (those were only the *already-workflowed* repos, not the census);
- the GitHub host count (≈88 incl. `.github`/non-mirror repos) is higher still.

The count that matters and that was previously never established is the canonical
side: **78**, of which the org rule actually drives **62** (class 1 + class 3);
the other **16** are deliberately not workflow-mirrored from the canonical forge.

## Counts per class

| class | meaning | count |
|-------|---------|-------|
| 1 | resolves and reachable | 61 |
| 2 | resolves but unreachable | 0 |
| 3 | needs an exception — the two forges disagree on the name | 1 |
| 4 | not mirrored by policy (deliberately) | 16 |

## Per org

| canonical org | GitHub mirror owner | class 1 | class 2 | class 3 | class 4 |
|---------------|---------------------|---------|---------|---------|---------|
| langevc | LangeVC | 7 | 0 | 1 | 1 |
| capacium | Capacium | 22 | 0 | 0 | 2 |
| elementeer | elementeer | 9 | 0 | 0 | 1 |
| fusionaize | fusionAIze | 18 | 0 | 0 | 0 |
| skillweave | LangeVC | 5 | 0 | 0 | 4 |
| veeona | Veeona-AI | 0 | 0 | 0 | 8 |

The skillweave figure in the brief (9 repos, 8 private / 1 public, all
main-branch mirror workflows carrying an inline GitHub destination) is **not a
microcosm of the whole**: the inline-destination generation is a predecessor to
the variable-derived rule and does not represent every org. The full census above
does.

## Known class 3 — the two forges disagree on a name

| canonical (forgejo) | class | github destination | note |
|---------------------|-------|--------------------|------|
| langevc/txt-humanizer | 3 | LangeVC/txtHumanizer | Forgejo spells it `txt-humanizer`; GitHub spells it `txtHumanizer`. The org rule composing `LangeVC/txt-humanizer` produces an unreachable name; the repo's own workflow hardcodes the corrected `LangeVC/txtHumanizer.git`. This is the exception case OME-002's repo-variable override exists for. |

Reported as class 3 (needs an exception), **not** class 2 (broken). Nearest other
candidate — an exact-name collision under the same org with a genuinely different
repository — was not present; no other two-forge name disagreement was found.

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
workflow are class 1 (e.g. across elementeer / fusionaize); they resolve to a
GitHub twin under the same name and, where the twin is private, feed a private
GitHub repo rather than surfacing publicly. Class 4 applies only where no mirror
workflow exists — not to every `*-internal-docs` name.

## Class 2 — resolves but unreachable

None. Every canonical repository that runs a mirror workflow resolves to a name
GitHub already carries. The single near-absent case (`txt-humanizer`) is a name
spelling disagreement and is class 3, not class 2.

## Full row table

Produced verbatim by the audit tool against the live forges; destination is the
`owner/name` the org rule resolves, or the GitHub spelling for class 3.

| org | repo | class | private | destination / note |
|-----|------|-------|---------|--------------------|
| langevc | agent-test-env | 1 | false | LangeVC/agent-test-env |
| langevc | envctl | 1 | true | LangeVC/envctl |
| langevc | lvc-docs | 1 | false | LangeVC/lvc-docs |
| langevc | lvc-internal-docs | 1 | true | LangeVC/lvc-internal-docs |
| langevc | lvc-ops | 1 | true | LangeVC/lvc-ops |
| langevc | lvc-planning | 4 | true | no workflow; policy marker |
| langevc | ops-engine | 1 | false | LangeVC/ops-engine |
| langevc | txt-humanizer | 3 | false | LangeVC/txtHumanizer (name differs) |
| langevc | wp-test-env | 1 | false | LangeVC/wp-test-env |
| capacium | capacium | 1 | true | Capacium/capacium |
| capacium | capacium-action-publish | 1 | false | Capacium/capacium-action-publish |
| capacium | capacium-action-validate | 1 | false | Capacium/capacium-action-validate |
| capacium | capacium-admin | 1 | true | Capacium/capacium-admin |
| capacium | capacium-app | 1 | true | Capacium/capacium-app |
| capacium | capacium-bridge | 1 | true | Capacium/capacium-bridge |
| capacium | capacium-bridge-tests | 1 | true | Capacium/capacium-bridge-tests |
| capacium | capacium-crawler | 1 | true | Capacium/capacium-crawler |
| capacium | capacium-docs | 1 | true | Capacium/capacium-docs |
| capacium | capacium-exchange | 1 | true | Capacium/capacium-exchange |
| capacium | capacium-github-app | 1 | false | Capacium/capacium-github-app |
| capacium | capacium-install-policy | 4 | true | no workflow measured |
| capacium | capacium-internal-docs | 1 | true | Capacium/capacium-internal-docs |
| capacium | capacium-marketplace-tui | 1 | false | Capacium/capacium-marketplace-tui |
| capacium | capacium-mcp | 1 | true | Capacium/capacium-mcp |
| capacium | capacium-models | 1 | true | Capacium/capacium-models |
| capacium | capacium-ops | 1 | true | Capacium/capacium-ops |
| capacium | capacium-planning | 4 | true | no workflow; policy marker |
| capacium | capacium-spec | 1 | true | Capacium/capacium-spec |
| capacium | capacium-test-lab | 1 | true | Capacium/capacium-test-lab |
| capacium | capacium-web | 1 | true | Capacium/capacium-web |
| capacium | claude-code-capacium | 1 | false | Capacium/claude-code-capacium |
| capacium | jetbrains-capacium | 1 | false | Capacium/jetbrains-capacium |
| capacium | vscode-capacium | 1 | true | Capacium/vscode-capacium |
| elementeer | elementeer | 1 | true | elementeer/elementeer |
| elementeer | elementeer-addon-voxel | 1 | true | elementeer/elementeer-addon-voxel |
| elementeer | elementeer-bridge | 1 | true | elementeer/elementeer-bridge |
| elementeer | elementeer-docs | 1 | true | elementeer/elementeer-docs |
| elementeer | elementeer-internal-docs | 1 | true | elementeer/elementeer-internal-docs |
| elementeer | elementeer-mcp | 1 | true | elementeer/elementeer-mcp |
| elementeer | elementeer-ops | 1 | true | elementeer/elementeer-ops |
| elementeer | elementeer-planning | 1 | true | elementeer/elementeer-planning |
| elementeer | elementeer-pro | 1 | true | elementeer/elementeer-pro |
| elementeer | elementeer-specs | 4 | false | no workflow measured |
| fusionaize | faifabric | 1 | true | fusionAIze/faifabric |
| fusionaize | faigate | 1 | true | fusionAIze/faigate |
| fusionaize | faigrid | 1 | true | fusionAIze/faigrid |
| fusionaize | failens | 1 | true | fusionAIze/failens |
| fusionaize | faiops-browser | 1 | true | fusionAIze/faiops-browser |
| fusionaize | faiops-cli | 1 | true | fusionAIze/faiops-cli |
| fusionaize | faios | 1 | true | fusionAIze/faios |
| fusionaize | faisignal | 1 | true | fusionAIze/faisignal |
| fusionaize | faistudio | 1 | true | fusionAIze/faistudio |
| fusionaize | fusionaize-docs | 1 | true | fusionAIze/fusionaize-docs |
| fusionaize | fusionaize-internal-docs | 1 | true | fusionAIze/fusionaize-internal-docs |
| fusionaize | fusionaize-metadata | 1 | true | fusionAIze/fusionaize-metadata |
| fusionaize | fusionaize-metadata-public | 1 | true | fusionAIze/fusionaize-metadata-public |
| fusionaize | fusionaize-ops | 1 | true | fusionAIze/fusionaize-ops |
| fusionaize | fusionaize-planning | 1 | true | fusionAIze/fusionaize-planning |
| fusionaize | fusionaize-project-template | 1 | true | fusionAIze/fusionaize-project-template |
| fusionaize | fusionaize-sdk | 1 | true | fusionAIze/fusionaize-sdk |
| fusionaize | grok-api-hook | 1 | true | fusionAIze/grok-api-hook |
| skillweave | skillweave | 1 | true | LangeVC/skillweave |
| skillweave | skillweave-docs | 1 | true | LangeVC/skillweave-docs |
| skillweave | skillweave-internal-docs | 1 | true | LangeVC/skillweave-internal-docs |
| skillweave | skillweave-ops | 1 | true | LangeVC/skillweave-ops |
| skillweave | skillweave-packs-pro | 4 | true | no workflow measured |
| skillweave | skillweave-planning | 4 | true | no workflow; policy marker |
| skillweave | skillweave-profiles | 4 | true | no workflow measured |
| skillweave | skillweave-sdk | 1 | true | LangeVC/skillweave-sdk |
| skillweave | skillweave-test-lab | 4 | false | no workflow measured |
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
  --map langevc:LangeVC,capacium:Capacium,elementeer:elementeer,\
         fusionaize:fusionAIze,skillweave:LangeVC,veeona:Veeona-AI \
  [--out PATH]
```

Credentials via `FORGEJO_TOKEN`+`GITHUB_TOKEN`. `--selftest` runs the planted
name-mismatch proof with no forge read.
