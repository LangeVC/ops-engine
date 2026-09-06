# Release body republish — 2026-09-05 (REL-014)

Republish of the contaminated public release bodies on the GitHub mirror of
`ops-engine`, executed in lane REL-014 (sequence lvc-250-2026-09-05).

## Remote and branch

The assigned worktree `wt-lvc250-ops-engine-14-republish-bodies` exposes two remotes:

```
origin  git@git.langevc.com:langevc/ops-engine.git (fetch)   <- CANONICAL forge
origin  git@git.langevc.com:langevc/ops-engine.git (push)
github  https://github.com/LangeVC/ops-engine.git (fetch)    <- public mirror
github  https://github.com/LangeVC/ops-engine.git (push)
```

All git refs for this lane are pushed only to the canonical `origin`. The
public-mirror `github` remote is never pushed to. The GitHub **release bodies**
are edited through the GitHub REST API (`gh`), never through the git `github`
remote.

- Lane branch: `chore/REL-014-republish-release-bodies`
- Base SHA: `de302bc` (`langevc/ops-engine main`)
- Pushed on the canonical forge; the re-fetched tip SHA is named in the REL-014
  lane verdict, not embedded here.

## Read / write surfaces

- WRITE (repository): `docs/release-retrofit-2026-09-05.md` (this file).
- WRITE (release object, external): the bodies of three GitHub releases of
  `LangeVC/ops-engine` — v3.1.0, v2.1.0, v0.1.0.
- READ (repository): `CHANGELOG.md`, `docs/release-backfill-2026-09-05.md`,
  `scripts/release_notes_audience_gate.py`.
- MUST NOT TOUCH: `src/`, `.forgejo/`, `pyproject.toml`, `CHANGELOG.md`.

This document is what reads and records the release-state change; every state
claim below was produced by a live `gh` API call in this session, not by
reading a file or trusting a prior verdict.

## Measured starting point — not inherited, enumerated

An earlier revision of this task claimed seven contaminated bodies. Measured on
2026-09-06 that is FALSE. REL-014 does not inherit REL-005's backfill list — a
release may have surfaced since. The surface was enumerated fresh by calling
the GitHub releases API, not by inheriting any prior list:

```
$ gh release list --repo LangeVC/ops-engine
v0.1.0 v2.0.0 v2.1.0 v2.1.1 v2.1.2 v2.1.3 v2.1.4 v3.0.0 v3.1.0 v3.2.0   (ten releases)
```

Enumerating this way is how **v0.1.0** surfaces at all: REL-005's backfill
record never mentions v0.1.0 as a contaminated body (that lane only corrected
titles, and v0.1.0's title already conformed), yet its body is contaminated.
Enumerate, do not inherit a list.

## Forgejo (canonical) measurement

The Forgejo side must be measured before anything is touched. The Forgejo API
at `https://git.langevc.com/api/v1` is unauthenticated-reachable only as a
signed-in user; every call returns HTTP 403 ("Only signed in user is allowed
to call APIs"). This session holds no Forgejo API token — none is present in
the `gh` credential store (hosts.yml lists only `github.com`), none in `tea`
(no hosts registered), and none in the environment. Without a token the
Forgejo release bodies cannot be fetched or measured.

The Forgejo release bodies are therefore **not measured and not changed in this
lane**. This is reported under the "Not closed" section, not papered over. Every
release-body change proved below is on GitHub, which is the surface the recorded
contamination lives on.

---

## Acceptance criterion 1 — every changed body equals the current CHANGELOG entry

The three contaminated GitHub bodies were each replaced by the body the release
workflow would extract today for that version: the version's section of the
current `CHANGELOG.md`, sliced from the heading to the next `##` heading,
`.strip()`ed — the identical slice `forgejo-release.yml` produces when it builds
a release body (and the identical definition an existing test asserts for v3.1.0).

### What is contaminated and was changed (before, fetched live)

| tag | title (pre) | published_at | body before (fetched) | contamination |
|-----|-------------|--------------|-----------------------|---------------|
| v3.1.0 | Ops Engine v3.1.0 | 2026-09-05T17:04:54Z | 4689 B, ticket refs | `OME-011`,
 `OME-012`, `OME-013`, `OME-014`, `OME-015`; private org names (`elementeer`,
 `capacium`, `fusionaize`, `veeona`, `skillweave`, `lvc-ops`, `LangeVC`) |
| v2.1.0 | Ops Engine v2.1.0 | 2026-06-04T18:56:14Z | 1432 B, ticket refs | `CORE-006`,
 `CORE-007`; private org name `Capacium` |
| v0.1.0 | Ops Engine v0.1.0 | 2026-05-20T23:10:43Z | 1887 B, promotional | private org
 names `LangeVC` / `langevc` (the body was brand prose, not the changelog entry at all) |

Ticket-ref contamination red-proven against the FETCHED bodies, not a local
file, using REL-011's gate with the organisation's tracker prefixes exactly as
`.forgejo/workflows/forgejo-release.yml` declares them (`LVC OME CORE LNF DST
REL CFG FFR`) — see criterion 3 below for the runs.

Private-org-name contamination shown by a sweep of the fetched bodies for the
organisation's own identities (see criterion 3).

Each contaminated body was replaced (`gh release edit --notes-file`, body only —
no `--title`, no tag change) with the current CHANGELOG entry for that version.

### After (fetched live) — equality with the current CHANGELOG entry

| tag | body bytes (after) | fetched body == current CHANGELOG entry? |
|-----|--------------------|------------------------------------------|
| v3.1.0 | 4376 | TRUE |
| v2.1.0 | 1246 | TRUE |
| v0.1.0 | 227 | TRUE |

Equality was compared content-wise with trailing whitespace ignored — the only
difference is the single trailing newline GitHub's API appends on write; this is
the same "content equal ignoring trailing newline" norm REL-005 recorded for the
v3.1.0 create. Each comparison was made from the live `gh release view` body
against the section sliced from the committed `CHANGELOG.md`, equal byte-for-byte
after that normalisation.

### Left alone, with the reason

| tag | body outcome | reason |
|-----|--------------|--------|
| v3.2.0 | left alone | freshly authored external-reader entry (REL-012/016); the fetched body already equals the current `3.2.0` CHANGELOG entry |
| v2.0.0 | left alone | genuine release content; fetched body carries no internal ticket ref and no private org name; not among the three measured contaminated |
| v2.1.1 | left alone | genuine changelog content; fetched body clean |
| v2.1.2 | left alone | genuine changelog content; fetched body clean |
| v2.1.3 | left alone | REL-005 gap-statement body; states the changelog gap, reconstructs no history; fetched body clean |
| v2.1.4 | left alone | REL-005 gap-statement body (as v2.1.3); fetched body clean |
| v3.0.0 | left alone | fetched body is the 25-byte title echo `Ops Engine v3.0.0 Release`; per the task it is reported, not acted on (see below) |

Criterion 1 met on GitHub. The Forgejo side is unmeasured (no token) — see
"Not closed".

---

## Acceptance criterion 2 — tag, title and publication date unchanged for every touched release

Titles still match `^Ops Engine v[0-9]+\.[0-9]+\.[0-9]+$`.

Every touched release was edited for body only. Before/after read live and compared:

| tag | tag before/after | published_at before/after | title before/after |
|-----|------------------|---------------------------|--------------------|
| v3.1.0 | v3.1.0 / v3.1.0 | 2026-09-05T17:04:54Z / unchanged | `Ops Engine v3.1.0` / unchanged |
| v2.1.0 | v2.1.0 / v2.1.0 | 2026-06-04T18:56:14Z / unchanged | `Ops Engine v2.1.0` / unchanged |
| v0.1.0 | v0.1.0 / v0.1.0 | 2026-05-20T23:10:43Z / unchanged | `Ops Engine v0.1.0` / unchanged |

Full title pattern over all ten releases after (fetched live):

```
BEFORE (all ten) title conforms: True
AFTER  (all ten) title conforms: True
```

Criterion 2 met.

---

## Acceptance criterion 3 — no body carries an internal ticket ref or a private org name afterwards

Proven by running REL-011's gate **against the FETCHED bodies**, never against
the local `CHANGELOG.md`. Each body was read back from the live releases API
(`gh release view --json body`) into a file and fed to
`scripts/release_notes_audience_gate.py` with the organisation's tracker
prefixes (`LVC OME CORE LNF DST REL CFG FFR`) — the same invocation the release
workflow makes.

REL-011's gate checks the ticket-reference clause; it is org-agnostic and ships
no forbid-file, so the private-org-name clause is shown separately by a sweep of
the fetched bodies for the organisation's own identities. Both runs are shown.

### Gate (ticket-reference clause) against the FETCHED bodies

Before (red — the three to be changed fail):

```
v3.1.0 exit=1  ReleaseNotesAudienceError: OME-011 x2, OME-012, OME-013, OME-014, OME-015
v2.1.0 exit=1  ReleaseNotesAudienceError: CORE-006, CORE-007
v0.1.0 exit=0  (no ticket-shaped ref under the org prefixes)
```

After (green — every fetched body):

```
v0.1.0 exit=0  PASS
v2.0.0 exit=0  PASS
v2.1.0 exit=0  PASS
v2.1.1 exit=0  PASS
v2.1.2 exit=0  PASS
v2.1.3 exit=0  PASS
v2.1.4 exit=0  PASS
v3.0.0 exit=0  PASS
v3.1.0 exit=0  PASS
v3.2.0 exit=0  PASS
AGGREGATE gate over all ten fetched after-bodies: rc=0  (all PASS)
```

### Private-org-name sweep over the FETCHED bodies

The organisation's own identities (proper nouns used as deployment/org anchors,
the same set that appeared in the contaminated bodies): `elementeer`, `capacium`,
`fusionaize`, `veeona`, `skillweave`, `lvc-ops`, `LangeVC`, `langevc`.

Before (contaminated):

```
v3.1.0  elementeer x1, capacium x3, fusionaize x2, veeona x2,
        skillweave x4, lvc-ops x1, LangeVC x5, langevc x5
v2.1.0  capacium x5
v0.1.0  LangeVC x2, langevc x2
```

After (republished):

```
v3.1.0  NONE
v2.1.0  NONE
v0.1.0  NONE
```

Criterion 3 met for the bodies that carry no private org name or ticket ref.

### The v3.0.0 25-byte finding — reported, not acted on

v3.0.0's fetched body is exactly 25 bytes: `Ops Engine v3.0.0 Release` — the
title echoed as the body, a stub rather than a changelog entry or a substantive
note. Reported here as the task requires; deliberately NOT acted on. It is not
one of the three contaminated bodies (it carries no internal ticket ref and no
private org name — gate passes, org sweep is NONE), and it is not the changelog
entry for its version. It is a separate thin-body finding for a later decision,
outside "republish the contaminated bodies."

---

## Not closed — named plainly

- **Forgejo (canonical) side unmeasured and unchanged.** This lane holds no
  Forgejo API token (gh hosts: only `github.com`; tea: no hosts; environment:
  none) and `https://git.langevc.com/api/v1` refuses unauthenticated calls
  (HTTP 403 "Only signed in user is allowed to call APIs"). The Forgejo release
  bodies could therefore not be fetched, so the claim of criterion 1 ("on BOTH
  forges every changed body equals the current CHANGELOG entry") is proved only
  for GitHub, where the recorded contamination lives; the Forgejo comparison is
  reported rather than fabricated. If the same old pre-REL-013 changelog text
  that produced the GitHub contamination also produced the Forgejo bodies for
  these old tags, the Forgejo bodies may be contaminated too; that is
  unverifiable here and would be the residual for a token-bearing run.
- **v3.0.0's 25-byte body** (`Ops Engine v3.0.0 Release`) is reported above and
  left alone per the task.
