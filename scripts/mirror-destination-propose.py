#!/usr/bin/env python3
"""mirror-destination-propose - propose the per-repository mirror pairing, and
apply it only after the operator confirms.

OME-011. The mirror workflow contract moved on 2026-09-03 from the single
``GH_REPOSITORY`` variable to a PAIR read through the Forgejo ``vars`` context:

    GH_REPO_OWNER   org scope    the GitHub org/user that owns the mirror
    GH_REPO         repo scope   the full owner/name destination

The workflow's preflight refuses, in cost order, on: unset owner, unset repo,
DOUBLE MATCH failure (``GH_REPO``'s owner prefix != ``GH_REPO_OWNER``, a
CASE-SENSITIVE string compare evaluated BEFORE any GitHub request), then
existence, then push permission. ``GH_REPOSITORY`` is no longer read by anything.

This lane therefore proposes and writes the ``GH_REPO_OWNER`` / ``GH_REPO``
pair. The destination OWNER is not derivable from the org name: ``elementeer``
stays lowercase, ``capacium`` becomes ``Capacium``, ``fusionaize`` becomes
``fusionAIze``, ``veeona`` becomes ``Veeona-AI``. The org-scope variable is the
ONLY source of the owner. The tool READS ``GH_REPO_OWNER`` and NEVER writes it.

Contract:

    precondition   the org carries GH_REPO_OWNER. The tool READS it. It NEVER
                   writes it.
    propose        for each repo in the org: GH_REPO = <GH_REPO_OWNER>/<name>,
                   unless amended. The composed value uses the owner VERBATIM.
    uniqueness     no two canonical repos may propose the same destination.
                   Colliding pairs are REFUSED, both sources and the destination
                   named. Never propose either half. This is first-come,
                   first-served, NOT a refusal of the whole org.
    confirm        --apply --confirm, exactly as today.
    write          CREATE (POST) when absent, UPDATE (PUT) when present, NOTHING
                   when already equal.

An owner that carries no variable and no ``--map`` override is a REFUSAL naming
the org. Reading org-scope variables may require rights the credential lacks:
ABSENT (404) and UNREADABLE (401/403/5xx) are reported distinctly, never folded
together and never into a derived owner.

Two forges: Forgejo (git.langevc.com) is canonical and the mirror source; a push
there mirrors a ref to github.com. A repo-scope ``GH_REPO`` override is the
destination VERBATIM, so a value like ``LangeVC/skillweave`` is already a full
``owner/name`` and is reported as paired/unchanged, never recomposed.

CONFIRMATION GATE (acceptance criterion: nothing applied without it)
The tool runs in modes that form the confirmation chain:

    propose    (default)    write NOTHING. Print the pairing, the unpaired sides,
                            and the collision rows (a loser named with its
                            competitor).
    apply                    apply the confirmed pairing to the canonical forge
                             (set each repo's GH_REPO variable). Refuses without
                             --confirm.

The apply path writes a per-repo GH_REPO variable on the FORGEJO side — never on
GitHub, never an org-scope variable, never a concrete ref. CREATE when absent,
UPDATE when present, NO request when already equal, so a rerun changes nothing.

RED PROOF: a proposal for a Forgejo org whose GitHub counterpart does not exist is
REFUSED, naming the org, rather than proposing an empty pairing. A 404 is a
refusal; a 401/403/5xx is an UNMEASURED refusal. The two are reported distinctly.

Stdlib only.

Environment:
  FORGEJO_API    base  (default https://git.langevc.com/api/v1)
  FORGEJO_USER   (default typelicious)
  FORGEJO_TOKEN  HTTP Basic password / api token for the canonical forge
  GITHUB_TOKEN   Bearer to api.github.com with read scope on the orgs

Usage:
    mirror-destination-propose.py [--map langevc:LangeVC] [--orgs a,b]
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
UA = "ops-engine/mirror-destination-propose (OME-011)"

# The two Actions variables of the 2026-09-03 mirror contract. GH_REPO_OWNER is
# org scope and is only ever READ here; GH_REPO is repo scope and is the value
# this tool proposes and writes. GH_REPOSITORY (the OME-001..004 single-variable
# contract) is no longer read or written anywhere by this tool.
OWNER_VAR = "GH_REPO_OWNER"
REPO_VAR = "GH_REPO"


class ReadFailure(Exception):
    """A read the tool must perform did not succeed, vs. the resource being absent."""


class Refusal(Exception):
    """A proposal is refused on principle.

    Carries the org name so the operator sees which org was named, not an empty
    pairing. ``reason`` states whether the counterpart does not exist (404) or
    exists but is unreadable (401/403/5xx, marked UNMEASURED). Distinct from
    ReadFailure in that a void pairing is the deliberate outcome here, not a
    transport failure.
    """

    def __init__(self, org, reason):
        self.org = org
        self.reason = reason
        super().__init__(f"REFUSED: org '{org}': {reason}")


class OwnerRefusal(Refusal):
    """The org carries no readable GH_REPO_OWNER and no --map override."""


def _http_json(url, headers, method="GET", payload=None):
    data = None
    if payload is not None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    req = urllib.request.Request(url, headers=headers, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        raise ReadFailure(f"{method} {url} -> {e.reason!r}") from e
    except TimeoutError as e:
        raise ReadFailure(f"{method} {url} -> timeout") from e


def _basic(u, t):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{t}".encode()).decode(),
            "User-Agent": UA}


def _bearer(t):
    return {"Authorization": f"Bearer {t}", "User-Agent": UA}


def _slug(name: str) -> str:
    """Normalise so the two forges' spelling drift folds onto one key.

    Forgejo ``txt-humanizer`` and GitHub ``txtHumanizer`` are the same repository
    to a human but not to a string compare.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _404(code: int) -> bool:
    return code == 404


class Census:
    """Read/write view of one Forgejo org and its GitHub counterpart."""

    def __init__(self, api, fg_user, fg_token, gh_token):
        self.fg = _basic(fg_user, fg_token)
        self.gh = _bearer(gh_token)
        self.api = api
        self.fg_json = {**self.fg, "Content-Type": "application/json"}

    def forgejo_repos(self, org) -> dict[str, dict]:
        """Enumerate the canonical org's repos, keyed by repo name."""
        status, body = _http_json(f"{self.api}/orgs/{org}/repos?limit=200", self.fg)
        if status != 200:
            raise ReadFailure(f"list org repos {org} -> HTTP {status}")
        repos = json.loads(body)
        return {r["name"]: r for r in repos}

    def org_owner(self, org) -> str | None:
        """Read org-scope GH_REPO_OWNER.

        Returns None when the variable is ABSENT (404). Raises ReadFailure when
        it is UNREADABLE (401/403/5xx/transport). The two are deliberately
        distinct: never let an unreadable scope degrade into "absent" or into a
        derived owner.
        """
        url = f"{self.api}/orgs/{org}/actions/variables/{OWNER_VAR}"
        status, body = _http_json(url, self.fg)
        if _404(status):
            return None
        if status != 200:
            raise ReadFailure(f"read org {OWNER_VAR} for '{org}' -> HTTP {status}")
        d = json.loads(body)
        if isinstance(d, dict) and d.get("data"):
            return d["data"]
        return None

    def github_org_exists_and_names(self, gh_login, org) -> set[str]:
        """Return the GitHub org's repo-name set, or Refusal if it does not exist."""
        url = f"{GITHUB_API}/orgs/{gh_login}/repos?per_page=200"
        status, body = _http_json(url, self.gh)
        if status == 404:
            raise Refusal(org, "GitHub org does not exist (HTTP 404)")
        if status != 200:
            raise Refusal(org, f"GitHub org exists but is unreadable: HTTP {status} (UNMEASURED)")
        repos = json.loads(body)
        return {r["name"] for r in repos}

    def repo_var(self, org, repo) -> str | None:
        """Read repo-scope GH_REPO; None if unset; ReadFailure if unreadable."""
        url = f"{self.api}/repos/{org}/{repo}/actions/variables/{REPO_VAR}"
        status, body = _http_json(url, self.fg)
        if _404(status):
            return None
        if status != 200:
            raise ReadFailure(f"read repo {REPO_VAR} for '{org}/{repo}' -> HTTP {status}")
        d = json.loads(body)
        return d.get("data") if isinstance(d, dict) else None


def read_owner(api, fg_user, fg_token, org, override_map):
    """Resolve the destination owner for one org.

    Order: --map override wins; else the org-scope GH_REPO_OWNER variable; else
    a refusal. An unreadable variable is an OwnerRefusal naming the scope, never
    a silent fall-through to "absent" or to a derived owner.
    """
    if org in override_map:
        return override_map[org]
    c = Census(api, fg_user, fg_token, "")
    try:
        owner = c.org_owner(org)
    except ReadFailure as e:
        raise OwnerRefusal(org, f"{OWNER_VAR} exists but is UNREADABLE: {e}") from e
    if owner is None:
        raise OwnerRefusal(
            org,
            f"no {OWNER_VAR} read at org scope and no --map override; "
            f"the destination owner cannot be derived from the org name",
        )
    return owner


def propose(api, fg_user, fg_token, gh_token, org, gh_login):
    """Enumerate both orgs and return (rows, unpaired_fg, unpaired_gh).

    Read-only: neither forge is written. ``gh_login`` is the destination OWNER,
    taken VERBATIM from the org-scope GH_REPO_OWNER (or a --map override). Every
    composed GH_REPO carries that owner verbatim, never normalised.
    """
    c = Census(api, fg_user, fg_token, gh_token)
    fg_repos = c.forgejo_repos(org)
    gh_names = c.github_org_exists_and_names(gh_login, org)

    gh_slug_to_name = {_slug(n): n for n in gh_names}
    fg_slug_set = {_slug(n) for n in fg_repos}

    rows = []
    unpaired_fg = []
    for name, _r in sorted(fg_repos.items()):
        try:
            override = c.repo_var(org, name)
        except ReadFailure:
            override = None
        # A repo-scope GH_REPO override is the destination VERBATIM (the
        # workflow's double match compares GH_REPO's owner prefix against
        # GH_REPO_OWNER, case-sensitive). A value with a "/" is a full owner/name
        # destination, never a bare name under the org owner.
        if override and "/" in override:
            dest_owner, dest_name = override.split("/", 1)
            pairing = override
            if dest_owner == gh_login and dest_name in gh_names:
                rows.append({"org": org, "repo": name, "dest": dest_owner,
                             "dest_name": dest_name, "pairing": pairing,
                             "status": "paired", "via": "override"})
            elif dest_owner == gh_login:
                slug_match = gh_slug_to_name.get(_slug(dest_name))
                if slug_match is not None:
                    rows.append({"org": org, "repo": name, "dest": dest_owner,
                                 "dest_name": slug_match,
                                 "pairing": f"{dest_owner}/{slug_match}",
                                 "status": "drift",
                                 "note": f"forgejo '{name}' vs github '{slug_match}' (spelling)"})
                else:
                    unpaired_fg.append({"org": org, "repo": name,
                                        "pairing": pairing, "status": "unpaired"})
            else:
                rows.append({"org": org, "repo": name, "dest": dest_owner,
                             "dest_name": dest_name, "pairing": pairing,
                             "status": "paired", "via": "override"})
            continue
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


def resolve_uniqueness(rows):
    """Enforce destination uniqueness, first-come-first-served, NOT a refusal.

    Two canonical repos that would compose the SAME GH_REPO cannot both hold it:
    they would push over each other on the public mirror. Leaving the loser unset
    prevents that by construction; its mirror job then refuses LOUDLY with the
    workflow's own message. "First" is principled and deterministic:

      i.  a repo that ALREADY carries GH_REPO wins, always — a decision the
          operator already made (the apply path reports it `unchanged`);
      ii. among repos carrying none, a stable order decides: sorted org, then
          sorted repo.

    Returns (kept_rows, loss_rows) where loss_rows name the loser, its competitor
    and the shared destination, with no variable written for the loser.
    """
    # First pass: an already-carried override (via == "override") is the operator's
    # prior decision and always beats a fresh compose. Group by destination so a
    # fresh compose that collides with any override loses outright.
    override_dests = {r.get("pairing"): r for r in rows if r.get("via") == "override"}

    seen = {}
    kept = []
    losses = []
    for row in sorted(rows, key=lambda r: (r["org"], r["repo"])):
        dest = row.get("pairing") or f"{row['dest']}/{row['dest_name']}"
        is_existing = row.get("via") == "override"
        rival = override_dests.get(dest)
        if not is_existing and rival is not None and rival["repo"] != row["repo"]:
            # A fresh compose that collides with an existing override loses.
            losses.append({
                "org": row["org"], "repo": row["repo"],
                "destination": dest,
                "winner": f"{rival['org']}/{rival['repo']}",
                "reason": "destination collision; the competitor already holds "
                          "this pairing",
            })
            continue
        if dest in seen:
            prev = seen[dest]
            # Neither dominated -> sorted order (org, repo) already decided prev.
            winner, loser = prev, row
            losses.append({
                "org": loser["org"], "repo": loser["repo"],
                "destination": dest,
                "winner": f"{winner['org']}/{winner['repo']}",
                "reason": "destination collision; first-come-first-served",
            })
            continue
        seen[dest] = row
        kept.append(row)
    return kept, losses


def load_amendments(path: str) -> dict[str, str | None]:
    """Read the operator's amendment map: canonical "owner/name" -> explicit
    "dest_owner/dest_name", or null to leave deliberately unmirrored."""
    with open(path) as f:
        data = json.load(f)
    return {k: (v if v is None else str(v)) for k, v in data.items()}


def double_match_ok(owner, dest_owner):
    """The workflow's preflight double match: GH_REPO owner prefix == GH_REPO_OWNER.

    A CASE-SENSITIVE string compare, evaluated BEFORE any GitHub request. This is
    enforced at write time too, so a mismatch is caught the moment the tool would
    write it, not on the operator's next push. Returns (ok, message).
    """
    if dest_owner != owner:
        return False, (
            f"DOUBLE MATCH failed: GH_REPO owner prefix '{dest_owner}' != "
            f"GH_REPO_OWNER '{owner}' (case-sensitive)"
        )
    return True, "double match OK"


def apply_pairing(api, fg_user, fg_token, rows, org, owner, dry_run=False):
    """Write the confirmed per-repo GH_REPO variable on the FORGEJO side.

    CREATE (POST) when absent, UPDATE (PUT) when present, NO request when the
    stored value already equals the target. The double match is enforced BEFORE
    any request: a composed owner that does not equal the org's GH_REPO_OWNER is
    refused naming both values and no request is made. Never writes to GitHub,
    never writes an org-scope variable, never pushes a ref.
    """
    c = Census(api, fg_user, fg_token, "")
    outcomes = []
    for row in rows:
        repo = row["repo"]
        target = f"{row['dest']}/{row['dest_name']}"
        ok, msg = double_match_ok(owner, row["dest"])
        if not ok:
            outcomes.append((repo, "REFUSED",
                             f"{msg}; target '{target}' not written"))
            continue
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
        url = f"{api}/repos/{org}/{repo}/actions/variables/{REPO_VAR}"
        payload = {"name": REPO_VAR, "value": target}
        if current is None:
            method = "POST"
            note = "create"
        else:
            method = "PUT"
            note = "update"
        try:
            status, _ = _http_json(url, c.fg_json, method=method, payload=payload)
        except ReadFailure as e:
            outcomes.append((repo, "FAILED", f"{method} -> {e}"))
            continue
        if status in (200, 201, 204):
            outcomes.append((repo, "set", f"({note}) {current or '<unset>'} -> {target}"))
        else:
            outcomes.append((repo, "FAILED", f"{method} -> HTTP {status}"))
    return outcomes


def emit(rows, unpaired_fg, unpaired_gh, losses, org, gh_login, as_json):
    if as_json:
        blob = {"org": org, "github_owner": gh_login,
                "paired": [r for r in rows if r["status"] == "paired"],
                "drift": [r for r in rows if r["status"] == "drift"],
                "unpaired_forgejo": unpaired_fg,
                "unpaired_github": unpaired_gh,
                "collisions": losses}
        sys.stdout.write(json.dumps(blob, indent=2) + "\n")
        return
    paired = [r for r in rows if r["status"] == "paired"]
    drift = [r for r in rows if r["status"] == "drift"]
    lines = ["# Mirror pairing proposal",
             "",
             f"Forgejo org `{org}`  ->  GitHub owner `{gh_login}`",
             "",
             f"| outcome | count |",
             "|---------|-------|",
             f"| paired | {len(paired)} |",
             f"| drift (names differ) | {len(drift)} |",
             f"| unpaired on Forgejo side | {len(unpaired_fg)} |",
             f"| unpaired on GitHub side | {len(unpaired_gh)} |",
             f"| collisions (loser left unset) | {len(losses)} |",
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
    if losses:
        lines += ["## Destination collisions (loser left unset)", "",
                  "| loser | destination | winner |",
                  "|-------|-------------|--------|"]
        for L in sorted(losses, key=lambda x: (x["org"], x["repo"])):
            lines.append(f"| {L['org']}/{L['repo']} | {L['destination']} | {L['winner']} |")
        lines.append("")
    lines.append("Nothing was written. Confirm or amend, then re-run with `--apply --confirm`.")
    sys.stdout.write("\n".join(lines) + "\n")


def _selftest():
    """Decision-function proofs without touching either forge."""
    # slug normalisation folds hyphen/case/dot onto one key
    assert _slug("txt-humanizer") == _slug("txtHumanizer")
    assert _slug("env-ctl.v2") == _slug("EnvCtlV2")
    # a full owner/name override is a verbatim destination: split once on "/"
    ov = "LangeVC/skillweave"
    owner, dname = ov.split("/", 1)
    assert owner == "LangeVC" and dname == "skillweave"
    assert owner + "/" + dname == ov
    # double match is case-sensitive and refuses on a real mismatch
    ok, _ = double_match_ok("LangeVC", "LangeVC")
    assert ok
    ok, _ = double_match_ok("fusionAIze", "fusionaize")
    assert not ok
    ok, _ = double_match_ok("Capacium", "capacium")
    assert not ok
    # uniqueness: first-come-first-served, existing override wins
    rows = [
        {"org": "a", "repo": "z", "pairing": "O/z", "status": "paired", "via": "compose"},
        {"org": "a", "repo": "a", "pairing": "O/z", "status": "paired", "via": "compose"},
    ]
    kept, losses = resolve_uniqueness(rows)
    assert len(kept) == 1 and len(losses) == 1, (kept, losses)
    assert kept[0]["repo"] == "a", kept  # sorted order: 'a' before 'z'
    # existing (via override) beats a fresh compose regardless of sort order
    rows = [
        {"org": "a", "repo": "z", "pairing": "O/z", "status": "paired", "via": "override"},
        {"org": "a", "repo": "a", "pairing": "O/z", "status": "paired", "via": "compose"},
    ]
    kept, losses = resolve_uniqueness(rows)
    assert kept[0]["repo"] == "z", kept  # override wins even though 'a' sorts first
    assert losses[0]["repo"] == "a"
    assert losses[0]["winner"] == "a/z"
    # refusal construction names the org
    r = Refusal("nonexistent-org", "GitHub org does not exist (HTTP 404)")
    assert "nonexistent-org" in str(r) and "404" in str(r)
    print("selftest PASS: slug folds spelling | override is verbatim | "
          "double match is case-sensitive | uniqueness FCFS with override priority "
          "| refusal names the org")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--map", required=False, help="forgejoOrg:GitHubOwner[, ...] (override for the org variable)")
    ap.add_argument("--orgs", help="restrict canonical orgs (comma list)")
    ap.add_argument("--amend", help="JSON amendments: {\"owner/repo\": \"dest/destName\" or null}")
    ap.add_argument("--apply", action="store_true", help="apply the confirmed pairing (writes Forgejo reps)")
    ap.add_argument("--confirm", action="store_true", help="required with --apply")
    ap.add_argument("--dry-run", action="store_true", help="with --apply: print intended writes, write nothing")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if a.apply and not a.confirm:
        print("mirror-destination-propose: ERROR: --apply requires --confirm "
              "(nothing is applied without the operator's confirmation)", file=sys.stderr)
        return 2
    override_map = dict(p.split(":", 1) for p in (a.map or "").split(",") if ":" in p)
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

    # Discover the canonical orgs: from --map if supplied, else all Forgejo orgs
    # readable under the credential. The owner for each is the org variable (with
    # --map as the explicit override).
    if not a.map:
        status, body = _http_json(f"{api}/user/orgs", _basic(fg_user, fg_token))
        if status != 200:
            print(f"mirror-destination-propose: ERROR: cannot list orgs: HTTP {status}",
                  file=sys.stderr)
            return 2
        org_names = sorted(o["username"] for o in json.loads(body))
    else:
        org_names = sorted(override_map.keys())

    exit_code = 0
    results = []
    for org in org_names:
        if orgs_only and org not in orgs_only:
            continue
        try:
            owner = read_owner(api, fg_user, fg_token, org, override_map)
        except OwnerRefusal as e:
            print(f"mirror-destination-propose: REFUSED {e}", file=sys.stderr)
            exit_code = 1
            continue
        try:
            rows, unpaired_fg, unpaired_gh = propose(api, fg_user, fg_token, gh_token, org, owner)
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
                    continue
                ow, nm = val.split("/", 1)
                final_rows.append({**r, "dest": ow, "dest_name": nm,
                                   "pairing": val, "status": "paired",
                                   "via": "amended"})
            else:
                final_rows.append(r)

        kept_rows, losses = resolve_uniqueness(final_rows)

        if not a.apply:
            emit(kept_rows, unpaired_fg, unpaired_gh, losses, org, owner, a.json)
            continue

        applyable = [r for r in kept_rows if r["status"] == "paired"]
        outcomes = apply_pairing(api, fg_user, fg_token, applyable, org, owner,
                                 dry_run=a.dry_run)
        changed = 0
        for repo, state, note in outcomes:
            if state in ("set", "would-set"):
                changed += 1
            print(f"  {state:9} {org}/{repo}  {note}", file=sys.stderr)
        for L in losses:
            print(f"  collision  {L['org']}/{L['repo']}  left unset; winner {L['winner']} "
                  f"for {L['destination']}", file=sys.stderr)
        print(f"apply {org}: {len(outcomes)} repos, {changed} changed, "
              f"{len(outcomes) - changed} unchanged/unmeasured/refused")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
