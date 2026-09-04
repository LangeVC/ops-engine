#!/usr/bin/env python3
"""mirror-destination-propose - propose each repository's mirror pairing from
its config destinations, and reconcile the deprecated Actions-variable mirror
workflow only after the operator confirms.

OME-011 / DST-004. After DST-001/002/003 the mirror destination is declared by
the Layover-2 config (``mirror.github`` -> :class:`Destination`, Layer-3
``.ops.yaml``) and resolved by the Layer-1 pure resolver
``resolve_destinations``. This tool therefore takes the org set as repeated
``--config PATH`` arguments and READS each repository's destination from its
config -- not from Forgejo Actions variables. There is no registry, no
discovery, no ``/user/orgs`` walk, and no org-scope ``GH_REPO_OWNER`` /
repo-scope ``GH_REPO`` Actions-variable read on the primary (propose) path.

What propose emits is the config-declared mirror pairing for each repository the
operator hands over: which github destination that repo's config resolves to.
An owner that declares a github destination whose ``repo`` is not an
``owner/name`` form is a REFUSAL naming the destination; a repo whose config
resolves no github destinations is not a config-charbound mirror and is listed
as "no github destination declared".

WHY A WRITE PATH REMAINS (DEC-003): the live ``.forgejo/workflows/mirror.yml``
files still read ``vars.*`` (the org-scope ``GH_REPO_OWNER`` and repo-scope
``GH_REPO`` pair) until they are migrated onto the config destination. Until
then the workflow's preflight must not refuse the repositories the config now
declares. ``--apply --confirm`` reconciles those legacy variables onto the
config-declared destination, so the config remains the single source of truth
and the Actions variable is only a compatibility sink for the still-variable
workflow. The variable *write* therefore survives to 4.0.0 (DEC-003); the
Actions variable is never the destination SOURCE.

CONFIRMATION GATE (nothing applied without it):

    propose    (default)    write NOTHING. Print, per config-declared repo, the
                            github mirror destination the config resolves.
    apply       with --confirm + --dry-run is allowed by default unless --confirm
    apply       apply the CONFIRMED pairing: for each config-declared github
                            destination whose repo does not yet carry a matching
                            repo-scope variable, set it. Refuses without
                            --confirm.

The apply path writes, per repository, the repo-scope GH_REPO variable on the
FORGEJO side (a compatibility sink for the still-variable workflow) to the value
the config declares -- never GitHub, never an org-scope variable. CREATE when
absent, UPDATE when present, NO request when already equal.

RED PROOF: a config github destination that is not an owner/name form (e.g. a
URL without an ``owner/`` prefix) is REFUSED, naming the destination, rather
than proposed.

Stdlib only (plus this package's own config/resolver modules, already runtime
dependencies).

Environment:
  FORGEJO_API    base  (default https://git.langevc.com/api/v1)  -- apply only
  FORGEJO_USER   (default typelicious)                            -- apply only
  FORGEJO_TOKEN  HTTP Basic password / api token (canonical forge) -- apply only

Usage:
    mirror-destination-propose.py --config ../lvc-ops/config.yml \\
        --config ../../capacium/capacium-ops/config.yml [--json]
    mirror-destination-propose.py --config <cfg> --apply --confirm [--dry-run]
    mirror-destination-propose.py --selftest
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
import yaml  # noqa: E402
from ops_engine.config_loader import OrgConfig, OpsEngineConfig  # noqa: E402
from ops_engine.modules.mirror import resolve_destinations  # noqa: E402

FORGEJO_API_DEFAULT = "https://git.langevc.com/api/v1"
UA = "ops-engine/mirror-destination-propose (DST-004)"

# The repo-scope Actions variable that the still-variable mirror.yml reads. This
# is a compatibility SINK on the apply path only; the destination SOURCE is the
# config. Named as a literal (it is no longer imported from anywhere).
REPO_VAR = "GH_REPO"

_NON_ORG_KEYS = {
    "image", "services", "ingress", "env", "migrations", "ssl", "secret",
    "health_monitor", "auto_triage", "stale_management", "notifications",
}


class Refusal(Exception):
    """A proposal is refused on principle, naming the destination."""

    def __init__(self, org, repo, reason):
        self.org = org
        self.repo = repo
        self.reason = reason
        super().__init__(f"REFUSED: {org}/{repo}: {reason}")


class OwnerRefusal(Refusal):
    """A config github destination that is neither an owner/name nor resolvable."""


def is_org_root(key: str, value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if key in _NON_ORG_KEYS:
        return False
    if "repositories" in value:
        return True
    return any(s in value for s in ("forgejo", "github", "auto_merge", "release"))


def org_roots(path: str):
    """Yield (org, OrgConfig) org roots declared by one layover config file."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError as e:
        raise SystemExit(f"mirror-destination-propose: ERROR: cannot read {path}: {e}")
    except yaml.YAMLError as e:
        raise SystemExit(f"mirror-destination-propose: ERROR: cannot parse {path}: {e}")
    if not isinstance(raw, dict):
        raise SystemExit(f"mirror-destination-propose: ERROR: {path} is not a mapping")
    if isinstance(raw.get("orgs"), dict):
        candidates = {k: v for k, v in raw["orgs"].items() if isinstance(v, dict)}
    else:
        candidates = {k: v for k, v in raw.items() if is_org_root(k, v)}
    roots = []
    for org, section in candidates.items():
        try:
            oc = OrgConfig.model_validate(section)
        except Exception:
            oc = None
        if oc is not None:
            roots.append((org, oc))
    if not roots:
        raise SystemExit(
            f"mirror-destination-propose: ERROR: no org root found in {path}")
    return roots


def github_mirror_destination(cfg, org: str, repo: str):
    """The github destination(s) a repo's config resolves.

    Returns (dests, note): dests is the list of ``forge==github`` destinations;
    note is a one-line symptom when the config could not be resolved.
    """
    try:
        dests = resolve_destinations(cfg, f"{org}/{repo}")
    except Exception as e:  # noqa: BLE001 - surfaced as the note, never silent
        return [], f"could not resolve: {e}"
    return [d for d in dests if d.forge == "github"], ""


def double_match_ok(owner, dest_owner):
    """The workflow preflight double match on the config-declared destination.

    A config github destination is a single ``owner/name`` field whose owner IS
    its prefix, so it is self-consistent by construction. This documents the
    exact check the legacy workflow applies when the destination is pushed as
    the repo-scope variable against the org-scope owner: both must agree
    case-sensitively. Returns (ok, message).
    """
    if dest_owner != owner:
        return False, (
            f"DOUBLE MATCH failed: destination owner prefix '{dest_owner}' != "
            f"org owner '{owner}' (case-sensitive)"
        )
    return True, "double match OK"


def propose_from_config(path: str):
    """Return (org, rows) proposals for one config file, read-only."""
    out = []
    for org, org_config in org_roots(path):
        cfg = OpsEngineConfig.load({"orgs": {org: org_config.model_dump()}})
        for repo_name in sorted(org_config.repositories):
            dests, note = github_mirror_destination(cfg, org, repo_name)
            if note:
                out.append({"org": org, "repo": repo_name, "status": "unresolved",
                            "note": note, "pairing": None})
                continue
            if not dests:
                out.append({"org": org, "repo": repo_name, "status": "no-github-dest",
                            "note": "no github destination declared by config",
                            "pairing": None})
                continue
            # one repository, one proposed github pairing (first declared wins);
            # a malformed destination is a named refusal before any GitHub work.
            d = dests[0]
            owner, sep, name = d.repo.partition("/")
            if not sep or not name:
                out.append({"org": org, "repo": repo_name, "status": "refused",
                            "note": f"config github destination '{d.repo}' is not "
                                    f"an owner/name form",
                            "pairing": None})
                continue
            out.append({"org": org, "repo": repo_name, "status": "paired",
                        "dest": d.repo, "pairing": d.repo, "via": "config",
                        "note": ""})
    return out


# ── deprecated apply (compatibility sink; see module docstring) ─────────────

class CensusForge:
    """Read/write view of the canonical Forgejo Forgejo org that owns the
    repositories (used only on the apply path to reconcile the deprecated
    repo-scope variable onto the config-declared destination)."""

    def __init__(self, api, fg_user, fg_token):
        self.api = api
        self.headers = {
            "Authorization": "Basic " + base64.b64encode(
                f"{fg_user}:{fg_token}".encode()).decode(),
            "Content-Type": "application/json",
            "User-Agent": UA,
        }

    def repo_var(self, org, repo):
        import urllib.request
        import urllib.error
        url = f"{self.api}/repos/{org}/{repo}/actions/variables/{REPO_VAR}"
        req = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status != 200:
                    return None
                d = json.loads(r.read())
                return d.get("data") if isinstance(d, dict) else None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        except Exception:  # noqa: BLE001
            return None


def apply_pairing(api, fg_user, fg_token, rows, org, owner, dry_run=False):
    """Reconcile the deprecated repo-scope variable onto the config destination."""
    import urllib.request
    import urllib.error

    fg = CensusForge(api, fg_user, fg_token)
    outcomes = []
    paired = [r for r in rows if r.get("status") == "paired" and r.get("owner") == org]
    for row in paired:
        repo = row["repo"]
        repo_owner, dest_name = row["pairing"].split("/", 1)
        ok, msg = double_match_ok(owner, repo_owner)
        if not ok:
            outcomes.append((repo, "REFUSED", f"{msg}; not written"))
            continue
        target = row["pairing"]
        try:
            current = fg.repo_var(org, repo)
        except urllib.error.HTTPError as e:
            outcomes.append((repo, "UNMEASURED", f"var read HTTP {e.code}"))
            continue
        if current == target:
            outcomes.append((repo, "unchanged", f"already {target}"))
            continue
        if dry_run:
            outcomes.append((repo, "would-set", f"{current or '<unset>'} -> {target}"))
            continue
        url = f"{api}/repos/{org}/{repo}/actions/variables/{REPO_VAR}"
        method = "PUT" if current is not None else "POST"
        payload = {"name": REPO_VAR, "value": target}.encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, data=payload, headers=fg.headers, method=method), timeout=30) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:  # noqa: BLE001
            outcomes.append((repo, "FAILED", str(e)))
            continue
        if code in (200, 201, 204):
            outcomes.append((repo, "set", f"({('update' if current is not None else 'create')}) "
                                          f"{current or '<unset>'} -> {target}"))
        else:
            outcomes.append((repo, "FAILED", f"HTTP {code}"))
    return outcomes


def emit(rows, as_json):
    if as_json:
        sys.stdout.write(json.dumps({"rows": rows}, indent=2) + "\n")
        return
    lines = ["# Mirror pairing proposal (config-first)", ""]
    paired = [r for r in rows if r.get("status") == "paired"]
    none_ = [r for r in rows if r.get("status") == "no-github-dest"]
    refused = [r for r in rows if r.get("status") == "refused"]
    unresolved = [r for r in rows if r.get("status") == "unresolved"]
    lines += [
        "| outcome | count |", "|---------|-------|",
        f"| config github pairing | {len(paired)} |",
        f"| no github destination declared | {len(none_)} |",
        f"| refused (malformed destination) | {len(refused)} |",
        f"| unresolved (config read) | {len(unresolved)} |",
        "",
    ]
    if paired:
        lines += ["## Config-declared github mirror pairing", "",
                  "| org | repo | destination (from config) |",
                  "|-----|------|---------------------------"]
        for r in sorted(paired, key=lambda x: (x["org"], x["repo"])):
            lines.append(f"| {r['org']} | {r['repo']} | {r['pairing']} |")
        lines.append("")
    if none_:
        lines += ["## No github destination declared (not a config mirror)", ""]
        for r in sorted(none_, key=lambda x: (x["org"], x["repo"])):
            lines.append(f"- {r['org']}/{r['repo']}")
        lines.append("")
    if refused:
        lines += ["## Refused (malformed destination)", ""]
        for r in sorted(refused, key=lambda x: (x["org"], x["repo"])):
            lines.append(f"- {r['org']}/{r['repo']}: {r['note']}")
        lines.append("")
    if unresolved:
        lines += ["## Unresolved config reads", ""]
        for r in sorted(unresolved, key=lambda x: (x["org"], x["repo"])):
            lines.append(f"- {r['org']}/{r['repo']}: {r['note']}")
        lines.append("")
    lines.append("Nothing was written without --apply --confirm. Re-run with "
                 "--apply --confirm to reconcile the deprecated repo-scope "
                 "variable onto a config-declared destination.")
    sys.stdout.write("\n".join(lines) + "\n")


def _selftest():
    """Decision proofs without touching any forge or config file."""
    # the double match on a config destination is owner-prefix vs org owner
    ok, _ = double_match_ok("LangeVC", "LangeVC")
    assert ok
    ok, _ = double_match_ok("fusionAIze", "fusionaize")
    assert not ok
    # is_org_root recognises an org holder by repositories, repels server keys
    assert is_org_root("langevc", {"repositories": {"a": {}}})
    assert not is_org_root("image", {"repository": "x"})
    print("selftest PASS: config double match is case-sensitive | org-root "
          "recognition keeps repositories, repels server keys")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", action="append", default=[],
                    help="Layover config.yml path (repeatable)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if a.apply and not a.confirm:
        print("mirror-destination-propose: ERROR: --apply requires --confirm",
              file=sys.stderr)
        return 2
    if not a.config:
        print("mirror-destination-propose: ERROR: need at least one --config PATH "
              "(the org set arrives at the call site; no registry, no discovery)",
              file=sys.stderr)
        return 2

    rows = []
    any_err = False
    for path in a.config:
        try:
            rows.extend(propose_from_config(path))
        except SystemExit as e:
            print(e, file=sys.stderr)
            any_err = True
    if not rows and not any_err:
        print("mirror-destination-propose: ERROR: no config-declared "
              "repositories found", file=sys.stderr)
        return 2
    if any_err and not rows:
        return 2

    if not a.apply:
        emit(rows, a.json)
        return 0

    # Apply: reconcile the deprecated repo-scope variable (per org present in
    # the configs) onto the config-declared destination. Applies are Forgejo
    # writes and are grouped by the org the repo belongs to.
    fg_token = os.environ.get("FORGEJO_TOKEN")
    fg_user = os.environ.get("FORGEJO_USER", "typelicious")
    api = os.environ.get("FORGEJO_API", FORGEJO_API_DEFAULT)
    if not fg_token:
        print("mirror-destination-propose: ERROR: apply needs FORGEJO_TOKEN",
              file=sys.stderr)
        return 2
    orgs = sorted({r["org"] for r in rows if r.get("owner")})
    # reconcile per org; the org owner equals the repo owner of the pairing
    for org in orgs:
        example_owner = next(r["owner"] for r in rows if r.get("org") == org and r.get("owner"))
        outcomes = apply_pairing(api, fg_user, fg_token, rows, org, example_owner,
                                 dry_run=a.dry_run)
        changed = 0
        for repo, state, note in outcomes:
            if state in ("set", "would-set"):
                changed += 1
            print(f"  {state:9} {org}/{repo}  {note}", file=sys.stderr)
        print(f"apply {org}: {len(outcomes)} repos, {changed} changed",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
