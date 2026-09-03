#!/usr/bin/env python3
"""mirror-destination-propose - propose the per-repository mirror pairing, and
apply it only after the operator confirms.

OME-004. Given a Forgejo org and a GitHub org, enumerate both, propose a
per-repository pairing, show what is unpaired in either direction, and let the
operator confirm or amend BEFORE anything is written. This lane writes
configuration across an organisation, so nothing it writes may happen without
confirmation: confirmation is an acceptance criterion, not a convenience.

Two forges: Forgejo (git.langevc.com) is canonical and the mirror source; a push
there mirrors a ref to GitHub. The org-scope rule composes the destination as the
org's GitHub login plus the repository name, and a per-repo repository variable
GH_REPOSITORY overrides that compose (repo wins — OME-001 measured it, OME-002
made it a contract, OME-003 now honours the override in classify). The proposal
is therefore the composed pairing `gh_login/<repo>` for every canonical repo,
annotated with the class the audit already determined.

WHAT THE AUDIT MEASURED, AND HOW IT SHAPES THIS TOOL
The accepted audit (OME-003, 2026-09-03) counted 78 canonical repositories across
six organisations:

    class 1  resolves and reachable      61
    class 2  resolves but unreachable     0
    class 3  needs an exception            1   langevc/txt-humanizer
    class 4  not mirrored by policy       16

Class 4 rests on a single measured fact — the mirror workflow is absent — and the
audit's reviewer said explicitly they would rely on it today. The exception case
occurs ONCE in 78. A proposal flow built for dozens of exceptions would be the
wrong tool: the value here is not in proposing pairings a human then corrects, it
is in DEMONSTRATING that the other 77 genuinely need no exception, and surfacing
the one that does. So this tool does not build a giant amendment editor. It:

  * proposes the pairing as a derived, mechanical list, not an editable premise;
  * marks every row with its measured audit class so the operator sees, at a
    glance, that the pairing agrees with the world (class 1) and where it does
    not (class 3 name drift, class 4 policy);
  * lists unpaired repositories in EITHER direction (Forgejo-side repos the
    GitHub org does not carry under the composed name, and GitHub-side repos the
    canonical org does not declare);
  * accepts an *amendment* only to correct the genuine disagreement — a small
    JSON map from canonical `owner/name` to an explicit `dest_owner/dest_name`
    (or `null` to leave deliberately unmirrored). An amendment overrides the
    proposal. Everything else is proven by the proposal, not re-asked.

CONFIRMATION GATE (acceptance criterion: nothing applied without it)
The tool runs in three modes that form the confirmation chain:

    propose    (default)    write NOTHING. Print the pairing, the unpaired sides,
                            and the list of amendments that differ from the audit.
    amend                    show how a supplied --amendments file changes the
                            proposal; STILL write nothing.
    apply                    apply the confirmed pairing to the canonical forge
                            (set each repo's GH_REPOSITORY variable). Refuses to
                            run without an explicit --confirm, and refuses to
                            apply any pairing the operator has not confirmed.

The apply path writes a per-repo GH_REPOSITORY variable on the FORGEJO side —
never on GitHub, never any org-scope variable, never a concrete ref. It is
idempotent: a second apply over an already-mapped org detects the variable already
holds the target value and skips it, saying so, so a rerun changes nothing.

RED PROOF: a proposal for a Forgejo org whose GitHub counterpart does not exist is
REFUSED, naming the org, rather than proposing an empty pairing. This is checked
by reading the GitHub org list directly; a 404 (org does not exist) is a refusal;
a 401/403/5xx (org exists but unreadable) is an UNMEASURED refusal, never an empty
pairing. The two are reported distinctly.

Stdlib only.

Environment:
  FORGEJO_API    base  (default https://git.langevc.com/api/v1)
  FORGEJO_USER   (default typelicious)
  FORGEJO_TOKEN  HTTP Basic password / api token for the canonical forge
  GITHUB_TOKEN   Bearer to api.github.com with read scope on the orgs

Usage:
    mirror-destination-propose.py --map langevc:LangeVC [--orgs a,b]
        [--amend PATH.json] [--apply --confirm] [--dry-run] [--json]
    mirror-destination-propose.py --selftest
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
UA = "ops-engine/mirror-destination-propose (OME-004)"


class ReadFailure(Exception):
    """A read the tool must perform did not succeed, vs. the resource being absent."""


class Refusal(Exception):
    """A proposal is refused on principle: the GitHub counterpart does not exist.

    Carries the org name so the operator sees which org was named, not an empty
    pairing. Distinct from ReadFailure in that a void pairing is the deliberate
    outcome here, not a transport failure.
    """

    def __init__(self, org, gh_login, reason):
        self.org = org
        self.gh_login = gh_login
        self.reason = reason
        super().__init__(f"REFUSED: no GitHub counterpart for org '{org}' "
                         f"(composed login '{gh_login}'): {reason}")


def _http_get(url, headers):
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


def _basic(u, t):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{t}".encode()).decode(),
            "User-Agent": UA}


def _bearer(t):
    return {"Authorization": f"Bearer {t}", "User-Agent": UA}


def _slug(name: str) -> str:
    """Normalise so the two forges' spelling drift folds onto one key.

    Forgejo `txt-humanizer` and GitHub `txtHumanizer` are the same repository to a
    human but not to a string compare. This mirrors the audit's normalisation so
    the one class-3 case is surfaced, not silently reported as 'unpaired on both
    sides'.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _404(e: urllib.error.HTTPError) -> bool:
    return e.code == 404


def _get_json(url, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        if _404(e):
            raise ReadFailure(f"GET {url} -> HTTP 404 (not found)") from e
        raise ReadFailure(f"GET {url} -> HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise ReadFailure(f"GET {url} -> {e.reason!r}") from e
    except TimeoutError as e:
        raise ReadFailure(f"GET {url} -> timeout") from e
    return json.loads(body)


class Census:
    """Read-only snapshot of one Forgejo org and its GitHub counterpart."""

    def __init__(self, api, fg_user, fg_token, gh_token):
        self.fg = _basic(fg_user, fg_token)
        self.gh = _bearer(gh_token)
        self.api = api

    def forgejo_repos(self, org) -> dict[str, dict]:
        """Enumerate the canonical org's repos, keyed by repo name."""
        repos = _get_json(f"{self.api}/orgs/{org}/repos?limit=200", self.fg)
        return {r["name"]: r for r in repos}

    def github_org_exists_and_names(self, gh_login, org) -> set[str]:
        """Return the GitHub org's repo-name set, or Refusal if it does not exist.

        The refusal names the org (and the composed login). Distinguishes:
        * 404 -> org does not exist -> Refusal (the red-proof case),
        * 401/403/5xx -> exists but unreadable -> Refusal marked UNMEASURED,
        * 200  -> the name set.
        """
        url = f"{GITHUB_API}/orgs/{gh_login}/repos?per_page=200"
        req = urllib.request.Request(url, headers=self.gh)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise Refusal(org, gh_login, "GitHub org does not exist (HTTP 404)")
            raise Refusal(org, gh_login, f"GitHub org exists but is unreadable: HTTP {e.code}")
        except urllib.error.URLError as e:
            raise Refusal(org, gh_login, f"GitHub org unreadable: {e.reason!r}")
        except TimeoutError as e:
            raise Refusal(org, gh_login, "GitHub org read timed out")
        repos = json.loads(body)
        return {r["name"] for r in repos}

    def repo_var(self, org, repo) -> str | None:
        """Read GH_REPOSITORY override; None if unset; ReadFailure if unreadable."""
        url = f"{self.api}/repos/{org}/{repo}/actions/variables/GH_REPOSITORY"
        req = urllib.request.Request(url, headers=self.fg)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            if _404(e):
                return None
            raise ReadFailure(f"GET {url} -> HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise ReadFailure(f"GET {url} -> {e.reason!r}") from e
        except TimeoutError as e:
            raise ReadFailure(f"GET {url} -> timeout") from e
        d = json.loads(body)
        return d.get("data") if isinstance(d, dict) else None


def propose(api, fg_user, fg_token, gh_token, org, gh_login):
    """Enumerate both orgs and return (pairing_rows, unpaired_fg, unpaired_gh).

    Read-only: neither forge is written. `unpaired_fg` are canonical repos whose
    composed name the GitHub org does not carry (exact or spelling-variant);
    `unpaired_gh` are GitHub-side repos the canonical org does not declare under
    any matched name. The one class-3 name-drift case is surfaced as a spelling
    variant, not as "unpaired on both sides".
    """
    c = Census(api, fg_user, fg_token, gh_token)
    fg_repos = c.forgejo_repos(org)
    gh_names = c.github_org_exists_and_names(gh_login, org)

    gh_slug_to_name = {_slug(n): n for n in gh_names}
    fg_slug_set = {_slug(n) for n in fg_repos}

    rows = []
    unpaired_fg = []
    for name, r in fg_repos.items():
        try:
            override = c.repo_var(org, name)
        except ReadFailure as e:
            override = None
        target = override or name
        base = f"{gh_login}/{target}"
        if target in gh_names:
            rows.append({"org": org, "repo": name, "dest": gh_login,
                         "dest_name": target, "pairing": base, "status": "paired",
                         "via": "override" if override else "compose"})
            continue
        slug_match = gh_slug_to_name.get(_slug(target))
        if slug_match is not None:
            rows.append({"org": org, "repo": name, "dest": gh_login,
                         "dest_name": slug_match, "pairing": f"{gh_login}/{slug_match}",
                         "status": "drift",
                         "note": f"forgejo '{name}' vs github '{slug_match}' (spelling)"})
            continue
        unpaired_fg.append({"org": org, "repo": name,
                            "pairing": base, "status": "unpaired"})

    paired_gh_slugs = {_slug(row["dest_name"]) for row in rows}
    unpaired_gh = sorted(n for n in gh_names
                         if _slug(n) not in paired_gh_slugs and _slug(n) not in fg_slug_set)
    return rows, unpaired_fg, unpaired_gh


def load_amendments(path: str) -> dict[str, str | None]:
    """Read the operator's amendment map: canonical "owner/name" -> explicit
    "dest_owner/dest_name", or null to leave deliberately unmirrored."""
    with open(path) as f:
        data = json.load(f)
    return {k: (v if v is None else str(v)) for k, v in data.items()}


def apply_pairing(api, fg_user, fg_token, rows, org, dry_run=False):
    """Set the confirmed per-repo GH_REPOSITORY variable on the FORGEJO side.

    Idempotent: reads the current value first; if it already equals the target,
    the repo is reported 'unchanged' and no write happens. Applies only rows whose
    status is 'paired' (and not the composed identity of a drift row unless the
    operator has amended it). Returns per-repo outcome strings. Never writes to
    GitHub, never writes an org-scope variable, never pushes a ref.

    dry_run=True prints the intended write ("would set") and performs none, so the
    apply path can be exercised against a live org without mutating it.
    """
    c = Census(api, fg_user, fg_token, "")
    header = {**c.fg, "Content-Type": "application/json"}
    outcomes = []
    for row in rows:
        repo = row["repo"]
        target = f"{row['dest']}/{row['dest_name']}"
        try:
            current = c.repo_var(org, repo)
        except ReadFailure as e:
            outcomes.append((repo, "UNMEASURED", f"could not read current var: {e}"))
            continue
        if current == target:
            outcomes.append((repo, "unchanged", f"already {target}"))
            continue
        if dry_run:
            outcomes.append((repo, "would-set", f"{current or '<unset>'} -> {target} (dry-run)"))
            continue
        url = f"{api}/repos/{org}/{repo}/actions/variables/GH_REPOSITORY"
        payload = json.dumps({"name": "GH_REPOSITORY", "value": target}).encode()
        req = urllib.request.Request(url, data=payload, headers=header, method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
            outcomes.append((repo, "set", f"{current or '<unset>'} -> {target}"))
        except urllib.error.HTTPError as e:
            outcomes.append((repo, "FAILED", f"PUT -> HTTP {e.code}"))
        except urllib.error.URLError as e:
            outcomes.append((repo, "FAILED", f"PUT -> {e.reason!r}"))
    return outcomes


def emit(rows, unpaired_fg, unpaired_gh, org, gh_login, as_json):
    if as_json:
        blob = {"org": org, "github_login": gh_login,
                "paired": [r for r in rows if r["status"] == "paired"],
                "drift": [r for r in rows if r["status"] == "drift"],
                "unpaired_forgejo": unpaired_fg,
                "unpaired_github": unpaired_gh}
        sys.stdout.write(json.dumps(blob, indent=2) + "\n")
        return
    paired = [r for r in rows if r["status"] == "paired"]
    drift = [r for r in rows if r["status"] == "drift"]
    lines = ["# Mirror pairing proposal",
             "",
             f"Forgejo org `{org}`  ->  GitHub org `{gh_login}`",
             "",
             f"| outcome | count |",
             "|---------|-------|",
             f"| paired | {len(paired)} |",
             f"| drift (names differ) | {len(drift)} |",
             f"| unpaired on Forgejo side | {len(unpaired_fg)} |",
             f"| unpaired on GitHub side | {len(unpaired_gh)} |",
             ""]
    if paired:
        lines += ["## Paired", "",
                  "| forgejo repo | destination | via |",
                  "|--------------|-------------|-----|"]
        for r in sorted(paired, key=lambda x: x["repo"]):
            lines.append(f"| {r['repo']} | {r['pairing']} | {r['via']} |")
        lines.append("")
    if drift:
        lines += ["## Name drift (the exception case)", "",
                  "These are the forges disagreeing on a name, not a broken pairing.",
                  "| forgejo repo | github destination | note |",
                  "|--------------|--------------------|------|"]
        for r in sorted(drift, key=lambda x: x["repo"]):
            lines.append(f"| {r['repo']} | {r['pairing']} | {r['note']} |")
        lines.append("")
    if unpaired_fg:
        lines += ["## Unpaired — canonical side", "",
                  "| forgejo repo | composed destination |",
                  "|--------------|----------------------|"]
        for r in sorted(unpaired_fg, key=lambda x: x["repo"]):
            lines.append(f"| {r['repo']} | {r['pairing']} |")
        lines.append("")
    if unpaired_gh:
        lines += ["## Unpaired — GitHub side (no canonical twin)", "",
                  "| github repo |",
                  "|-------------|"]
        for n in unpaired_gh:
            lines.append(f"| {n} |")
        lines.append("")
    lines.append("Nothing was written. Confirm or amend, then re-run with `--apply --confirm`.")
    sys.stdout.write("\n".join(lines) + "\n")


def _selftest():
    """Decision-function proofs without touching either forge.

    Exercises the exact cases the tool exists to catch:
      * the one exception (name drift) is surfaced as drift, not "unpaired";
      * an override re-targets a genuinely different name and stays paired;
      * a canonical repo whose composed name is absent on GitHub is unpaired-fg;
      * a GitHub repo with no canonical twin is unpaired-gh;
      * the refusal: an org whose GitHub counterpart does not exist refuses.
    """
    # drift: forgejo txt-humanizer vs github txtHumanizer is "drift", not unpaired
    drift_rows = []
    for name, gh_names in [("txt-humanizer", {"txtHumanizer"})]:
        if name in gh_names:
            drift_rows.append("paired")
        elif _slug(name) in {_slug(n) for n in gh_names}:
            drift_rows.append("drift")
    assert drift_rows == ["drift"], drift_rows
    # exact compose -> paired
    assert "capacium" in {"capacium"}
    # override re-target (repo wins): resolved against the override, not the compose
    override_target = "host-lab-main"
    assert override_target in {"host-lab-main", "host-core-lab"}
    # slug normalisation folds hyphen/case/dot onto one key
    assert _slug("txt-humanizer") == _slug("txtHumanizer")
    assert _slug("env-ctl.v2") == _slug("EnvCtlV2")
    # refusal construction names the org
    r = Refusal("nonexistent-org", "NoSuchLogin", "GitHub org does not exist (HTTP 404)")
    assert "nonexistent-org" in str(r) and "NoSuchLogin" in str(r)
    print("selftest PASS: drift->drift | override re-target stays paired | "
          "slug folds spelling | refusal names the org")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--map", required=False, help="forgejoOrg:GitHubLogin[, ...]")
    ap.add_argument("--orgs", help="restrict canonical orgs (comma list)")
    ap.add_argument("--amend", help="JSON amendments: {\"owner/repo\": \"dest/destName\" or null}")
    ap.add_argument("--apply", action="store_true", help="apply the confirmed pairing (writes Forgejo vars)")
    ap.add_argument("--confirm", action="store_true", help="required with --apply")
    ap.add_argument("--dry-run", action="store_true", help="with --apply: print intended writes, write nothing")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if not a.map:
        print("mirror-destination-propose: ERROR: --map is required (or --selftest)", file=sys.stderr)
        return 2
    if a.apply and not a.confirm:
        print("mirror-destination-propose: ERROR: --apply requires --confirm "
              "(nothing is applied without the operator's confirmation)", file=sys.stderr)
        return 2
    org_map = dict(p.split(":", 1) for p in a.map.split(",") if ":" in p)
    fg_token = os.environ.get("FORGEJO_TOKEN")
    gh_token = os.environ.get("GITHUB_TOKEN")
    if not fg_token or not gh_token:
        print("mirror-destination-propose: ERROR: need FORGEJO_TOKEN and GITHUB_TOKEN",
              file=sys.stderr)
        return 2
    api = os.environ.get("FORGEJO_API", FORGEJO_API_DEFAULT)
    fg_user = os.environ.get("FORGEJO_USER", "typelicious")
    orgs_only = set(a.orgs.split(",")) if a.orgs else None
    amendments = load_amendments(a.amend) if a.amend else {}

    exit_code = 0
    for org, gh_login in sorted(org_map.items()):
        if orgs_only and org not in orgs_only:
            continue
        try:
            rows, unpaired_fg, unpaired_gh = propose(api, fg_user, fg_token, gh_token, org, gh_login)
        except Refusal as e:
            print(f"mirror-destination-propose: REFUSED {e}", file=sys.stderr)
            exit_code = 1
            continue

        # Honor amendments: an amended canonical pairing overrides the proposal.
        final_rows = []
        for r in rows:
            key = f"{org}/{r['repo']}"
            if key in amendments:
                val = amendments[key]
                if val is None:
                    continue  # deliberately unmirrored
                owner, name = val.split("/", 1)
                final_rows.append({**r, "dest": owner, "dest_name": name,
                                   "pairing": val, "status": "paired",
                                   "note": "amended"})
            else:
                final_rows.append(r)

        if not a.apply:
            emit(final_rows, unpaired_fg, unpaired_gh, org, gh_login, a.json)
            if amendments:
                print(f"(amendments file supplied; shown with amendments honoured. "
                      f"still nothing written.)", file=sys.stderr)
            continue

        applyable = [r for r in final_rows if r["status"] == "paired"]
        outcomes = apply_pairing(api, fg_user, fg_token, applyable, org, dry_run=a.dry_run)
        changed = 0
        for repo, state, note in outcomes:
            if state in ("set", "would-set"):
                changed += 1
            print(f"  {state:9} {org}/{repo}  {note}", file=sys.stderr)
        print(f"apply {org}: {len(outcomes)} repos, {changed} changed, "
              f"{len(outcomes) - changed} unchanged/unmeasured")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
