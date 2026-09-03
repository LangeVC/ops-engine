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

A repository is NEVER silently class 4. Class 4 rests on measured evidence of
workflow absence on the canonical repo (no `.forgejo/workflows/mirror.yml` at
the default branch). The *-planning / *-internal-docs marker only selects the
note text; it is not a second gate and never a name list that decides the class.
Absence and unreadability are kept distinct on every read: HTTP 404 means a
workflow (or variable) genuinely does not exist; any other failure (401/403/5xx,
network) means the read itself failed and is reported UNMEASURED - the one case a
tool must not confuse with "not mirrored". A denied forge is a clean diagnostic
exit, never a crash mid-report that fabricates or drops rows.

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
import urllib.error
import urllib.request

FORGEJO_API_DEFAULT = "https://git.langevc.com/api/v1"
GITHUB_API = "https://api.github.com"
UA = "ops-engine/mirror-destination-audit (OME-003)"

PLANNING_MARKER = re.compile(r"(?:^|-)(?:planning|internal-docs)$")


class ReadFailure(Exception):
    """A read the audit must perform *did not* succeed.

    Raised on a genuine transport/HTTP failure (network, 401/403/5xx), where the
    audit cannot distinguish "not there" from "could not look". Absence is a
    distinct outcome (HTTP 404) and is never reported as a failure, so a caller
    can tell a missing workflow or variable apart from an unreadable one.
    """


def _http_get(url, headers):
    """GET url, returning the parsed document, or raising ReadFailure.

    Only this function talks to the raw network. urllib transport and HTTP errors
    are folded into ReadFailure so every caller decides, from the exception alone,
    that a read failed vs. a resource being absent (404 is left to the callers).
    """
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        raise ReadFailure(f"GET {url} -> HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise ReadFailure(f"GET {url} -> {e.reason!r}") from e
    except TimeoutError as e:
        raise ReadFailure(f"GET {url} -> timeout") from e


def _get(url, headers):
    return _http_get(url, headers)[1]


def _get_json(url, headers):
    return json.loads(_get(url, headers))


class Census:
    """Read-only snapshot of both forges for the mapped orgs."""

    def __init__(self, forgejo_api, fg_user, fg_token, gh_token):
        self.fg = _basic(fg_user, fg_token)
        self.gh = _bearer(gh_token)
        self.api = forgejo_api

    def mirror_yml(self, org, repo, branch):
        """Return the mirror.yml text, None if absent, or raise ReadFailure.

        Three outcomes, never conflated:
          * text             - workflow present and read
          * None             - HTTP 404: genuinely no such workflow (policy path)
          * ReadFailure      - 403/401/5xx/network: could not read; caller must
                               report UNMEASURED, never infer an absence.
        """
        url = f"{self.api}/repos/{org}/{repo}/contents/.forgejo/workflows/mirror.yml?ref={branch}"
        req = urllib.request.Request(url, headers=self.fg)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                status = r.status
                body = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise ReadFailure(f"GET {url} -> HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise ReadFailure(f"GET {url} -> {e.reason!r}") from e
        except TimeoutError as e:
            raise ReadFailure(f"GET {url} -> timeout") from e
        if status != 200:
            raise ReadFailure(f"GET {url} -> HTTP {status}")
        d = json.loads(body)
        if isinstance(d, dict) and d.get("content"):
            return base64.b64decode(d["content"]).decode("utf-8", "replace")
        return None

    def repo_var(self, org, repo):
        """Return GH_REPOSITORY value, None if unset, or raise ReadFailure.

        HTTP 404 means the variable genuinely does not exist (compose applies and
        is the normal case); any other failure is a ReadFailure the caller must
        surface as UNMEASURED rather than silently treating as "no override".
        """
        url = f"{self.api}/repos/{org}/{repo}/actions/variables/GH_REPOSITORY"
        req = urllib.request.Request(url, headers=self.fg)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                status = r.status
                body = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise ReadFailure(f"GET {url} -> HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise ReadFailure(f"GET {url} -> {e.reason!r}") from e
        except TimeoutError as e:
            raise ReadFailure(f"GET {url} -> timeout") from e
        if status != 200:
            raise ReadFailure(f"GET {url} -> HTTP {status}")
        d = json.loads(body)
        return d.get("data") if isinstance(d, dict) else None


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


def classify(repo_name: str, mirror_yml_present: bool, gh_names: set[str], gh_login: str,
             override: str | None = None):
    """Return (cls, detail) for one canonical repo against a GitHub org name set.

    Resolution precedence is the measured repo-wins rule (OME-001/002): a repo
    variable `GH_REPOSITORY` overrides the org compose, so the effective name we
    must find on GitHub is the override when set, else the org-composed name
    `gh_login/repo_name`. An unset variable means the compose name applies.

    Classification of the effective destination:
      * effective name present  -> class 1 (resolves and reachable)
      * only a spelling-variant of that name under the org
        -> class 3 (the two forges disagree on the name; needs an exception)
      * no matching name        -> class 2 (composes but the destination is unreachable)
      * no mirror workflow      -> class 4 (policy), guarded by the *-planning marker.
    The repo-scope override is honoured before any name test, so a repo whose
    `GH_REPOSITORY` names a genuinely different GitHub repository (not a spelling
    variant of the compose) is class 1 when that name is present — not class 2.
    """
    via = bool(override)
    target = override if via else repo_name
    base = f"{gh_login}/{target}"
    if not mirror_yml_present:
        if PLANNING_MARKER.search(repo_name):
            return "4", "no workflow; *-planning/*-internal-docs policy marker"
        return "4", "no mirror workflow on canonical repo (mirror not declared)"
    if target in gh_names:
        if via:
            return "1", f"{base} (via GH_REPOSITORY variable)"
        return "1", base
    # name disagreement: a spelling-variant of the effective name present under the org.
    matches = [n for n in gh_names if _slug(n) == _slug(target)]
    if matches:
        actual = sorted(matches)[0]
        if via:
            return "3", f"{base} (GH_REPOSITORY {repo_name}->{target} vs github {actual})"
        return "3", f"{gh_login}/{actual} (forgejo {repo_name} vs github {actual})"
    if via:
        return "2", f"{base} (GH_REPOSITORY names a repo github does not carry/read)"
    return "2", f"{base} (composes but absent/unreachable on github)"


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
        except Exception as e:
            rows.append({"org": u, "repo": "(org unreadable)", "class": "UNMEASURED",
                         "dest_or_note": f"could not enumerate canonical org: {e}", "private": None,
                         "gh_login": gh_login, "repo_var": None})
            continue
        try:
            gh_names = {r["name"] for r in _get_json(f"{GITHUB_API}/orgs/{gh_login}/repos?per_page=200", c.gh)}
        except Exception as e:
            # GitHub side unreadable: we cannot judge reachability for this org,
            # so we must not class any of its repos. One UNMEASURED row per org,
            # matching the canonical-enumeration failure shape.
            for rname, r in canon.items():
                rows.append({"org": u, "repo": rname, "class": "UNMEASURED",
                             "dest_or_note": f"could not read GitHub org {gh_login}: {e}",
                             "private": r["private"], "gh_login": gh_login, "repo_var": None})
            continue
        for rname, r in canon.items():
            branch = r.get("default_branch") or "main"
            try:
                yml = c.mirror_yml(u, rname, branch)
            except ReadFailure as e:
                rows.append({"org": u, "repo": rname, "class": "UNMEASURED",
                             "dest_or_note": f"could not read mirror workflow: {e}",
                             "private": r["private"], "gh_login": gh_login, "repo_var": None})
                continue
            try:
                rvar = c.repo_var(u, rname)
            except ReadFailure as e:
                rows.append({"org": u, "repo": rname, "class": "UNMEASURED",
                             "dest_or_note": f"could not read GH_REPOSITORY variable: {e}",
                             "private": r["private"], "gh_login": gh_login, "repo_var": None})
                continue
            cls_, detail = classify(rname, yml is not None, gh_names, gh_login, rvar)
            rows.append({"org": u, "repo": rname, "class": cls_, "dest_or_note": detail,
                         "private": r["private"], "gh_login": gh_login, "repo_var": rvar})
    return rows


def _selftest():
    """Decision-function proofs (no forge is touched).

    Two families of proof, exercising the exact cases the auditor exists to catch:
      * two-forge name drift: the same repo whose spelled name differs between the
        forges (forgejo `txt-humanizer` vs github `txtHumanizer`) must be class 3,
        never class 2;
      * the repo-scope GH_REPOSITORY override (repo wins over org compose): a repo
        whose override names a genuinely *different* GitHub repo (not a spelling
        variant the slug check would ever derive) is class 1 when that name is
        present, and is NOT left at class 2 by a compose the override supersedes.
    """
    # --- two-forge name drift ---
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
    # --- GH_REPOSITORY override (repo wins), non-slug re-target ---
    # canonical compose `gh_login/host-core-lab` is deliberately absent; the repo's
    # override names `host-lab-main`, a genuinely different github repo slug check
    # would never derive from `host-core-lab`. Its presence must class it 1, not 2.
    cls_v1, d_v1 = classify("host-core-lab", True, {"host-lab-main"}, "migrate", "host-lab-main")
    assert cls_v1 == "1" and "GH_REPOSITORY" in d_v1, (cls_v1, d_v1)
    # the same repo WITHOUT the override (compose applies, target absent) is class 2:
    cls_v2, _ = classify("host-core-lab", True, {"host-lab-main"}, "migrate")
    assert cls_v2 == "2", cls_v2  # (override=None -> compose name absent)
    # override whose target is absent on github and not a variant -> class 2:
    cls_v3, _ = classify("host-core-lab", True, {"something-unrelated"}, "migrate", "gone-now")
    assert cls_v3 == "2", cls_v3
    # override to a spelling variant of its own target -> class 3, not 2:
    cls_v4, _ = classify("host-core-lab", True, {"HostCoreLab"}, "migrate", "host-core-lab")
    assert cls_v4 == "3", cls_v4
    print("selftest PASS: drift->3 | absent slug->2 | exact->1 | unmirrored->4 | "
          "override re-target->1 | override variant->3")
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
    try:
        rows = gather(api, fg_user, fg_token, gh_token, org_map, orgs_only)
    except ReadFailure as e:
        # Neither forge is reachable/authorised at the bootstrap read; there is no
        # repo set to class. Report cleanly rather than traceback, and never invent
        # a class for an org we could not even scope.
        print(f"mirror-destination-audit: ERROR: could not read a forge ({e}); "
              f"nothing to classify", file=sys.stderr)
        return 2
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
        unmeasured = class_count.get("UNMEASURED", 0)
        lines = [
            "# Mirror destination audit",
            "",
            f"Canonical (Forgejo) repositories in census: **{len(rows)}**"
            + (f" (of which {unmeasured} UNMEASURED)" if unmeasured else ""),
            "",
            "## Counts per class",
            "| class | count |", "|-------|-------|",
        ]
        for c in ("1", "2", "3", "4"):
            lines.append(f"| {c} | {class_count.get(c, 0)} |")
        if unmeasured:
            lines.append(f"| UNMEASURED | {unmeasured} |")
        # Per-org grid: add an UNMEASURED column only when some row carries it, so
        # the grid reconciles with the row table whether or not a read failed. In the
        # common all-readable case the six fixed columns render exactly as before.
        show_um_col = any(c.get("UNMEASURED", 0) for c in org_counts.values())
        if show_um_col:
            cols = ["org", "GitHub mirror login", "class 1", "class 2", "class 3", "class 4", "UNMEASURED"]
            lines += ["", "## Per org (class counts)", "",
                      "| " + " | ".join(cols) + " |",
                      "| " + " | ".join("-" * len(c.replace(" ", "")) for c in cols) + " |"]
        else:
            lines += ["", "## Per org (class counts)", "",
                      "| org | GitHub mirror login | class 1 | class 2 | class 3 | class 4 |",
                      "|-----|---------------------|---------|---------|---------|---------|"]
        for o in sorted(org_counts):
            c = org_counts[o]
            cells = [o, str(org_map[o]), str(c.get('1', 0)), str(c.get('2', 0)),
                     str(c.get('3', 0)), str(c.get('4', 0))]
            if show_um_col:
                cells.append(str(c.get("UNMEASURED", 0)))
            lines.append("| " + " | ".join(cells) + " |")
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
