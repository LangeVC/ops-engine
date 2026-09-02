# mirror-visibility-error-rates

Measurement of the mirror visibility gate (a glob over path names, applied to a
push *diff*) in **both** error directions: the paths it wrongly blocks (false
positive) and the intellectual property it lets reach a public mirror anyway
(false negative). This document records the method and the counts. It changes
no rule.

## The rule under measurement

The running gate on every repository that carries a mirror workflow blocks a
push when the pushed diff touches any of these path names:

```
.skillweave/**
strategy.md, **/strategy.md
prd*.md, **/prd*.md
prd*.json, **/prd*.json
*.contract*, **/*.contract*
*contract*.md, **/*contract*.md
*proposal*.md, **/*proposal*.md
*.proposal*, **/*.proposal*
```

Two properties do most of the work. The glob keys on the **path name**, not on
content, so a shipped schema named `prd.schema.json` is treated like a planning
document, and a real PRD shipped as `forgejo-first.json` is invisible to it. And
the gate tests a **push diff** (deltas), so any matching file already resident in
the public default branch, a non-default branch, or a release tag is never
re-evaluated: exposure is a property of *state*, the gate only sees *change*.

## Method

For each of the five organisations the mirrored repositories were split by
direction and measured separately.

- **False positives (Direction 1).** On each canonical (Forgejo) repository that
  both deploys the gate and mirrors to a public GitHub target, all reachable
  history was walked (`git log --all --diff-filter=A --name-only`) for every
  distinct **added** path name matching the glob. Each was inspected and
  classified: genuine planning substrate (the rule's intended target) or
  product (a shipped schema/asset, a code interface doc, a test fixture whose
  purpose is only to mirror a real artifact's shape). Product-class matches are
  false positives.
- **False negatives (Direction 2).** The **current state** of each public GitHub
  mirror was fetched (default branch, every non-default branch, every tag) and
  scanned for any file whose path matches the glob. Each match was content-probed
  and classified: real intellectual property (a PRD, a strategy document, a
  contract, internal planning/dogfooding state) or product/no-IP. Real IP exposed
  in a public state is a false negative. Names, classes and counts only — no
  content is reproduced here.
- Repositories that could not be fetched or that the API reports but serves no
  objects are listed as UNMEASURED, never assumed clean.

Top-line findings were independently re-verified against the live GitHub API
(this document's author probed the decisive public-default-branch exposures
directly); totals below distinguish field-verified examples from the wider
scanned tally. The measurement was read-only throughout.

### Corrigendum to the brief's known instance

The brief cites `tests/fixtures/prd-schema/forgejo-first.json` as the canonical
false negative and asserts "the glob matches the path". On the literal rule above
it does **not**: the leaf does not start `prd`, and no glob arm matches a `.json`
under `tests/`. Direct API check shows the file is real planning JSON (project,
version, tasks, sequence) and is **present on `main` of the public mirror right
now** (≈97 kB). The exposure is real and severe, but the mechanism is the inverse
of the brief's gloss: the rule was evaded by *filename*, not beaten by state. A
real PRD was parked in a `tests/fixtures/` lane that the gate treats as safe, and
then, once resident, was never swept. Both readings agree it is a false negative;
they disagree on why, which matters for OME-008.

## Direction 1 — false positives (rule blocks no-IP product)

| org | public gate-bearers walked | distinct blocked additions | genuine-IP | product (FP) |
|---|---|---|---|---|
| langevc | 0 — no canonical langevc repo deploys the gate | 0 | 0 | **0** |
| skillweave | 4 (skillweave, -docs, -profiles, -sdk) | 115 (all in `skillweave`) | 108 | **7** |
| capacium | 8 (capacium + 7 product repos) | 17 | 16 | **1** |
| elementeer | 1 (`elementeer-ops`) | 1 | 1 | **0** |
| fusionaize | 5 (faigate, faigrid, -docs, -sdk, -metadata-public) | 10 | 6 | **4** |
| **total** | | **143** | **131** | **12** |

Product-class false positives (each named):

- **skillweave (7):** `skills/skillweave-blueprint/assets/prd.schema.json` (the
  known 2026-09-02 release blocker — a shipped product schema);
  `skills/skillweave-blueprint/references/prd-template.md` (shipped blank
  template); `examples/prd-based-sequence-example.md` (synthetic worked example);
  `test/prd-sample.json` and `test/prd-sample.md` (test samples);
  `tests/fixtures/…/prd.md` and `…/prd.json` (neutral placeholder fixtures).
- **capacium (1):** `capacium-marketplace-tui/docs/ipc-contract.md` — a product
  IPC/JSON-RPC interface spec; the word "contract" in its filename is the only
  reason the glob catches it.
- **fusionaize (4):** `faigrid/docs/reference/executor-contract.md` (shipped
  plugin interface spec); `fusionaize-sdk/docs/contracts/TYPEMODEL_V1.md`
  (shipped type-model doc); `fusionaize-sdk/packages/sdk-contracts/CHANGELOG.md`
  and `…/README.md` (npm package docs). All three sdk hits are the substring
  `contract` in a shipped package/dir name.

The false-positive cost is a **name-vs-content** collision. A legitimate product
push touching any of these is refused (or demands a `release_override`), and the
schema instance shows the refusal can halt a release when it lands on a merge into
the mirrored default branch.

## Direction 2 — false negatives (real IP public anyway)

The decisive result. Distinct real-IP files present in a current public mirror
state (default `main`, a non-default branch, or a release tag). Counts here are
distinct files, not per-ref duplicates — near-identical planning files that rode
out on many release tags are counted once per file present on at least one ref.

| org | public repo(s) with real-IP exposure | distinct real-IP files in public state | tier |
|---|---|---|---|
| langevc | `LangeVC/agent-test-env`, `LangeVC/txtHumanizer` | 3–5 | 2 on public `main` |
| skillweave | `LangeVC/skillweave` (+ `-docs` low) | ~93 | large tag/non-default-branch body + 1 real PRD on `main` |
| capacium | capacium product repos | 8 (tag tier) + low-IP `agents.md` on many `main`s | tags + branches |
| elementeer | `elementeer`, `elementeer-mcp` | real PRD + contract set | on `main` + via `elementeer-mcp` contract on all release tags |
| fusionaize | `faigate`, `faigrid`, -docs, -sdk | 2 genuine PRDs + low-IP pointers | on `main` + release tags |

Concrete field-verified real-IP exposures on a public **default branch right
now**:

- **skillweave → `LangeVC/skillweave`** `tests/fixtures/prd-schema/forgejo-first.json` — a real 97 kB PRD on `main`. *(API-verified this session.)*
- **langevc → `LangeVC/agent-test-env`** `prd.json` (≈27 kB) and `prd.md` (≈19 kB) on `main`. *(API-verified.)*
- **langevc → `LangeVC/txtHumanizer`** `.skillweave/prd-txthumanizer-v0.0.2.json` on `main`. *(API-verified.)*
- **elementeer → `elementeer/elementeer-mcp`** `docs/blueprints/intent-wizard-contract.md` on `main` and on every release tag — a genuine product-contract/decision document. *(API-verified.)*
- **fusionaize → `FusionAIze/faigate`** `docs/blueprints/model-updater/prd.md`/`prd.json` on `main`. *(API-verified.)*

Wider scanned tally (distinct genuine-IP blocked files present on ≥1 current ref
of the public mirror):

- **skillweave product mirror (`LangeVC/skillweave`):** ~93 distinct genuine
  planning files — initiative/v0.x PRD sets and a council strategy — present on
  roughly half a dozen non-default branches and the `v0.8.3`…`v1.5.0` release-tag
  range; most of the volume sits on old **release tags**, exactly the state the
  delta gate never revisits.
- **capacium:** the `prd/` PRD set (8 files: prd + complexity/risk/verification/
  assignment/parallel/workflow planning) is on release tags `v0.3.0`–`v0.6.0` and a
  pull-ref, not on `main`. A byte-identical `.skillweave/agents.md` (internal
  planning pointer, referencing the private planning repo and local clone paths)
  sits on the public `main` of `capacium` and ten product repos.
- **elementeer:** `.skillweave/agents.md` + `.skillweave/tracking-log/ELM-MUT/*`
  on `main`, several branches and tags of `elementeer`; plus the
  `intent-wizard-contract.md` above. Note: elementeer's only gate-bearer
  (`elementeer-ops`) mirrors to a **private** target — no public elementeer repo
  is actually gate-covered.
- **fusionaize:** genuine PRDs only on faigate (above); `.skillweave/agents.md`
  planning pointers on faigrid/docs/sdk `main` and on metadata-public's
  non-default branch.

## Totals

| axis | per org | total |
|---|---|---|
| false positive (distinct product blocked paths) | langevc 0, skillweave 7, capacium 1, elementeer 0, fusionaize 4 | **12** |
| false negative (distinct real-IP files public, default+other state) | langevc 3–5, skillweave ~93, capacium ~8, elementeer ~1–3, fusionaize ~6 | **≈110+** |

Whatever the exact tag-rollup of the skillweave body, the asymmetry is not in
doubt: the false-negative side outnumbers the false-positive side by roughly an
order of magnitude, and — worse — the false negatives include **real PRDs and a
product contract on the public default branch today**, which is precisely the
exposure the gate exists to prevent. The gate is simultaneously too broad (it
names "contract"/"prd"/"strategy" words, so it trips on shipped product docs)
and structurally blind (it judges diffs, so the content that is already public —
old release tags, pre-existing branches, `.skillweave` that rode to `main` before
the gate, and anything whose filename dodges the glob) is never revisited).

UNMEASURED: `LangeVC/skillweave-profiles` — GitHub lists the repository but serves
no objects (empty clone; treated neither clean nor counted).

Three qualitative findings sharpen the counts:

1. **The oldest content is the least protected.** The real PRDs on the public
   mirrors were added *before* each org's gate deployed; the gate added later
   never sees them because it only evaluates new deltas. State that predates the
   policy is the whole false-negative body.
2. **Tag/branch exposure is invisible to a per-push gate.** Release tags carry
   the full planning subtree because the gate checked the *push* and the file was
   already there — the forced state-sync re-gate exists but has not been run
   retroactively across these histories.
3. **Coverage is uneven by org.** langevc and elementeer gate no public repo at
   all (elementeer's sole gate-bearer mirrors to a private target); capacium,
   fusionaize and skillweave gate their public product mirrors, yet the 
   pre-existing-state exposure above is measured *despite* the gate.

## What the numbers imply for OME-008

The next task rewrites the rule; these numbers bound what a rewrite must do.
Four implications are stated here without design:

- A path-name glob on *deltas* cannot be made correct by adding more name arms; it
  is fighting the wrong quantity. Pre-existing state — branches and **release
  tags**, not default-branch only — is where the real IP sits, and a delta gate
  will never reach it. OME-008 must address state existence, or the ~93-file /
  multi-tag body stays public no matter how the glob is tuned.
- Content beats names, and the false-positive list names the casualties of a naive
  fix: banning the *word* "contract"/"prd" also bans shipped schemas and interface
  specs. Pulling both error rates down requires a content or purpose distinction,
  not a longer blocklist.
- A definitive public-surface sweep (tags + non-default branches) is the missing
  primitive; the false-negative number is state nobody has swept, and it is ~10x
  the false-positive number.
- The five orgs are not symmetric — three gate all their public mirrors, two gate
  none — so redistribution of the same gate cannot move all five error rates
  equally; OME-008 first has to say *where* the gate is meant to run.
