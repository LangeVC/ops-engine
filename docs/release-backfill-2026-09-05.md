# Release backfill — 2026-09-05

Backfill of the missing GitHub releases and correction of release titles on the public
mirror, executed in lane REL-005 (sequence lvc-250-2026-09-05).

## Scope

- Repository: `LangeVC/ops-engine` (GitHub public mirror of the canonical Forgejo repo).
- This document records the GitHub release operations and the evidence that proves each
  acceptance criterion. Every state claim below was produced by a live `gh` API call in
  this session, not by reading a file or trusting a prior verdict.

## Remote and branch

The worktree `wt-lvc250-ops-engine-5-backfill` exposes two remotes:

```
origin  git@git.langevc.com:langevc/ops-engine.git (fetch)   <- CANONICAL forge
origin  git@git.langevc.com:langevc/ops-engine.git (push)
github  https://github.com/LangeVC/ops-engine.git (fetch)    <- read-only public mirror
github  https://github.com/LangeVC/ops-engine.git (push)
```

All git refs for this lane are pushed only to the canonical `origin`. The public-mirror
`github` remote is never pushed to. The GitHub **release objects** (create / title-edit)
are written through the GitHub REST API (`gh`), not through the git `github` remote.

- Lane branch: `chore/REL-005-backfill-github-releases`
- Base SHA: `018bb92` (`langevc/ops-engine main`)
- Pushed tip SHA on the canonical forge: `3baefaf2cf32c5a9b32f7a5949ab2075d45d8a34`
  (fetched again after the push and confirmed equal to the local branch head).

## Read / write surfaces

- WRITE (repository): `docs/release-backfill-2026-09-05.md` (this file).
- READ (repository): `CHANGELOG.md` (used verbatim for bodies / gap analysis).
- WRITE (release objects, external): GitHub releases and titles of `LangeVC/ops-engine`.
- MUST NOT TOUCH: `src/`, `.forgejo/`, `pyproject.toml`.

The document below is what reads and records the release-state change; each criterion cites
the live command and its output.

---

## Acceptance criterion 1 — v3.1.0 and v2.1.4 exist as GitHub releases

Before this session the two git tags existed on the mirror but no corresponding GitHub
release existed (live check, both MISSING):

```
$ gh release view v3.1.0 --repo LangeVC/ops-engine   -> MISSING
$ gh release view v2.1.4 --repo LangeVC/ops-engine   -> MISSING
$ gh release view v3.0.0 --repo LangeVC/ops-engine   -> exists (asset count 0, same as all mirror releases)
```

Both releases were created anchored to the existing, immutable git tags (so the underlying
commit is not moved):

```
$ gh release create v3.1.0 --repo LangeVC/ops-engine \
    --title "Ops Engine v3.1.0" \
    --notes-file <body 3.1.0 = CHANGELOG.md section "## 3.1.0">
$ gh release create v2.1.4 --repo LangeVC/ops-engine \
    --title "Ops Engine v2.1.4" \
    --notes-file <gap-statement 2.1.4>
```

- v3.1.0 body is the release's own CHANGELOG entry. The `## 3.1.0` section of `CHANGELOG.md`
  was extracted unchanged and used as `--notes-file`. Post-publish verification compared the
  released body to the CHANGELOG text: content identical, SHA-256 of both bodies equal
  (`82777fecccf332c2`), the only difference being one trailing blank line introduced by the
  capture. Real proof:
  ```
  $ awk '/^## 3\.1\.0/{p=1;next}/^## 3\.0\.0/{exit}p' CHANGELOG.md > body-3.1.0.md
  $ gh release view v3.1.0 --repo LangeVC/ops-engine --json body > released-body-3.1.0.txt
  $ python3 -c "(rstrip both; compare)"
  content-equal-ignoring-trailing-newline: True
  sha src 82777fecccf332c2
  sha rel 82777fecccf332c2
  ```
- v2.1.4 body states the changelog gap. `CHANGELOG.md` has no 2.1.4 heading (see criterion 4);
  the body names that gap instead of inventing content. Live proof:
  ```
  $ gh release view v2.1.4 --repo LangeVC/ops-engine --json name,tagName,body
  name  : Ops Engine v2.1.4
  tagName: v2.1.4
  body  : "Backfill of the v2.1.4 GitHub release. ... CHANGELOG.md contains
          **no entry for 2.1.4** ... 2.1.3 and 2.1.4 are both absent. ... without
          inventing a feature summary."
  ```

Criterion 1 met.

---

## Acceptance criterion 2 — all release titles match `^Ops Engine v[0-9]+\.[0-9]+\.[0-9]+$`

Title list, before this session (live `gh release list`):

```
Ops Engine v3.0.0   <- conforms
ops-engine v2.1.3   <- VIOLATES (lower-case product prefix)
ops-engine v2.1.2   <- VIOLATES
ops-engine v2.1.1   <- VIOLATES
ops-engine v2.1.0   <- VIOLATES
ops-engine v2.0.0   <- VIOLATES
Ops Engine v0.1.0   <- conforms
```

Red proof before any edit (the current set does not satisfy the pattern):

```
$ regex '^Ops Engine v[0-9]+\.[0-9]+\.[0-9]+$' over before.json
AFTER... total releases before: 7
VIOLATIONS: ['ops-engine v2.1.3','ops-engine v2.1.2','ops-engine v2.1.1','ops-engine v2.1.0','ops-engine v2.0.0']
violation count: 5
exit=1    (non-zero -> pattern not satisfied before the fix)
```

Correction (title only, per release, via edit preserving tag, published_at and body):

```
$ gh release edit v2.1.0 --repo LangeVC/ops-engine --title "Ops Engine v2.1.0"
$ gh release edit v2.1.1 --repo LangeVC/ops-engine --title "Ops Engine v2.1.1"
$ gh release edit v2.1.2 --repo LangeVC/ops-engine --title "Ops Engine v2.1.2"
$ gh release edit v2.1.3 --repo LangeVC/ops-engine --title "Ops Engine v2.1.3"
$ gh release edit v2.0.0 --repo LangeVC/ops-engine --title "Ops Engine v2.0.0"
```

Title list, after this session (live `gh release list`, 9 releases):

```
Ops Engine v3.1.0
Ops Engine v3.0.0
Ops Engine v2.1.4
Ops Engine v2.1.3
Ops Engine v2.1.2
Ops Engine v2.1.1
Ops Engine v2.1.0
Ops Engine v2.0.0
Ops Engine v0.1.0
```

Green proof after the fix:

```
$ regex '^Ops Engine v[0-9]+\.[0-9]+\.[0-9]+$' over after.json
AFTER total releases: 9
AFTER violations: []
AFTER conform: 9 / 9
exit=0    (pattern satisfied)
```

Criterion 2 met.

---

## Acceptance criterion 3 — tag and published_at unchanged for every touched release

Every touched release = the five edited for title (v2.1.0, v2.1.1, v2.1.2, v2.1.3, v2.0.0)
plus the two new creates (v3.1.0, v2.1.4). For the five title-edited releases the tag and
`published_at` must be unchanged, and for each we state whether the body was filled or left
alone and why:

| tag | tag before/after | published_at before/after | body outcome | why |
|-----|------------------|---------------------------|--------------|-----|
| v2.1.0 | v2.1.0 / v2.1.0 | 2026-06-04T18:56:14Z unchanged | left alone | body already carries the genuine `## [2.1.0]` CHANGELOG content; only the title prefix was wrong |
| v2.1.1 | v2.1.1 / v2.1.1 | 2026-06-04T19:04:42Z unchanged | left alone | body already carries the genuine `## [2.1.1]` CHANGELOG content |
| v2.1.2 | v2.1.2 / v2.1.2 | 2026-06-04T19:07:59Z unchanged | left alone | body already carries the genuine `## [2.1.2]` CHANGELOG content |
| v2.1.3 | v2.1.3 / v2.1.3 | 2026-06-07T14:13:34Z unchanged | left alone | body is the pre-existing terse stub "Release v2.1.3"; CHANGELOG has no 2.1.3 entry to source a body from, and rewriting would risk invention (criterion 4). The title was the only defect in scope |
| v2.0.0 | v2.0.0 / v2.0.0 | 2026-05-25T14:03:23Z unchanged | left alone | body already carries genuine release content; only the title prefix was wrong |

Live comparison (catches any accidental tag or published_at shift during the edits):

```
v2.1.0  tag=v2.1.0  publishedAt before=2026-06-04T18:56:14Z after=2026-06-04T18:56:14Z UNCHANGED=True
v2.1.1  tag=v2.1.1  publishedAt before=2026-06-04T19:04:42Z after=2026-06-04T19:04:42Z UNCHANGED=True
v2.1.2  tag=v2.1.2  publishedAt before=2026-06-04T19:07:59Z after=2026-06-04T19:07:59Z UNCHANGED=True
v2.1.3  tag=v2.1.3  publishedAt before=2026-06-07T14:13:34Z after=2026-06-07T14:13:34Z UNCHANGED=True
v2.0.0  tag=v2.0.0  publishedAt before=2026-05-25T14:03:23Z after=2026-05-25T14:03:23Z UNCHANGED=True
v3.0.0  (never touched) UNCHANGED=True
v0.1.0  (never touched) UNCHANGED=True
```

The two newly created releases (v3.1.0, v2.1.4) get their `published_at` set to creation
time (2026-09-05T17:04:54Z and 17:05:01Z) — there was no prior GitHub release to preserve;
their git tags pre-existed and were not moved. Bodies for the creates were filled (criterion
1); both were anchored to existing tags rather than moving a commit.

Criterion 3 met.

---

## Acceptance criterion 4 — no body is reconstructed from commit history

- v3.1.0 body: verbatim `CHANGELOG.md` `## 3.1.0` section. No commit-history text was written.
- v2.1.4 body: because `CHANGELOG.md` has no 2.1.4 entry, the body states the gap instead of
  inventing one. Live proof of the gap (real grep, exit non-zero = no heading):
  ```
  $ grep -nE '^\s*##?\s*\[?v?2\.1\.[34]' CHANGELOG.md ; echo "exit=$?"
  NO 2.1.3 OR 2.1.4 CHANGELOG HEADING (exit 1)
  ```
  The released body reads in full: "This tag exists ... but `CHANGELOG.md` contains **no
  entry for 2.1.4**. The changelog's versioned headings jump from `## [2.1.2] — 2026-06-04`
  to `## 2.2.0`; 2.1.3 and 2.1.4 are both absent. This body deliberately states the gap
  instead of reconstructing changes from commit history."
- Title-edited releases: no body was rewritten (bodies preserved, see criterion 3), so no
  reconstruction could occur there.

Criterion 4 met.

---

## Not closed / noted plainly

- The backfilled releases (v3.1.0, v2.1.4) were created without assets, matching the
  existing mirror behaviour where every GitHub release carries zero assets (verified live:
  v3.0.0 has asset count 0). Mirroring the four canonical assets to these two backfilled
  GitHub releases is out of the acceptance scope for REL-005 (existence + body + title) and
  is not gated by any criterion here; it would need the canonical asset bytes and is left
  as a functional / release-publishing concern, not a metadata backfill.
- v2.1.3's released body is the pre-existing terse stub "Release v2.1.3"; it neither
  reconstructs history nor states a changelog gap. `CHANGELOG.md` also lacks a 2.1.3 entry
  (same live grep above). Rewriting that body would require sourcing content the changelog
  does not provide and was deliberately avoided (criterion 4); the title (criterion 2) was
  the only defect corrected for v2.1.3.
