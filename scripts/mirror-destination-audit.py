#!/usr/bin/env python3
"""mirror-destination-audit - classify every canonical repository by mirror policy.

OME-003, retrofitted to the OME-012 two-variable contract. Answers "which
repositories does the organisation mirror rule not cover", and replaces the
never-counted "about seventy" figure with a real total.

Forgejo (git.langevc.com) is the single source of truth for repositories: a
push there triggers a workflow that mirrors the ref to GitHub. The mirror
destination is a TWO-variable contract (OME-012) — not an org compose plus a
repo override — and the two halves carry no precedence between them:

    GH_REPO_OWNER  ORG scope   the GitHub owner            e.g. Capacium
    GH_REPO        REPO scope  the full owner/name         e.g. Capacium/capacium

BOTH are required. A repository's GH_REPO is the destination VERBATIM
(whitespace stripped only; never recomposed, never recased), and its owner
prefix must EQUAL the org's GH_REPO_OWNER in a case-sensitive double match that
is decided BEFORE any GitHub request. Mismatch (or an unset half) refuses; the
repository will never mirror. This audit reads both forges *read-only*, resolves
each canonical repository against this contract, and places it in exactly one
of five classes:

  1. resolves and reachable        - double match OK, GitHub carries the name
  2. resolves but unreachable      - double match OK, GitHub lacks the name
  3. needs an exception            - the two forges disagree on the name
  4. not mirrored by policy        - no mirror workflow on the canonical repo
  5. refused before any GitHub request - an unset half or a double-match mismatch

Class 5 is the contract's own refusal: an unset GH_REPO_OWNER/GH_REPO or a
GH_REPO whose owner prefix disagrees with GH_REPO_OWNER stops the workflow
before it ever asks GitHub, so the repository will not mirror — and this audit,
whose job is to say which repositories will and will not mirror, reports it as
its own outcome rather than folding it into reachable/unreachable.

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

The two variable names are NOT declared here: they are imported from
``ops_engine.modules.mirror`` (the single declaration point), because
restatement is how this contract drifted in the first place. That import pulls
in the ops-engine package (pydantic/httpx are already runtime dependencies of
this repository), so the script no longer claims to be stdlib-only.

Environment:
  FORGEJO_API    base  (default https://git.langevc.com/api/v1)
  FORGEJO_USER   (default typelicious)
  FORGEJO_TOKEN  HTTP Basic password / api token for the canonical forge
  GITHUB_TOKEN   Bearer to api.github.com with read scope on the orgs

The GitHub mirror owner for each canonical org is read live from the org-scope
GH_REPO_OWNER variable; ``--map forgejoOrg:GitHubOwner[,...]`` remains as an
explicit override only (it is no longer how the owner is discovered). An org
that carries no GH_REPO_OWNER and no --map entry is skipped, exactly as it would
never mirror. Class 4's basis (workflow absence) does not depend on the owner.

Usage:
    mirror-destination-audit.py [--map langevc:LangeVC,capacium:Capacium]
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

# The two variable names are declared ONCE in ops_engine.modules.mirror and
# exported for exactly this purpose. Import them; do not restate the strings.
# The audit runs standalone from scripts/, so the repo's src/ must be on the
# path (the same layout pytest uses via pythonpath = ["src"]).
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
from ops_engine.modules.mirror import (  # noqa: E402
    MIRROR_OWNER_VARIABLE,
    MIRROR_REPO_VARIABLE,
)

FORGEJO_API_DEFAULT = "https://git.langevc.com/api/v1"
GITHUB_API = "https://api.github.com"
UA = "ops-engine/mirror-destination-audit (OME-013)"

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

    def org_var(self, org):
        """Return org-scope GH_REPO_OWNER, None if unset, or raise ReadFailure.

        HTTP 404 means the variable genuinely does not exist (the org is not a
        mirror owner); any other failure is a ReadFailure the caller must surface
        as UNMEASURED rather than silently treating as "no owner".
        """
        url = f"{self.api}/orgs/{org}/actions/variables/{MIRROR_OWNER_VARIABLE}"
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

    def repo_var(self, org, repo):
        """Return repo-scope GH_REPO value, None if unset, or raise ReadFailure.

        The value is the full ``owner/name`` destination, used verbatim. HTTP 404
        means the variable genuinely does not exist (the workflow will refuse the
        unset half); any other failure is a ReadFailure the caller must surface as
        UNMEASURED rather than silently treating as "no destination".
        """
        url = f"{self.api}/repos/{org}/{repo}/actions/variables/{MIRROR_REPO_VARIABLE}"
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
    This makes a hyphen-/case-/dot-insensitive key for drift detection ONLY.

    Slugging is FORBIDDEN everywhere a real contract decision is made: it must
    never feed the double match (a case-sensitive compare of GH_REPO's owner
    prefix against GH_REPO_OWNER), and it must never rewrite a destination (the
    owner and destination are used verbatim, whitespace strip only). It exists
    solely to recognise the class-3 name-drift case after the double match has
    already passed on the exact strings.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def classify(repo_name: str, mirror_yml_present: bool, gh_names: set[str],
             owner: str | None = None, repo_dest: str | None = None):
    """Return (cls, detail) for one canonical repo against a GitHub org name set.

    The mirror contract is TWO variables with no precedence between them:
    ``GH_REPO_OWNER`` (org scope, the GitHub owner) and ``GH_REPO`` (repo scope,
    the full ``owner/name`` destination). There is no override-versus-compose:
    ``GH_REPO`` is the destination VERBATIM (whitespace stripped only) and must
    AGREE with ``GH_REPO_OWNER`` via a case-sensitive double match on its owner
    prefix, decided BEFORE any GitHub request.

    Classification, in the workflow's own refusal order:

      * no mirror workflow            -> class 4 (policy).
      * GH_REPO_OWNER unset           -> class 5 (refused; never asks GitHub).
      * GH_REPO unset                 -> class 5 (refused; never asks GitHub).
      * GH_REPO owner prefix != owner -> class 5 (double-match mismatch).
      * double match OK, name present -> class 1 (resolves and reachable).
      * name present only as a slug-variant (spelling drift) -> class 3.
      * double match OK, name absent  -> class 2 (resolves but unreachable).

    The owner and destination are used VERBATIM beyond a whitespace strip — no
    case folding, no slugging — because the double match is a plain case-sensitive
    compare and any normalisation breaks the very check the contract is built on.
    """
    if not mirror_yml_present:
        if PLANNING_MARKER.search(repo_name):
            return "4", "no workflow; *-planning/*-internal-docs policy marker"
        return "4", "no mirror workflow on canonical repo (mirror not declared)"

    owner = owner.strip() if owner is not None else None
    repo_dest = repo_dest.strip() if repo_dest is not None else None

    if not owner:
        return "5", (f"{MIRROR_OWNER_VARIABLE} unset at org scope "
                     f"(workflow refuses before any GitHub request)")
    if not repo_dest:
        return "5", (f"{MIRROR_REPO_VARIABLE} unset at repo scope "
                     f"(workflow refuses before any GitHub request)")

    # DOUBLE MATCH, before any GitHub request: GH_REPO must be owner/name with a
    # non-empty owner and name, and its owner prefix must equal GH_REPO_OWNER
    # case-sensitively. GH_REPO is the destination verbatim (split once); it is
    # never recomposed onto the org owner.
    dest_owner, sep, dest_name = repo_dest.partition("/")
    if not sep or not dest_name or dest_owner != owner:
        return "5", (f"DOUBLE MATCH failed: {MIRROR_REPO_VARIABLE} '{repo_dest}' "
                     f"owner prefix '{dest_owner}' != {MIRROR_OWNER_VARIABLE} "
                     f"'{owner}' (case-sensitive); refused before any GitHub request")

    if dest_name in gh_names:
        return "1", repo_dest
    # name disagreement: a spelling-variant of the destination present under the org.
    matches = [n for n in gh_names if _slug(n) == _slug(dest_name)]
    if matches:
        actual = sorted(matches)[0]
        return "3", (f"{owner}/{actual} ({MIRROR_REPO_VARIABLE} {repo_dest} "
                     f"vs github {actual})")
    return "2", f"{repo_dest} (absent/unreachable on github)"


def gather(api, fg_user, fg_token, gh_token, org_map, orgs_only=None):
    c = Census(api, fg_user, fg_token, gh_token)
    rows = []
    forgejo_orgs = _get_json(f"{api}/user/orgs", c.fg)
    for org in forgejo_orgs:
        u = org["username"]
        if orgs_only and u not in orgs_only:
            continue
        # The GitHub mirror owner is the org-scope GH_REPO_OWNER, read live; an
        # explicit --map entry overrides it (only). An org the credential cannot
        # enumerate is UNMEASURED, never silently skipped.
        try:
            canon = {r["name"]: r for r in _get_json(f"{api}/orgs/{u}/repos?limit=200", c.fg)}
        except Exception as e:
            rows.append({"org": u, "repo": "(org unreadable)", "class": "UNMEASURED",
                         "dest_or_note": f"could not enumerate canonical org: {e}", "private": None,
                         "gh_login": org_map.get(u), "repo_var": None})
            continue
        if u in org_map:
            owner = org_map[u]
        else:
            try:
                owner = c.org_var(u)
            except ReadFailure as e:
                for rname, r in canon.items():
                    rows.append({"org": u, "repo": rname, "class": "UNMEASURED",
                                 "dest_or_note": f"could not read {MIRROR_OWNER_VARIABLE}: {e}",
                                 "private": r["private"], "gh_login": None, "repo_var": None})
                continue
            if owner is None:
                continue  # no GH_REPO_OWNER and no --map entry -> not a mirror org
        gh_login = owner
        if gh_login is None:
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
                             "dest_or_note": f"could not read {MIRROR_REPO_VARIABLE} variable: {e}",
                             "private": r["private"], "gh_login": gh_login, "repo_var": None})
                continue
            cls_, detail = classify(rname, yml is not None, gh_names, gh_login, rvar)
            rows.append({"org": u, "repo": rname, "class": cls_, "dest_or_note": detail,
                         "private": r["private"], "gh_login": gh_login, "repo_var": rvar})
    return rows


def _selftest():
    """Decision-function proofs (no forge is touched).

    Three families of proof against the two-variable contract:
      * two-forge name drift: the same repo whose spelled name differs between
        the forges (forgejo `txt-humanizer` vs github `txtHumanizer`) is class 3,
        never class 2;
      * the full-destination GH_REPO is used VERBATIM (never recomposed onto the
        org owner) and the double match decides refusal BEFORE any GitHub name
        test — an unset half or an owner-prefix mismatch is class 5;
      * the destination owner is not derivable (elementeer stays lowercase,
        capacium capitalised, veeona becomes Veeona-AI), so the double match is
        case-sensitive and no normalisation is applied.
    """
    # --- two-forge name drift (class 3), full destination passes double match ---
    cls3, detail3 = classify("txt-humanizer", True, {"txtHumanizer"}, "LangeVC", "LangeVC/txt-humanizer")
    assert cls3 == "3" and "vs github" in detail3, (cls3, detail3)
    # a genuinely absent name (double match OK, name not carried) is class 2 and must stay 2:
    cls2, _ = classify("envctl", True, {"unrelated"}, "LangeVC", "LangeVC/envctl")
    assert cls2 == "2", cls2
    # exact destination present -> class 1, reported as the verbatim GH_REPO:
    cls1, d1 = classify("skillweave", True, {"skillweave"}, "LangeVC", "LangeVC/skillweave")
    assert cls1 == "1" and d1 == "LangeVC/skillweave", (cls1, d1)
    # --- the full-destination GH_REPO is used verbatim; never recomposed ---
    # LangeVC/skillweave must NOT become LangeVC/LangeVC/skillweave, and the
    # recomposed string is unreachable in the detail:
    cls_c, d_c = classify("capacium", True, {"capacium"}, "Capacium", "Capacium/capacium")
    assert cls_c == "1" and d_c == "Capacium/capacium", (cls_c, d_c)
    assert "Capacium/Capacium/" not in d_c and "LangeVC/LangeVC/" not in d_c
    # --- double match decides refusal BEFORE any GitHub name test (class 5) ---
    # unset owner -> 5
    cls5a, d5a = classify("envctl", True, {"envctl"}, None, "LangeVC/envctl")
    assert cls5a == "5" and "unset at org scope" in d5a, (cls5a, d5a)
    # unset repo -> 5
    cls5b, _ = classify("envctl", True, {"envctl"}, "LangeVC", None)
    assert cls5b == "5", cls5b
    # owner-prefix mismatch (case-sensitive) -> 5, even though the name is
    # present on GitHub and would otherwise be class 1:
    cls5c, d5c = classify("skillweave", True, {"skillweave"}, "LangeVC", "langevc/skillweave")
    assert cls5c == "5" and "DOUBLE MATCH failed" in d5c, (cls5c, d5c)
    # a bare GH_REPO (no "/") is not a full destination -> mismatch -> 5:
    cls5d, _ = classify("envctl", True, {"envctl"}, "LangeVC", "envctl")
    assert cls5d == "5", cls5d
    # --- owner is not derivable: case-sensitive, no normalisation ---
    # elementeer stays lowercase; a capitalised owner prefix mismatches:
    cls_e1, _ = classify("elementeer", True, {"elementeer"}, "elementeer", "elementeer/elementeer")
    assert cls_e1 == "1", cls_e1
    cls_e2, _ = classify("elementeer", True, {"elementeer"}, "elementeer", "Elementeer/elementeer")
    assert cls_e2 == "5", cls_e2
    # fusionAIze and Veeona-AI keep their mixed case verbatim:
    cls_f, _ = classify("faigate", True, {"faigate"}, "fusionAIze", "fusionAIze/faigate")
    assert cls_f == "1", cls_f
    cls_f2, _ = classify("faigate", True, {"faigate"}, "fusionAIze", "fusionaize/faigate")
    assert cls_f2 == "5", cls_f2
    cls_v, _ = classify("veeona", True, {"veeona"}, "Veeona-AI", "Veeona-AI/veeona")
    assert cls_v == "1", cls_v
    # --- no workflow -> 4 (unchanged) ---
    cls4, _ = classify("miama-planning", False, set(), "LangeVC", None)
    assert cls4 == "4", cls4
    cls4b, _ = classify("envctl", False, {"envctl"}, "LangeVC", "LangeVC/envctl")
    assert cls4b == "4", cls4b
    print("selftest PASS: drift->3 | absent->2 | exact->1 | verbatim destination | "
          "double-match refusal->5 (unset/mismatch/bare) | case-sensitive no-normalise | "
          "unmirrored->4")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--map", required=False, help="forgejoOrg:GitHubOwner[, ...] (override for the org GH_REPO_OWNER)")
    ap.add_argument("--orgs", help="restrict canonical orgs (comma list)")
    ap.add_argument("--out", help="write markdown report to PATH")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    org_map = dict(p.split(":", 1) for p in (a.map or "").split(",") if ":" in p)
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
        for c in ("1", "2", "3", "4", "5"):
            lines.append(f"| {c} | {class_count.get(c, 0)} |")
        if unmeasured:
            lines.append(f"| UNMEASURED | {unmeasured} |")
        # Per-org grid: add an UNMEASURED column only when some row carries it, so
        # the grid reconciles with the row table whether or not a read failed.
        show_um_col = any(c.get("UNMEASURED", 0) for c in org_counts.values())
        base_cols = ["org", "GitHub mirror owner", "class 1", "class 2", "class 3", "class 4", "class 5"]
        if show_um_col:
            cols = base_cols + ["UNMEASURED"]
            lines += ["", "## Per org (class counts)", "",
                      "| " + " | ".join(cols) + " |",
                      "| " + " | ".join("-" * len(c.replace(" ", "")) for c in cols) + " |"]
        else:
            lines += ["", "## Per org (class counts)", "",
                      "| " + " | ".join(base_cols) + " |",
                      "| " + " | ".join("-" * len(c.replace(" ", "")) for c in base_cols) + " |"]
        for o in sorted(org_counts):
            c = org_counts[o]
            owner = next((r["gh_login"] for r in rows if r["org"] == o and r["gh_login"]), org_map.get(o))
            cells = [o, str(owner), str(c.get('1', 0)), str(c.get('2', 0)),
                     str(c.get('3', 0)), str(c.get('4', 0)), str(c.get('5', 0))]
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
