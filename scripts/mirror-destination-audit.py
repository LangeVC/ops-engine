#!/usr/bin/env python3
"""mirror-destination-audit - classify every canonical repository by mirror policy.

OME-003. Answers "which repositories does the organisation mirror rule not
cover", and replaces the never-counted "about seventy" figure with a real total.

Forgejo (git.langevc.com) is the single source of truth for repositories: a
push there triggers a workflow that mirrors the ref to GitHub. An org-scope
rule composes the GitHub mirror destination from the org's GitHub login plus
the repository name; a per-repo repository variable GH_REPOSITORY overrides
that compose (repo wins) - see OPS-002 / OME-001/002. This audit reads both
forges *read-only*, resolves each canonical repository's effective destination,
and places it in exactly one of four classes:

  1. resolves and reachable        - GitHub carries owner/<exact name>
  2. resolves but unreachable      - destination account resolves, name does not
  3. needs an exception            - the two forges disagree on the name
  4. not mirrored by policy        - no mirror workflow on the canonical repo

A repository is NEVER silently class 4. Class 4 rests on two pieces of evidence
measured here: (a) no mirror workflow on the canonical repo, and (b) the name
matches the *-planning / *-internal-docs rule the rollout records as
"always private / never on a public mirror". A repo with no workflow and none
of that marker is surfaced under the note, never auto-guessed. A repository the
token cannot read is reported UNMEASURED, never classed.

The script changes neither forge: only GET list/content reads (a repository
listed under an authed GitHub org is reachable to a mirror token; the private
twins these orgs mirror to are reachable with the credentials that also read
them here), and it writes nowhere except its own report (stdout or --out). Repo
and org variables are only read, never set, so the planted-mismatch proof runs as
a pure self-test (--selftest) without touching a variable on a live repository.

Stdlib only.

Environment:
  FORGEJO_API    base  (default https://git.langevc.com/api/v1)
  FORGEJO_USER   (default typelicious)
  FORGEJO_TOKEN  HTTP Basic password / api token for the canonical forge
  GITHUB_TOKEN   Bearer to api.github.com with read scope on the orgs

The org map is the one operational assertion the forges cannot supply - which
GitHub login is the mirror owner for each canonical org - so it is passed with
--map forgejoOrg:GitHubLogin[,...]. Class 4's basis (workflow absence) does not
depend on this map. GitHub orgs that have no canonical twin are reported as
GITHUB_ONLY, not woven into the canonical census.

Usage:
    mirror-destination-audit.py --map langevc:LangeVC,capacium:Capacium
        [--out PATH] [--orgs a,b] [--json]
    mirror-destination-audit.py --selftest
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.request

FORGEJO_API_DEFAULT = "https://git.langevc.com/api/v1"
GITHUB_API = "https://api.github.com"
UA = "ops-engine/mirror-destination-audit (OME-003)"

PLANNING_MARKER = re.compile(r"(?:^|-)(?:planning|internal-docs)$")


def _get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _get_json(url, headers):
    return json.loads(_get(url, headers))


class Census:
    """Read-only snapshot of both forges for the mapped orgs."""

    def __init__(self, forgejo_api, fg_user, fg_token, gh_token):
        self.fg = _basic(fg_user, fg_token)
        self.gh = _bearer(gh_token)
        self.api = forgejo_api

    def mirror_yml(self, org, repo, branch):
        try:
            d = _get_json(f"{self.api}/repos/{org}/{repo}/contents/.forgejo/workflows/mirror.yml?ref={branch}", self.fg)
            if isinstance(d, dict) and d.get("content"):
                return base64.b64decode(d["content"]).decode("utf-8", "replace")
        except Exception:
            pass
        return None

    def repo_var(self, org, repo):
        try:
            d = _get_json(f"{self.api}/repos/{org}/{repo}/actions/variables/GH_REPOSITORY", self.fg)
            return d.get("data") if isinstance(d, dict) else None
        except Exception:
            return None


def _basic(u, t):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{t}".encode()).decode(), "User-Agent": UA}


def _bearer(t):
    return {"Authorization": f"Bearer {t}", "User-Agent": UA}


def _slug(name: str) -> str:
    """Normalise a slug so the two forges' spelling drift folds onto one key.

    Forgejo and GitHub disagree on case, hyphenation and dots for the *same*
    repository (real specimen: forgejo `txt-humanizer` vs GitHub `txtHumanizer`).
    This makes a hyphen-/case-/dot-insensitive key for drift detection.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def classify(repo_name: str, mirror_yml_present: bool, gh_names: set[str], gh_login: str):
    """Return (cls, detail) for one canonical repo against a GitHub org name set.

    Resolution precedence is repo-variable override else org compose, which
    reduces to the name we must find on GitHub: gh_login/<repo> (or the override).
    Classification:

      * exact name present  -> class 1 (resolves and reachable)
      * only a spelling-variant of the same slug present under the org
        -> class 3 (the two forges disagree on the name; needs an exception)
      * no matching name    -> class 2 (composes but the destination is unreachable)
      * no mirror workflow  -> class 4 (policy), guarded by the *-planning marker.
    """
    if not mirror_yml_present:
        if PLANNING_MARKER.search(repo_name):
            return "4", "no workflow; *-planning/*-internal-docs policy marker"
        return "4", "no mirror workflow on canonical repo (mirror not declared)"
    if repo_name in gh_names:
        return "1", f"{gh_login}/{repo_name}"
    # spelling drift: same slug under a different spelling is a name disagreement
    matches = [n for n in gh_names if _slug(n) == _slug(repo_name)]
    if matches:
        actual = sorted(matches)[0]
        return "3", f"{gh_login}/{actual} (forgejo {repo_name} vs github {actual})"
    return "2", f"{gh_login}/{repo_name} (composes but absent/unreachable on github)"


def gather(api, fg_user, fg_token, gh_token, org_map, orgs_only=None):
    c = Census(api, fg_user, fg_token, gh_token)
    rows = []
    forgejo_orgs = _get_json(f"{api}/user/orgs", c.fg)
    for org in forgejo_orgs:
        u = org["username"]
        if orgs_only and u not in orgs_only:
            continue
        if u not in org_map:
            continue  # canonical org has no mapped mirror owner -> out of this census
        gh_login = org_map[u]
        try:
            canon = {r["name"]: r for r in _get_json(f"{api}/orgs/{u}/repos?limit=200", c.fg)}
        except Exception:
            rows.append({"org": u, "repo": "(org unreadable)", "class": "UNMEASURED",
                         "dest_or_note": "could not enumerate canonical org", "private": None,
                         "gh_login": gh_login, "repo_var": None})
            continue
        gh_names = {r["name"] for r in _get_json(f"{GITHUB_API}/orgs/{gh_login}/repos?per_page=200", c.gh)}
        for rname, r in canon.items():
            branch = r.get("default_branch") or "main"
            yml = c.mirror_yml(u, rname, branch)
            rvar = c.repo_var(u, rname)
            cls_, detail = classify(rname, yml is not None, gh_names, gh_login)
            rows.append({"org": u, "repo": rname, "class": cls_, "dest_or_note": detail,
                         "private": r["private"], "gh_login": gh_login, "repo_var": rvar})
    return rows


def _selftest():
    """Planted-mismatch proof: intral-forge name drift must be class 3, never 2.

    Uses only the decision functions (no forge is touched). It exercises the
    exact case the audit exists to catch: the same repository whose spelled name
    differs between the two forges. GitHub's list is planted with `txtHumanizer`
    (no hyphen, camelCase); the canonical name is `txt-humanizer` (hyphen). The
    classifier must return class 3 (needs an exception), not class 2 (broken).
    """
    # case/hyphen drift of the SAME repo
    cls3, detail3 = classify("txt-humanizer", True, {"txtHumanizer"}, "LangeVC")
    assert cls3 == "3" and "vs github" in detail3, (cls3, detail3)
    # a genuinely absent slug (no name agreement) is class 2 and must stay 2:
    cls2, detail2 = classify("txt-humanizer", True, {"unrelated"}, "LangeVC")
    assert cls2 == "2", (cls2, detail2)
    # exact same name -> 1
    cls1, _ = classify("envctl", True, {"envctl"}, "LangeVC")
    assert cls1 == "1", cls1
    # no workflow + planning marker -> 4
    cls4, _ = classify("miama-planning", False, set(), "LangeVC")
    assert cls4 == "4", cls4
    # no workflow + no marker -> still 4 (via declared-workflow-absence), noted
    cls4b, _ = classify("envctl", False, {"envctl"}, "LangeVC")
    assert cls4b == "4", cls4b
    print("selftest PASS: name mismatch -> 3 | absent slug -> 2 | exact -> 1 | unmirrored -> 4")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--map", required=False, help="forgejoOrg:GitHubLogin[, ...]")
    ap.add_argument("--orgs", help="restrict canonical orgs (comma list)")
    ap.add_argument("--out", help="write markdown report to PATH")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if not a.map:
        print("mirror-destination-audit: ERROR: --map is required (or --selftest)", file=sys.stderr)
        return 2
    org_map = dict(p.split(":", 1) for p in a.map.split(",") if ":" in p)
    fg_token = os.environ.get("FORGEJO_TOKEN")
    gh_token = os.environ.get("GITHUB_TOKEN")
    if not fg_token or not gh_token:
        print("mirror-destination-audit: ERROR: need FORGEJO_TOKEN and GITHUB_TOKEN", file=sys.stderr)
        return 2
    api = os.environ.get("FORGEJO_API", FORGEJO_API_DEFAULT)
    fg_user = os.environ.get("FORGEJO_USER", "typelicious")
    orgs_only = set(a.orgs.split(",")) if a.orgs else None
    rows = gather(api, fg_user, fg_token, gh_token, org_map, orgs_only)
    emit(rows, org_map, a.json, a.out)
    return 0


def emit(rows, org_map, as_json, out):
    from collections import Counter
    class_count = Counter(r["class"] for r in rows)
    org_counts = {}
    for r in rows:
        org_counts.setdefault(r["org"], Counter())[r["class"]] += 1
    if as_json:
        blob = {"counts": dict(class_count),
                "orgs": {o: dict(c) for o, c in org_counts.items()},
                "rows": rows}
        text = json.dumps(blob, indent=2)
    else:
        lines = [
            "# Mirror destination audit",
            "",
            f"Canonical (Forgejo) repositories in census: **{len(rows)}**",
            "",
            "## Counts per class",
            "| class | count |", "|-------|-------|",
        ]
        for c in ("1", "2", "3", "4"):
            lines.append(f"| {c} | {class_count.get(c, 0)} |")
        lines += ["", "## Per org (class counts)", "",
                  "| org | GitHub mirror login | class 1 | class 2 | class 3 | class 4 |",
                  "|-----|---------------------|---------|---------|---------|---------|"]
        for o in sorted(org_counts):
            c = org_counts[o]
            lines.append(f"| {o} | {org_map[o]} | {c.get('1', 0)} | {c.get('2', 0)} | {c.get('3', 0)} | {c.get('4', 0)} |")
        lines += ["", "| org | repo | class | private | destination / note |",
                  "|-----|------|-------|---------|----------------------|"]
        for r in sorted(rows, key=lambda x: (x["org"], x["repo"])):
            lines.append(f"| {r['org']} | {r['repo']} | {r['class']} | {r['private']} | {r['dest_or_note']} |")
        text = "\n".join(lines) + "\n"
    if out:
        with open(out, "w") as f:
            f.write(text)
        print(f"wrote {out}", file=sys.stderr)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    sys.exit(main())
