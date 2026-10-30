#!/usr/bin/env python3
"""mirror-destination-drift - report mirror destinations that have drifted since the audit.

OME-005. Detection only - this tool never writes to either forge: no variable
set, no ref pushed, no workflow dispatch. It reads both forges read-only and
writes nowhere except its own report (stdout or --out).

WHAT IT SHARES WITH THE AUDIT, AND WHY:
  The audit (OME-003, scripts/mirror-destination-audit.py) classifies every
  canonical repository into four classes over 78 repos in six orgs. Drift is the
  same question asked repeatedly, so this script REUSES that classification by
  importing the audit module and its Census/classify/gather paths directly - it
  does not build a second, divergent classifier. Two tools that classify the same
  world differently are worse than one. The only thing added here is the
  repetition plus one read the audit never took: the default-branch HEAD of the
  mirror twin, so a configured-and-resolvable destination that has quietly
  stopped receiving the newer default branch is caught. That is the exact
  failure of the two incidents - nothing was class 2 (no destination vanished),
  yet the mirror was silently stale for five integration waves, and again for
  v1.5.2 when GitHub main stopped tracking Forgejo's.

WHAT DRIFT MEANS HERE (three signals, each spoken in the audit's vocabulary):
  1. declaration gone missing    - repo whose mirror.yml once existed now reads
                                   class 4 "no mirror workflow".
  2. destination dead/missing    - repo that once mirrored to a reachable name now
                                   reads class 2 (resolves but unreachable), or a
                                   twin genuinely 404s on the GitHub side.
  3. silently stopped mirroring  - a repo whose destination still resolves AND is
                                   reachable (audit class 1, exact or via the
                                   GH_REPOSITORY override), but whose GitHub twin's
                                   default-branch HEAD is not the canonical Forgejo
                                   repo's default-branch HEAD. The "default branch
                                   behind its source" case.

Each drift row is tagged with the audit class the repository fell into (1-4 or
UNMEASURED), so a reader never has to translate. The report lists org,
repositories, the audit class, and a short human note.

A repository this tool cannot read is UNMEASURED, never "no drift". A failed read
(401/403/5xx, network, timeout) is surfaced as an UNMEASURED drift row; only a
genuine HTTP 404 (a genuinely-missing workflow on the canonical side, or a twin
that truly does not exist on the GitHub side) is treated as its real absence. The
confusion that forced the audit's rework - a failed read reported as an absence -
is not repeated here.

WHY A BASELINE IS THE DEFAULT STATE, BUT NOT REQUIRED FOR THE SILENT STOP:
  Drift is a delta, so a stored known-good snapshot makes "declaration went
  missing" and "destination went dead" precise. Provide one with
  --baseline (the audit's census JSON or this script's own --snapshot). The
  silent-stop case (signal 3) needs NO baseline at all: it compares each live
  class-1 repo's canonical default HEAD against its GitHub twin's live HEAD and
  fires the moment they diverge. A single healthy run seeds that with --snapshot,
  which becomes the next run's --baseline.

No writes: both forges are only ever GET. The only path this script writes is its
own report/snapshot, via --out / --snapshot, on the machine that runs it.

Environment (identical to the audit):
  FORGEJO_API    base  (default https://git.langevc.com/api/v1)
  FORGEJO_USER   (default typelicious)
  FORGEJO_TOKEN  HTTP Basic password / api token for the canonical forge
  GITHUB_TOKEN   Bearer to api.github.com with read scope on the orgs

Usage:
    mirror-destination-drift.py --map langevc:LangeVC,capacium:Capacium
        [--baseline PATH] [--snapshot PATH] [--out PATH]
        [--orgs a,b] [--max-drift N]
    mirror-destination-drift.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import Counter

# Reuse the audit's classifier, Census and reading helpers rather than building a
# divergent one (OME-005 mandate). Both modules live in scripts/; make it
# importable whether invoked in-place or via -m.
_SCRIPTS = os.path.dirname(os.path.abspath(__file__))

# The audit module lives as scripts/mirror-destination-audit.py (a dashed file
# name, not a legal import identifier). Load it from its path so drift reuses
# the exact same Census / classify / ReadFailure functions the audit ships.
def _load_audit():
    import importlib.util
    path = os.path.join(_SCRIPTS, "mirror-destination-audit.py")
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    spec = importlib.util.spec_from_file_location("mirror_destination_audit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audit = _load_audit()

UA = "ops-engine/mirror-destination-drift (OME-005)"

DEST_LIVE = "1"
DEST_DEAD = "2"
NEEDS_EXC = "3"
NOT_MIRRORED = "4"
UNMEASURED = "UNMEASURED"


# --------------------------------------------------------------------------- #
# Default-branch HEAD read (the ONE probe the audit never needed).
# --------------------------------------------------------------------------- #
class TwinProbeError(Exception):
    """A read of the GitHub twin's default-branch HEAD did not succeed."""


def twin_default_head(gh_token, owner, repo, default_branch):
    """GitHub default-branch HEAD sha of owner/repo.

    Returns (sha, None) when the twin exists and its default branch was read, or
    (None, detail) when it genuinely does not exist (404). Any other failure
    raises TwinProbeError so the caller marks the repo UNMEASURED, never healthy.
    """
    if not gh_token:
        raise TwinProbeError("no GITHUB_TOKEN")
    q = urllib.parse.quote(f"{owner}/{repo}")
    commit_url = f"https://api.github.com/repos/{q}/commits/{default_branch}"
    head = {"Authorization": f"Bearer {gh_token}", "User-Agent": UA}
    req = urllib.request.Request(commit_url, headers=head)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        return d["sha"], None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, "twin gone (404)"
        raise TwinProbeError(f"GET {commit_url} -> HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise TwinProbeError(f"GET {commit_url} -> {e.reason!r}") from e
    except TimeoutError as e:
        raise TwinProbeError(f"GET {commit_url} -> timeout") from e


def canonical_default_head(api, fg, org, repo, default_branch):
    """Canonical Forgejo default-branch HEAD sha, or None if the repo has none.

    Raises audit.ReadFailure on a genuine read failure (=> UNMEASURED upstream).
    """
    url = f"{api}/repos/{org}/{repo}/branches/{default_branch}"
    req = urllib.request.Request(url, headers=fg)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise audit.ReadFailure(f"GET {url} -> HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise audit.ReadFailure(f"GET {url} -> {e.reason!r}") from e
    except TimeoutError as e:
        raise audit.ReadFailure(f"GET {url} -> timeout") from e
    return (d.get("commit") or {}).get("id")


# The twin NAME for a class-1 repo is decided by the SAME repo-wins logic as the
# audit, and only the audit's decision vocabulary may select it. classify_live
# below delegates class + chosen twin-name to the shared classifier by feeding it
# the same inputs gather uses, so drift never narrows or widens the audit's world.
def resolved_twin_name(repo_var, repo_name, gh_login, gh_names):
    """Return the GitHub twin name for a repo-variable/compose resolution.

    Returns the twin repo name as the audit would resolve it: the GH_REPOSITORY
    override when set, else the org-composed repo name, only when that name is
    actually present on GitHub. Returns None when the name is not present (that is
    audit class 2 / dead destination territory), so a caller does not probe a
    twin that does not exist.
    """
    target = repo_var if repo_var else repo_name
    if target in gh_names:
        return target
    return None


# --------------------------------------------------------------------------- #
# Live gather - mirrors audit.gather.shape but adds the twin-HEAD probe for each
# class-1 repo. Read-only, UNMEASURED-correct.
# --------------------------------------------------------------------------- #
def collect(api, fg_user, fg_token, gh_token, org_map, orgs_only=None):
    """Live, read-only census with the twin-HEAD probe per class-1 repo.

    Returns a list of row dicts. Every row carries org, repo, class (audit), the
    audit's own per-row note, the resolved GitHub destination when live, and a
    "drift" note set only when this run found the repo drifted. Class 4 rows
    (no mirror workflow) are never drift on their own - that is policy. Class 2 is
    inherently drift (dead destination). Class 1 with a divergent twin HEAD is
    drift type 3. Every UNMEASURED read is surfaced, never "no drift".
    """
    c = audit.Census(api, fg_user, fg_token, gh_token)
    rows = []
    try:
        forgejo_orgs = audit._get_json(f"{api}/user/orgs", c.fg)
    except audit.ReadFailure as e:
        rows.append(_um("", "(bootstrap)", f"could not read canonical orgs: {e}", ""))
        return rows
    for org in forgejo_orgs:
        u = org["username"]
        if u not in org_map:
            continue
        if orgs_only and u not in orgs_only:
            continue
        gh_login = org_map[u]
        try:
            canon = {r["name"]: r for r in audit._get_json(f"{api}/orgs/{u}/repos?limit=200", c.fg)}
        except Exception as e:
            rows.append(_um(u, "(org unreadable)", f"could not enumerate canonical org: {e}", gh_login))
            continue
        gh_names = set()
        try:
            gh_repos = audit._get_json(f"{audit.GITHUB_API}/orgs/{gh_login}/repos?per_page=200", c.gh)
            gh_names = {r["name"] for r in gh_repos}
        except Exception as e:
            # GitHub side unreadable => every canonical repo of this org UNMEASURED.
            for rname, r in canon.items():
                rows.append(_um(u, rname, f"could not read GitHub org {gh_login}: {e}", gh_login))
            continue
        for rname, r in canon.items():
            branch = r.get("default_branch") or "main"
            try:
                yml = c.mirror_yml(u, rname, branch)
            except audit.ReadFailure as e:
                rows.append(_um(u, rname, f"could not read mirror workflow: {e}", gh_login))
                continue
            try:
                rvar = c.repo_var(u, rname)
            except audit.ReadFailure as e:
                rows.append(_um(u, rname, f"could not read GH_REPOSITORY variable: {e}", gh_login))
                continue
            try:
                cls_, note = audit.classify(rname, yml is not None, gh_names, gh_login, rvar)
            except Exception as e:  # classifier invariant broken => unmeasured
                rows.append(_um(u, rname, f"classification failed: {e}", gh_login))
                continue
            base = {"org": u, "repo": rname, "private": r["private"], "gh_login": gh_login,
                    "repo_var": rvar, "mirror_yml_present": yml is not None,
                    "default_branch": branch, "cls_note": note, "class": cls_}
            if cls_ == NOT_MIRRORED or cls_ == NEEDS_EXC:
                # class 4: not mirrored (policy) - no twin, no drift unless a
                # baseline says a workflow disappeared. class 3: name disagreement;
                # unresolved until given an exception, reported, not probed as a
                # silent stop.
                _finish_nonlive(rows, base, cls_, dest="")
                continue
            if cls_ == DEST_DEAD:
                # resolves but unreachable: the destination name is not on GitHub.
                _finish_nonlive(rows, base, cls_,
                                dest=f"{gh_login}/{rvar if rvar else rname}")
                continue
            # cls_ == DEST_LIVE -> probe canonical + twin HEAD parity (type 3).
            rows.append(_probe_live(c, api, gh_token, u, rname, gh_login, rvar,
                                    gh_names, base))
    return rows


def _um(org, repo, note, gh_login):
    return {"org": org, "repo": repo, "class": UNMEASURED, "dest": None,
            "private": None, "gh_login": gh_login, "drift": note,
            "cls_note": note}


def _finish_nonlive(rows, base, cls_, dest):
    rows.append({**base, "dest": dest, "drift": None})


def _probe_live(c, api, gh_token, org, repo, gh_login, rvar, gh_names, base):
    """Type-3 probe: is the reachable twin's default branch behind the canonical?"""
    branch = base["default_branch"]
    twin_name = resolved_twin_name(rvar, repo, gh_login, gh_names)
    dest = f"{gh_login}/{twin_name if twin_name else repo}"
    if twin_name is None:
        # Class says 1 but the twin name just stopped resolving on GitHub - a
        # borderline that is really a dead destination (class 2), never healthy.
        return {**base, "dest": dest,
                "drift": "class-1 flagged but twin name not on GitHub - dead destination"}
    try:
        canon_head = canonical_default_head(api, c.fg, org, repo, branch)
    except audit.ReadFailure as e:
        return {**base, "class": UNMEASURED, "dest": dest,
                "drift": f"could not read canonical default-branch head: {e}"}
    base["canon_head"] = canon_head
    try:
        twin_head, gone = twin_default_head(gh_token, gh_login, twin_name, branch)
    except TwinProbeError as e:
        return {**base, "class": UNMEASURED, "dest": dest,
                "drift": f"could not read twin default-branch head: {e}"}
    if gone is not None:
        # genuine 404 twin: destination is gone.
        return {**base, "class": DEST_DEAD, "dest": dest,
                "drift": "destination gone (twin 404) - class 2, not live"}
    base["twin_head"] = twin_head
    if canon_head is None:
        return {**base, "class": UNMEASURED, "dest": dest,
                "drift": "canonical repo has no default-branch HEAD (empty?)"}
    if canon_head != twin_head:
        base["drift"] = ("silently stopped mirroring: twin default-branch HEAD "
                         f"{twin_head[:10]} behind canonical {canon_head[:10]}")
        return base
    base["drift"] = None
    return base


# --------------------------------------------------------------------------- #
# Report emission - drift rows only, spoken in audit classes.
# --------------------------------------------------------------------------- #
def _is_drift(row):
    if row.get("drift"):
        return True
    # A class-2 dead destination is drift by its class alone (no note needed). A
    # class-1 row already got a drift note (or its none) from the probe; class 4
    # policy and class 3 exception are NOT themselves drift.
    if row.get("class") == DEST_DEAD:
        return True
    return False


def emit(rows, org_map, out, max_drift=None):
    drift_rows = [r for r in rows if _is_drift(r)]
    if max_drift is not None:
        drift_rows = drift_rows[:max_drift]
    counts = Counter(r["class"] for r in drift_rows)
    lines = [
        "# Mirror destination drift report",
        "",
        f"Canonical repositories in census: **{len(rows)}**",
        f"Drift rows: **{len(drift_rows)}**",
        "",
        "## Drift per audit class",
        "| class | meaning | drifted |", "|-------|---------|---------|",
    ]
    class_meaning = {
        DEST_LIVE: "resolves and reachable",
        DEST_DEAD: "resolves but destination unreachable/went missing or twin gone",
        NEEDS_EXC: "two forges disagree on the name (needs an exception)",
        NOT_MIRRORED: "not mirrored by policy (mirror.yml absent)",
        UNMEASURED: "could not be read - never 'no drift'",
    }
    for cl in (DEST_LIVE, DEST_DEAD, NEEDS_EXC, NOT_MIRRORED, UNMEASURED):
        lines.append(f"| {cl} | {class_meaning[cl]} | {counts.get(cl, 0)} |")
    lines += ["", "## Org | repo | audit class | destination | drift",
              "|-----|------|-------------|-------------|------", ""]
    for r in sorted(drift_rows, key=lambda x: (x["org"], x["repo"])):
        dest = r.get("dest") or ""
        dnote = r.get("drift") or r.get("cls_note") or ""
        lines.append(f"| {r['org']} | {r['repo']} | {r['class']} | {dest} | {dnote} |")
    text = "\n".join(lines) + "\n"
    if out:
        with open(out, "w") as f:
            f.write(text)
        sys.stderr.write(f"wrote {out}\n")
    else:
        sys.stdout.write(text)


def _dump_snapshot(rows, out):
    blob = {"rows": rows}
    text = json.dumps(blob, indent=2)
    if out:
        with open(out, "w") as f:
            f.write(text)
        sys.stderr.write(f"wrote snapshot {out}\n")
    else:
        sys.stdout.write(text)


def _diff_baseline(live_rows, baseline_map):
    """Tag live rows that regressed from the baseline toward drift.

    baseline_map: {(org, repo): class}. A repos is drilled when the baseline said
    mirror-declared+reachable (class 1) or needs-an-exception (3) and it is now
    class 4 (workflow gone - declaration lost) or class 2/UNMEASURED (destination
    lost). Class-3 baseline is drift too when a workflow disappeared. Nothing
    re-derived: only audit classes move.
    """
    for r in live_rows:
        bcl = baseline_map.get((r["org"], r["repo"]))
        if bcl is None:
            continue
        lcl = r.get("class")
        # A baseline-declared mirror (1, or 3 that had a workflow) that is now
        # class 4 has lost its declaration (signal 1); 1 -> 2/UNMEASURED lost the
        # destination (signal 2). Only when the drift note is not already set.
        if r.get("drift"):
            continue
        if bcl in (DEST_LIVE, NEEDS_EXC) and lcl in (NOT_MIRRORED, DEST_DEAD, UNMEASURED):
            r["drift"] = (f"baseline was class {bcl} (mirror declared) now {lcl}: "
                          "declaration/destination lost")
            r["cls_note"] = (r.get("cls_note") or "") + " [baseline regression]"
    return live_rows


def _redproof():
    """End-to-end red proof of the report-entry lifecycle, touching no forge.

    Acceptance 2 (OME-005): point a declaration at a repository that does not
    exist, confirm it produces a report entry (org + repo + audit class), restore
    it, confirm the entry clears. Because this tool is read-only by mandate and
    must not alter any real organisation's variable, the proof runs the shared
    classifier and the real report emitter over a throwaway declaration the
    operator owns entirely in memory - the same decision that the audit's own
    planted-mismatch proof (its acceptance 2, --selftest) used: no forge read,
    no variable written. The classifier under test is the very one the audit
    ships (mirror_destination_audit.classify), so nothing is fabricated that a
    live census would resolve differently.

    Scenario: a throwaway canonical repo 'ome005-ghost' under a throwaway org
    'langevc' owns a declaration (GH_REPOSITORY) that points at a GitHub repo
    'definitely-not-here', which does not exist. classify returns class 2 (dest
    dead/missing), which _is_drift reports. Then we 'restore' the declaration to
    point at a repo that exists and is in sync; classify returns class 1 with a
    matching twin HEAD, so no drift row is emitted. Both rounds print the same
    table emit() would, and the proof asserts the second no longer contains the
    dead-destination row.
    """
    gh_login, org = "LangeVC", "langevc"
    throwaway = "ome005-ghost"
    real_gh_names = {  # the mirror org carries these and NOT the throwaway twin
        "ops-engine", "envctl", "lvc-docs", "lvc-ops", "txtHumanizer",
    }

    def one(override_target, twin_head_equal):
        # Same census call both rounds: any unreadable = would be UNMEASURED, and
        # we assert it is never silently healthy. Override names the declaration's
        # destination; mirror.yml present => declared; twin equal => in sync.
        yml_present = True
        cls_, note = audit.classify(throwaway, yml_present, real_gh_names, gh_login,
                                    override_target)
        base = {"org": org, "repo": throwaway, "private": False, "gh_login": gh_login,
                "repo_var": override_target, "mirror_yml_present": True,
                "default_branch": "main", "cls_note": note, "class": cls_,
                "dest": f"{gh_login}/{override_target}"}
        if cls_ == DEST_LIVE:
            # type-3 probe substitute: twin reachable + HEAD choose drift on parity.
            if not twin_head_equal:
                base["drift"] = ("silently stopped mirroring: twin default-branch "
                                 "HEAD behind canonical")
            base["canon_head"] = base["twin_head"] = "a" if twin_head_equal else "b"
        else:
            base["drift"] = note  # class 2 already describes the dead destination
        return [base]

    # -- round one: declaration at a non-existent destination => report entry --
    dead_rows = one("definitely-not-here", twin_head_equal=True)
    dead_rep = render(dead_rows, {org: gh_login})
    dead_drift = [r for r in dead_rows if _is_drift(r)]
    assert any(r["class"] == "2" for r in dead_drift), dead_rows
    assert f"| {org} | {throwaway} | 2 |" in dead_rep, dead_rep

    # -- round two: restored (declared destination exists and is in sync) => none --
    live_rows = one("ops-engine", twin_head_equal=True)
    live_drift = [r for r in live_rows if _is_drift(r)]
    assert live_drift == [], live_rows
    live_rep = render(live_rows, {org: gh_login})
    assert "| 2 |" not in live_rep or throwaway not in live_rep, live_rep

    # Any UNMEASURED read stays surfaced; never silently "no drift".
    um = {"org": org, "repo": throwaway, "class": "UNMEASURED", "drift": "read failed",
          "dest": None, "private": True, "gh_login": gh_login, "cls_note": ""}
    assert _is_drift(um) is True
    print("redproof PASS: dead destination -> org+repo+class-2 report row; "
          "restored -> row clears; unmeasured stays surfaced")
    return 0


def render(rows, org_map):
    """Render rows through the same emit() markdown this script produces."""
    from io import StringIO
    old_out, old_err = sys.stdout, sys.stderr
    buf = StringIO()
    sys.stdout, sys.stderr = buf, buf
    try:
        emit(rows, org_map, out=None, max_drift=None)
        return buf.getvalue()
    finally:
        sys.stdout, sys.stderr = old_out, old_err


def _selftest():
    """Pure decision proof, no forge touched.

    Confirms the drift-vs-class boundaries and UNMEASURED-vs-absent semantics using
    fabricated rows. No network write happens.
    """
    # class-1 with divergent HEADs is the sole live silent-stop signal
    assert _is_drift({"class": "1", "drift": "silently stopped mirroring: twin behind"}) is True
    assert _is_drift({"class": "1", "drift": None}) is False
    assert _is_drift({"class": "2", "drift": None}) is True     # class 2 is drift
    assert _is_drift({"class": "4", "drift": None}) is False    # policy, not drift
    assert _is_drift({"class": "3", "drift": None}) is False    # exception, not drift
    assert _is_drift({"class": "UNMEASURED", "drift": "read failed"}) is True
    # baseline regression tagging
    live = [{"org": "langevc", "repo": "ops-engine", "class": "4", "drift": None}]
    out = _diff_baseline(live, {("langevc", "ops-engine"): "1"})
    assert "drift" in out[0] and "declaration/destination lost" in out[0]["drift"]
    live2 = [{"org": "langevc", "repo": "ops-engine", "class": "1", "drift": None}]
    out2 = _diff_baseline(live2, {("langevc", "ops-engine"): "1"})
    assert out2[0].get("drift") is None
    print("selftest PASS: silent-stop->drift | clean->none | class2->drift | "
          "policy3/4->none | unmeasured->drift | baseline regression->drift")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--map", help="forgejoOrg:GitHubLogin[, ...]")
    ap.add_argument("--orgs", help="restrict canonical orgs (comma list)")
    ap.add_argument("--baseline", help="baseline census JSON (audit --json or drift --snapshot)")
    ap.add_argument("--snapshot", help="write full live census as snapshot JSON (seed for next baseline)")
    ap.add_argument("--out", help="write markdown report to PATH (else stdout)")
    ap.add_argument("--max-drift", type=int, default=None)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--redproof", action="store_true",
                    help="red proof (acceptance 2): dead destination -> report row; "
                         "restored -> row clears; no forge touched")
    a = ap.parse_args()
    if a.redproof:
        return _redproof()
    if a.selftest:
        return _selftest()
    if not a.map:
        sys.stderr.write("mirror-destination-drift: ERROR: --map is required (or --selftest)\n")
        return 2
    org_map = dict(p.split(":", 1) for p in a.map.split(",") if ":" in p)
    fg_token = os.environ.get("FORGEJO_TOKEN")
    gh_token = os.environ.get("GITHUB_TOKEN")
    if not fg_token or not gh_token:
        sys.stderr.write("mirror-destination-drift: ERROR: need FORGEJO_TOKEN and "
                         "GITHUB_TOKEN (same env as the audit)\n")
        return 2
    api = os.environ.get("FORGEJO_API", audit.FORGEJO_API_DEFAULT)
    fg_user = os.environ.get("FORGEJO_USER", "typelicious")
    orgs_only = set(a.orgs.split(",")) if a.orgs else None
    try:
        rows = collect(api, fg_user, fg_token, gh_token, org_map, orgs_only)
    except audit.ReadFailure as e:
        sys.stderr.write(f"mirror-destination-drift: ERROR: could not read a forge "
                         f"({e}); nothing to classify\n")
        return 2
    if a.baseline:
        try:
            with open(a.baseline) as f:
                baseline_rows = json.load(f).get("rows", [])
        except Exception:
            baseline_rows = []
        base_map = {(b["org"], b["repo"]): b.get("class") for b in baseline_rows}
        rows = _diff_baseline(rows, base_map)
    if a.snapshot:
        _dump_snapshot(rows, a.snapshot)
        return 0
    emit(rows, org_map, a.out, a.max_drift)
    return 0


if __name__ == "__main__":
    sys.exit(main())
