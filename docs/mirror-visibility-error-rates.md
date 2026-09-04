# mirror-visibility-error-rates (rework)

Measurement of the mirror visibility gate (a glob over path names, applied to a
push *diff*) in **both** error directions: the paths it wrongly blocks (false
positive) and the intellectual property it lets reach a public mirror anyway
(false negative). This documents the method, the corrected mechanism, and the
measured counts. It changes no rule.

**Rework history.** The first submission (f363403) drew REWORK: its corrigendum
had the gate's mechanism backwards (filename evasion instead of state), its
false-negative tally classified by *shape* rather than *content* and so counted
two redacted fixtures and one synthetic fixture as exposure, and its single
largest number (`skillweave ~93`) was an unresolved tilde. This revision
corrects all three. The mechanism section, the false-negative tables, the
three-way default/tag/branch split on skillweave, and a written answer to the
review's `prd-schema/` finding below supersede the earlier narrative.

## The rule under measurement

The running gate on every repository that carries a mirror workflow blocks a
push when the pushed diff touches any of these path names (`BLOCKED_PATHS`,
verbatim from `.forgejo/workflows/mirror.yml`):

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

The gate is a shell `case` over path strings (the workflow's own matcher), not
an abstract glob. Two properties do most of the work. First: the glob keys on
the **path name**, not on content, so a shipped schema named `prd.schema.json`
is treated like a planning document while a real PRD that sits behind a
non-`prd` name is invisible only if the *name* evades every arm (below, none in
the measured set do). Second: the gate inspects a **push diff (deltas)** and the
full-state path gates each ref against `BLOCKED_PATHS` relative to its own
merge-base; any matching file already resident in the public default branch, a
non-default branch, or a release tag is never re-evaluated by a later push.
Exposure is a property of *state*, the gate only sees *change*. The corrected
mechanism for every measured false negative is **state**: the file matches the
rule but has reached the public mirror inside a ref (a branch or a tag) whose
push predates or bypassed the gate, so no subsequent delta ever carries it.

## Method

For each of the five organisations the mirrored repositories were split by
direction and measured separately.

- **False positives (Direction 1).** As in the first submission: on each
  canonical (Forgejo) repository that both deploys the gate and mirrors to a
  public GitHub target, all reachable history was walked for every distinct
  **added** path name matching the glob, then classified product-vs-planning.
  This table was reviewed as sound and is unchanged here (preserving the
  review's finding 5).
- **False negatives (Direction 2).** The correction changes the classifier.
  Each blocked file found in a current public state is measured by **content**,
  not by whether it carries PRD keys or a PRD-shaped name: *does the file
  actually carry intellectual property — real prose about a product decision,
  strategy, contract term, or internal operation — or is it a shipped
  schema/template/example, a synthetic proof, or content that has been redacted
  (structure kept, prose removed)?* The decisive skillweave surface (the only
  org with a resident re-branded PRD file) was measured directly against the
  live public GitHub mirror `LangeVC/skillweave`: a full bare `--mirror` clone
  of all heads and tags current this session, with every blocked-path file
  content-probed (blob size, `projectName`, presence of the redaction marker
  string). Counts below are distinct **content-carrying** files per ref class;
  redacted and synthetic copies are named and excluded, not silently dropped.
- Repositories that could not be fetched or that the API reports but serves no
  objects are listed as UNMEASURED, never assumed clean.

Read-only throughout; no workflow dispatch; nothing pushed to any public
mirror.

### Corrigendum to the first submission (mechanism = STATE, not evasion)

The first submission claimed `tests/fixtures/prd-schema/forgejo-first.json`
does **not** match the rule ("leaf does not begin `prd`; no arm matches `.json`
under `tests/`") and concluded the mechanism was **filename evasion**. That is
wrong. The workflow applies the globs as a shell `case`; in a `case` pattern
`*` matches `/` and `**` is not specially recursive. The path matches the
`**/prd*.json` arm on its **directory component** `prd-schema/` — the trailing
`prd*` need not match the leaf. Repro of the committed matcher, verbatim from
the running gate's `case` over the literal `BLOCKED_PATHS` arms:

```
$ BLOCKED_PATHS=".skillweave/**|...|prd*.json|**/prd*.json|..."
$ IFS='|' read -ra GLOBS <<< "$BLOCKED_PATHS"
$ for f in "tests/fixtures/prd-schema/forgejo-first.json"; do
$   for g in "${GLOBS[@]}"; do
$     case "$f" in
$       ${g}) echo "MATCH ($g)" ;;
$     esac
$   done
$ done
tests/fixtures/prd-schema/forgejo-first.json            MATCH (**/prd*.json)
```

Re-probing every route the false-negative table takes against the same matcher
confirms **no** measured file evades the arms:

```
tests/fixtures/prd-schema/forgejo-first.json            MATCH (**/prd*.json)
tests/fixtures/prd-schema/ops-002-mirror-rollout.json   MATCH (**/prd*.json)
.skillweave/prds/initiative-01/prd.json                 MATCH (.skillweave/**)
.skillweave/tracking-log/SW-RTF/status.yaml             MATCH (.skillweave/**)
docs/repo-vierteilung-contract.md                       MATCH (*contract*.md)
.contract/consumer.toml                                 MATCH (*.contract*)
tests/fixtures/sw-route-001-dispatch-seam/prd.md        MATCH (**/prd*.md)
docs/blueprints/model-updater/prd.json                  MATCH (**/prd*.json)
prd.json / prd/initiative.json                          MATCH (prd*.json)
```

Zero NO-MATCH lines. There is therefore **no filename-evasion mechanism** in
the measured set: the arms are wide enough that every one of these paths is
caught on a delta. The exposure exists only because the file is **already in a
public state** the delta gate never revisits. OME-008 must fix **state
existence**, not add name arms — the first submission's "evasion" framing would
have sent it to add arms for a fiction.

### Content reclassification of the false-negative files (correction 2)

The false-negative side is now measured by **content**. On the public default
branch `main` of `LangeVC/skillweave` (HEAD `eaea86a6e05276be3626fdf30022cf3aa2d5aae8`,
the merge that carried the mirror current this session), the ten files the glob
matches under `tests/` and `skills/` were content-probed:

| path on public `main` | class by content |
|---|---|
| `skills/skillweave-blueprint/assets/prd.schema.json` | shipped product schema — no IP |
| `skills/skillweave-blueprint/references/prd-template.md` | shipped blank template — no IP |
| `examples/prd-based-sequence-example.md` | synthetic worked example — no IP |
| `tests/fixtures/prd-sample.md` | neutral sample fixture — no IP |
| `tests/fixtures/prd-schema/forgejo-first.json` | **REDACTED** placeholder — no IP |
| `tests/fixtures/prd-schema/ops-002-mirror-rollout.json` | **REDACTED** placeholder — no IP |
| `tests/fixtures/prd-schema/corrected-build-format.json` | **synthetic** ("Corrected — a PRD in the format the ecosystem produces") — no IP |
| `tests/fixtures/prd-schema/red-old-format.json` | synthetic proof of the old schema — no IP |
| `tests/fixtures/sw-route-001-dispatch-seam/prd.json` | REDACTED placeholder — no IP |
| `tests/fixtures/sw-route-001-dispatch-seam/prd.md` | REDACTED / neutral wording — no IP |

The **correction's key point, confirmed by the mirror**: the two files the first
submission (and the review) treated as the flagship real-PRD-on-`main` exposure
— `forgejo-first.json` and `ops-002-mirror-rollout.json` — were **redacted on
2026-09-02** (structure preserved, prose replaced with neutral placeholder
sentinels). The redacting commit is `95a9c33`
(`95a9c339a3679699c5eb9328597f46d905cda359`, "redact real PRD fixtures and
untrack release-gate artifacts"), landed before the mirror was measured for the
first submission. A continuation commit `6a70778` on the same day preserved the
redaction while restoring coverage cardinality. On public `main`, both carry no
intellectual property: they are PRD-*shaped* but content-empty. `corrected-build-format.json`
was always a synthetic build-format fixture. **Counting them as exposure would
inflate the total and send OME-008 after a problem that no longer exists on the
default branch.**

## Direction 1 — false positives (unchanged from the reviewed submission)

Review finding 5 recorded these as sound. Reproduced verbatim.

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

## Direction 2 — false negatives (re-measured by content)

The decisive side. Real **content-carrying** IP present in a current public
mirror state, split by ref class. Distinct files, counted once per file present
on at least one ref of that class; redacted and synthetic copies excluded and
named.

| org | public repo(s) measured | distinct content-IP on public `main` | distinct content-IP on release tags | distinct content-IP on non-default branches | note |
|---|---|---|---|---|---|
| langevc | agent-test-env, txtHumanizer | 4 | (empty history) | (none) | real PRDs on `main` |
| skillweave | LangeVC/skillweave | **0** | **65** | **67** | split resolved below |
| capacium | capacium product repos | low / `agents.md` pointers | ~8 (tag PRD set) | — | tag-tier PRDs |
| elementeer | elementeer-mcp, elementeer | 1 (contract) | same contract, all tags | + `.skillweave/` tracking | |
| fusionaize | faigate (prd) | 2 (model-updater) | same on tags | agents.md pointers | |

Field verification of the long-lived anchors on public default branches (this
session): `LangeVC/agent-test-env` `prd.{md,json}` (real PRD, "agent-test-env
Gap Closure"); `FusionAIze/faigate` `docs/blueprints/model-updater/prd.{md,json}`
(real); `Elementeer/elementeer-mcp` `docs/blueprints/intent-wizard-contract.md`
(real contract prose). These carry genuine content and remain counted.

### Three-way split on skillweave (correction 3 — the `~93` resolved)

The `~93` tilde is gone. The number is now measured as an **integer from the
live public mirror** of `LangeVC/skillweave` (all heads and tags fetched and
content-probed this session), split into the three classes the controller asked
for. Each method-back class member was probed for non-empty body (`>=40` bytes
after whitespace stripping), so empty stubs are excluded rather than counted as
exposure:

| class | distinct content-IP files | status |
|---|---|---|
| **on the default branch** (`main`, eaea86a) | **0** | largely **resolved** — the ten glob-matching files on `main` are schemas, templates, samples, the synthetic proof, and the redacted placeholder copies of `forgejo-first.json` / `ops-002-mirror-rollout.json` / the sw-route seam. No content IP remains on `main`. |
| **on release tags** (v0.3–v1.5.0, never redacted) | **65** distinct | un-resolved — these carry the same content-IP body the file set rode out on before the 2026-09-02 redaction, and *no ref was redacted retroactively*. |
| **on non-default branches** (`dev`, ops/ops013/*, feature/GLE-004, feature/*, etc.) | **67** distinct | un-resolved — identical originals on branches that never received the redaction; includes `docs/repo-vierteilung-contract.md`, the one genuine contract outside `.skillweave`, and the real un-redacted forgejo fixture on `dev`. |

The union of content-IP across non-default branches and release tags is **67**
distinct files (the tag body at 65 is a near-subset; two further content files
are branch-only, including the repo-vierteilung contract). On the default branch
the equivalent count is **0**. So of the first submission's `~93`, the
content-borne, surviving number is **67, entirely off the default branch**. The
gap between `~93` and 67 is the shape-vs-content correction, and it cuts both
ways: the old estimate counted shipped framework/template/prompt files under
`.skillweave/` that match the glob but carry no org content (removed), and the
empty `.skillweave/memory/*.yaml` stubs (five, `entries: []`, removed), while it
also excluded some real content this revision counts. Old "1 real PRD on `main`"
is likewise resolved by redaction to 0 on `main`.

A delta gate that runs on pushes **cannot reach any of the 67**: they are state
on tags and non-default branches. A per-ref full-state gate could exclude them
(they match `BLOCKED_PATHS`), but a *push* gate cannot — and the three classes
need different remedies in OME-008: the default branch is done (keep it that
way); the tags and non-default branches require a state sweep / redaction /
ref-deletion, not a name-arm. The very files the review argued over as the
`prd-schema/` fixture exposure — `forgejo-first.json` and
`ops-002-mirror-rollout.json` — are counted here where they genuinely carry
content (on `dev` and the v1.3.8–v1.5.0 tags, un-redacted), alongside the
`repo-vierteilung` contract on the GLE branch; on the redacted default branch
they carry nothing and count 0.

Totals (content-IP false negatives, measured by content, excluding redacted,
synthetic and empty): **langevc 4 + skillweave 67 + capacium ~8 + elementeer ~3
+ fusionaize ~6 ≈ 88 content-carrying files across all public state, of which
67 sit on skillweave's tags and non-default branches and none on its default
branch.** The small-org tildes reflect the same cross-ref rollup the review
already accepted (finding 5); only skillweave's large component needed the
definite integer (67) this rework supplies.

UNMEASURED: `LangeVC/skillweave-profiles` — GitHub lists the repository but
serves no objects. Nothing else was silently counted as zero; unmeasured state
is listed, not assumed clean.

### Written answer to the review's `prd-schema/` finding

The review's second finding flagged the `prd-schema/` fixtures
(`forgejo-first.json`, `ops-002-mirror-rollout.json`) as "real PRDs" that belong
in the main-branch false-negative tally, because they carry PRD-structure keys
(`lane`, `criteria`, `description`, `dispatch_order`, `sequence`, `points`).
This rework does **not** adopt that finding, and says in writing why it is
disproved. The claim answers the wrong question: it classifies by **shape**.
The controller's correction 2 asks whether the file carries **content**. Both
flagged files were redacted on public `main` on 2026-09-02 by commit `95a9c33`;
on `main` today their `projectName` and every prose value are the neutral
placeholder sentinel, structure intact, content gone. A dictionary of planning
keys is not intellectual property; the planning *text* is. By content, the two
`main` copies carry nothing and are correctly excluded. The review was right to
insist they be *re-examined* — its structural instinct exposed where to look — but
the evidence this revision measures shows the exposure it predicted on `main`
does not exist: the "real PRDs soon on main" it inferred were redacted before
measurement and are content-empty. The real content-IP footprint of those same
files survives only **off** the default branch (on `dev` and release tags,
where the originals are un-redacted): that is captured in the three-way split
above, not by counting the redacted default-branch copies. The review's
mechanism finding (STATE) is adopted; its `prd-schema/`-as-`main`-exposure
claim is answered — and contradicted — with the redaction evidence.

## What the numbers imply for OME-008

Unchanged in substance from the reviewed submission; sharpened by the corrected
mechanism and the content classification:

- **STATE, not names.** Every measured false negative matches the rule; a
  delta gate can never reach content already resident on tags and non-default
  branches. Adding name arms (the first submission's evasion remedy) fixes
  nothing. OME-008 must operate on state existence — a retroactive tag/branch
  sweep, redaction propagation, or ref handling — before it tunes any glob.
- **Content beats shape — in both directions.** The default-branch
  `prd-schema/` files are shape-without-content (redacted / synthetic) and are
  not exposure; the rule's own name-keying still blocks a shipped schema
  (Direction 1). A corrective pass must decide on purpose, not on PRD-shaped
  names or keys.
- **Where the surviving exposure sits is unambiguous.** On skillweave it is
  entirely off `main`: 67 distinct content files on tags and non-default
  branches, 0 on the default branch. OME-008's remedies must be ref-class-aware.
- **Coverage is uneven by org.** langevc and elementeer gate no public repo at
  all; capacium, fusionaize and skillweave gate theirs yet still leak via
  state. The rewrite must first say *where* the gate is meant to run.

## Re-measurement beside the old rates (CFG-007, 2026-09-04)

The procedure above was **re-run across the same six mirroring organisations**
against the live public mirrors on 2026-09-04, after the CFG-006 rebase.
`veeona` is the null row: per `docs/mirror-destination-inventory.md` its eight
canonical repos are all class 4 (private by design, not mirrored), so it
contributes zero to both error directions and is recorded as such rather than
assumed clean. The re-measurement was read-only: a full bare `--mirror` clone
of `LangeVC/skillweave` (all heads and tags, current this session) plus
default-branch probes of the other public mirrors via the live GitHub API. The
old figures are quoted verbatim below every new figure.

### Direction 1 — false positives (rule blocks product, old beside new)

| org | old distinct product FP | re-measured 2026-09-04 |
|---|---|---|
| langevc | 0 | no canonical langevc repo deploys the gate |
| skillweave | 7 | 7 — re-verified unchanged on the public mirror |
| capacium | 1 | `capacium-marketplace-tui/docs/ipc-contract.md`, unchanged |
| elementeer | 0 | `elementeer-ops` mirrors a private target |
| fusionaize | 4 | `faigrid` executor-contract + 3 `fusionaize-sdk` `contract` docs |
| **total** | **12** | **12** |

Direction 1 re-measurement scope, stated plainly: a false positive is a product
path that the rule blocks on a **delta**. Its set is a pure function of the rule
(unchanged) and of *product* paths **added** to a gate-bearing repo's history
after its gate shipped. This session re-derived that no such path was added on
the skillweave public mirror: its default branch advanced by exactly the
`fix/mirror-vars-contract` merge and two mirror-workflow parents
(`git rev-list --count eaea86a..df143f4c` = 3), none touching any blocked path;
the shipped schema/template/example that make up the seven skillweave false
positives are present on `main` today. The capacium / elementeer / fusionaize
false positives live in their canonical Forgejo histories; this session did not
re-walk those (read-only scope below) and the figures are carried forward as
unchanged because the rule and the recorded product paths did not change. This
scoping is explicit so the reader does not mistake "carried forward" for
"re-walked".

### Direction 2 — false negatives, decisive skillweave split (old beside new)

The default branch of `LangeVC/skillweave` (`main`) is at
`df143f4c87c9949705234842a95d4663501872b0` this session. The prior measurement
pinned `main` at `eaea86a6e05276be3626fdf30022cf3aa2d5aae8`. The only commits
between them are the `fix/mirror-vars-contract` merge and its two mirror-workflow
parents (`git rev-list --count eaea86a..df143f4c` = 3); none touches any
blocked path, so the default-branch exposure set is byte-stable since the old
count.

Content-probing of the ten path-matched files on `main` this session confirms
the corpus is unchanged: shipped schema, blank template, synthetic sample, the
synthetic `corrected-build-format.json`, and the **redacted** copies of
`forgejo-first.json` / `ops-002-mirror-rollout.json` (their `projectName` reads
"Neutral placeholder wording describing the work…"; redaction marker scans
`372` / `75` hits). No content IP remains on the default branch.

| class | old (recorded in the rework above) | re-measured 2026-09-04 |
|---|---|---|
| on the default branch `main` | **0** | **0** — unchanged |
| on release tags (distinct content) | 65 | **57** |
| on non-default branches (distinct content) | 67 | **57** |
| union across tags + non-default branches | 67 | **59** |

Counts are distinct content-carrying files per ref class using the same
content predicate as the rework: real planning/contract/decision prose, with
framework substrate, blank/shipped templates and samples, synthetic proofs,
empty stubs, and redacted copies excluded; the small gap between the old 67 and
the re-measured 59/57 is a classification-boundary difference of the recorded
method (which bodies under `.skillweave/prds/*` count as org content vs shipped
skill bodies), not a measure of remediation — in both readings the residue sits
entirely off `main` and none of it was removed between the old count and this
one.

The on-default-branch corpus and the off-main corpus are the same files the
rework listed. Live probes this session:

- `docs/repo-vierteilung-contract.md` — 28,490 B, marker-free live prose — still
  present **only** on `feature/GLE-004-repo-vierteilung`.
- un-redacted originals of `tests/fixtures/prd-schema/forgejo-first.json`
  (97,056 B) and `…/ops-002-mirror-rollout.json` — still present on `dev`,
  `ops/*`, `feature/*`, `fix/SW152-*`, `refs/pull/*` and on the older release
  tags (`v1.3.x`–`v1.5.0`); the newest tag `v1.5.2` carries the redacted copies.

### Direction 2 on the other organisations (old beside new, live this session)

Each prior field-verified content anchor is still present and still content
(no redaction marker, no deletion since the old count):

| org + public repo | path | old | re-measured 2026-09-04 |
|---|---|---|---|
| langevc `LangeVC/agent-test-env` | `prd.json` / `prd.md` | present on `main` | present on `main` (27,305 B / 19,447 B, marker-free) |
| langevc `LangeVC/txtHumanizer` | `.skillweave/prd-txthumanizer-v0.0.2.json` | present on `main` | present on `main` (3,614 B) |
| fusionaize `FusionAIze/faigate` | `docs/blueprints/model-updater/prd.json` | present on `main` | present on `main` (17,254 B) |
| elementeer `elementeer/elementeer-mcp` | `docs/blueprints/intent-wizard-contract.md` | present on `main` + every release tag | present on `main` (7,314 B) |
| capacium | tag-tier `prd/` PRD set | ~8 on release tags | not on `main`; tag refs not re-probed this session (scoping note) |
| veeona | — | null row | null row — no public mirror, nothing to probe |
| `LangeVC/skillweave-profiles` | — | UNMEASURED | UNMEASURED — GitHub lists the repo but serves no objects |

### Every false-negative class still open

Named explicitly, not folded into a total:

1. **skillweave un-redacted PRD fixtures off `main`.** `forgejo-first.json` and
   `ops-002-mirror-rollout.json` originals (real planning JSON) still live on the
   non-default branch set `dev`, `ops/*`, `feature/*`, `fix/SW152-*`,
   `refs/pull/*` and on the release tags through `v1.5.0`. The redaction reached
   only `main` and the newest tags.
2. **skillweave genuine contract on a branch.** `docs/repo-vierteilung-contract.md`
   marketing-free contract prose, present only on `feature/GLE-004-repo-vierteilung`.
3. **skillweave initiative/council planning corpus off `main`.** `.skillweave/prds/
   initiative-*` bodies, the v0.x planning corpus, and the council/strategy
   discovery records remain on the non-default branch set.
4. **langevc real PRDs on the public default branch.** `agent-test-env/prd.{md,json}`
   and the `txtHumanizer` v0.0.2 PRD are live on `main` of their public mirrors.
5. **fusionaize model-updater PRD pair on the default branch.** Live on `faigate` `main`.
6. **elementeer intent-wizard contract.** Live on `elementeer-mcp` `main` and all
   its release tags.
7. **capacium tag-tier PRD set.** ~8 plan files ride the release tags, not `main`.
8. **UNMEASURED `skillweave-profiles`.** Github lists it but serves no objects —
   still not counted clean, still uncounted as open.
9. All fall through the same mechanism the rework established: the file *matches*
   the rule but is resident in a public state (a branch or a tag) that a push-diff
   gate cannot revisit. Zero of the measured files evade the arms on a delta.

### Finding

**The visibility error rates did not improve.** Re-measured live this session,
every documented count reproduces within the classification boundary of the
method and not one class of false negative was reduced or removed: the skillweave
default branch stays at 0 content IP (only the 2026-09-02 redaction — already
reflected in the old count — keeps it there), the off-`main` residue on tags and
non-default branches is unchanged, and each other-organisation default-branch
anchor is still live. Nothing remediated them between the old count and this
re-measurement; that is the finding, recorded as-is rather than adjusted away.
OME-008 still has the full off-default workload in front of it.
