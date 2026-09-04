#!/usr/bin/env python3
"""mirror-destination-audit - classify every config-declared repository by the
mirror destinations its config resolves to, against live GitHub.

DST-004. After DST-001/002/003 the mirror destination is declared by the
Layover-2 config (``mirror.github`` -> :class:`Destination`, with an optional
Layer-3 ``.ops.yaml``) and resolved by the Layer-1 pure resolver
``resolve_destinations`` -- NOT read from Forgejo Actions variables. This audit
is an OPERATOR act: the org set is supplied at the call site as repeated
``--config PATH`` arguments. There is no registry, no discovery, no ``/user/orgs``
walk, and no org-scope ``GH_REPO_OWNER`` / repo-scope ``GH_REPO`` Actions-variable
read anywhere on the primary path.

The mirror destination of a repository is whatever its resolved config says it
is. Each repository the operator names (by handing over the layover config that
declares it) is placed in exactly one of five classes, in the destination
model's own refusal order:

  1. declared and reachable    - config declares a github mirror destination and
                                 GitHub carries the exact name
  2. declared but unreachable  - config declares a github mirror destination but
                                 GitHub lacks the name
  3. declares a name variant   - the config destination is a spelling/name drift
                                 of a name GitHub carries under that owner
  4. no github mirror declared - the config resolves no github mirror
                                 destination for this repo (the deliberate, or
                                 as-yet-unmigrated, case)
  5. refused before any GitHub request - a config github destination that is not
                                 an ``owner/name`` form

Class 5 is the model's own refusal: a github destination whose ``repo`` names no
``owner/name`` cannot be a github mirror and is refused before any GitHub
request. Class 4 rests solely on the CONFIG: a repo the config resolves with no
github mirror destination. It is never inferred from a live forge enumeration.

Only class 1/2/3 read GitHub. Reachability reads GitHub through ``GITHUB_TOKEN``
or the authenticated ``gh`` CLI; Forgejo Actions variables are never read. A
GitHub read that fails for a reason other than a genuine 404 is reported
UNMEASURED, never folded into "declared but unreachable".

The script changes neither forge: the GitHub side is read-only, and it writes
nowhere except its own report (stdout or --out).

Environment:
  GITHUB_TOKEN  Bearer to api.github.com with read scope on the mirror orgs
                 (optional if ``gh`` is authenticated)

Usage:
    mirror-destination-audit.py --config ../lvc-ops/config.yml \\
        --config ../../capacium/capacium-ops/config.yml [--out PATH] [--json]
    mirror-destination-audit.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

# ops-engine's src/ must be on the path (the same layout pytest uses via
# pythonpath = ["src"]). The config models and the Layer-1 pure resolver
# ``resolve_destinations`` live in the package; the resolver is the ONLY source
# of a repository's mirror destination here.
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
import yaml  # noqa: E402
from ops_engine.config_loader import OrgConfig, OpsEngineConfig  # noqa: E402
from ops_engine.modules.mirror import resolve_destinations  # noqa: E402

GITHUB_API = "https://api.github.com"
UA = "ops-engine/mirror-destination-audit (DST-004)"

# Top-level layover keys that are NOT org roots even though they carry a dict.
_NON_ORG_KEYS = {
    "image", "services", "ingress", "env", "migrations", "ssl", "secret",
    "health_monitor", "auto_triage", "stale_management", "notifications",
}


class ReadFailure(Exception):
    """A transport-level GitHub read failed (network / timeout / transport)."""


def _http_get_json(url, headers):
    """GET url returning (status, parsed); raises ReadFailure only on
    transport failure. HTTP statuses (incl. 404) are returned to the caller."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        raise ReadFailure(f"GET {url} -> {e.reason!r}") from e
    except TimeoutError as e:
        raise ReadFailure(f"GET {url} -> timeout") from e
    if not body:
        return 200, None
    try:
        return r.status, json.loads(body)
    except (ValueError, AttributeError):
        # r may be unbound when urlopen raised; a successful read returns here.
        return 200, None


def _gh_token() -> str:
    """A GitHub read token: GITHUB_TOKEN env, else the authenticated ``gh`` CLI."""
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok.strip()
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], check=True, capture_output=True,
            text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


class GitHubReader:
    """Read-only reachability view of api.github.com for mirror destinations."""

    def __init__(self, token: str):
        self.token = token
        base = {
            "Accept": "application/vnd.github+json",
            "User-Agent": UA,
        }
        if token:
            base["Authorization"] = f"Bearer {token}"
        self.headers = base

    def repo_status(self, full_name: str) -> bool | None:
        """True = reachable, False = truly absent (404), None = UNMEASURED."""
        code, data = _http_get_json(f"{GITHUB_API}/repos/{full_name}", self.headers)
        if code == 404:
            return False
        if code != 200 or not isinstance(data, dict):
            return None
        return full_name.split("/")[1] in {data.get("name")} and bool(data.get("full_name"))

    def owner_repo_names(self, owner: str) -> set[str] | None:
        """Enum repo names under a GitHub owner; None on a failed read."""
        names: set[str] = set()
        page = 1
        try:
            while True:
                code, data = _http_get_json(
                    f"{GITHUB_API}/users/{owner}/repos?per_page=100&page={page}",
                    self.headers,
                )
                if code != 200 or not isinstance(data, list):
                    if code == 404:
                        return None
                    return None
                if not data:
                    break
                names.update(r.get("name") for r in data if isinstance(r, dict) and r.get("name"))
                if len(data) < 100:
                    break
                page += 1
        except ReadFailure:
            return None
        return names


def _slug(name: str) -> str:
    """Normalise a slug so case/hyphen/dot drift folds onto one key.

    Slugging is FORBIDDEN where a real contract decision is made; it exists only
    to recognise class-3 name drift AFTER reachability told us the exact name is
    absent. The declared destination is used verbatim beyond reachability.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def split_owner(dest: str) -> tuple[str, str]:
    """Split a destination into (owner, name); name empty when no ``/``."""
    owner, sep, name = dest.partition("/")
    return owner, (name if sep else "")


def is_org_root(key: str, value: object) -> bool:
    """A top-level layover section is an org root iff it carries the org/repo
    config shape (a ``repositories`` mapping is the decisive signal)."""
    if not isinstance(value, dict):
        return False
    if key in _NON_ORG_KEYS:
        return False
    if "repositories" in value:
        return True
    # An org root may legitimately have no declared repositories (pure-defaults
    # org), but only when it carries org-only sections.
    return any(s in value for s in ("forgejo", "github", "auto_merge", "release"))


def load_config(path: str):
    """Yield (org, OrgConfig) roots declared by one layover config file."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError as e:
        raise SystemExit(f"mirror-destination-audit: ERROR: cannot read {path}: {e}")
    except yaml.YAMLError as e:
        raise SystemExit(f"mirror-destination-audit: ERROR: cannot parse {path}: {e}")
    if not isinstance(raw, dict):
        raise SystemExit(
            f"mirror-destination-audit: ERROR: {path} is not a config mapping")
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
            f"mirror-destination-audit: ERROR: no org root found in {path}")
    return roots


def classify_declared(dest: str, gh: GitHubReader):
    """Class ONE config-declared github destination against live GitHub.

    Returns ``(cls, detail)``. A github read that failed is UNMEASURED, never an
    invented reachability.
    """
    owner, name = split_owner(dest)
    dest = dest.strip()
    if not name or not owner:
        return "5", (
            f"config declares github destination '{dest}' which is not an "
            f"'owner/name' form; refused before any GitHub request"
        )
    present = gh.repo_status(dest)
    if present is None:
        return "UNMEASURED", f"could not read GitHub for {dest}"
    if present:
        return "1", dest
    owner_names = gh.owner_repo_names(owner)
    if owner_names is None:
        return "UNMEASURED", f"could not read GitHub owner '{owner}'"
    matches = [n for n in owner_names if n and _slug(n) == _slug(name)]
    if matches:
        actual = sorted(matches)[0]
        return "3", f"{owner}/{actual} (config '{dest}' vs github '{actual}')"
    return "2", f"{dest} (absent/unreachable on github)"


def census_from_config(path: str, gh: GitHubReader) -> list[dict]:
    """Classify every repository a config file declares."""
    roots = load_config(path)
    rows = []
    for org, org_config in roots:
        # OpsEngineConfig's shape is { orgs: {<org>: OrgConfig} }; a single
        # layover config's org root fills one element of that mapping.
        cfg = OpsEngineConfig.load({"orgs": {org: org_config.model_dump()}})
        for repo_name in sorted(org_config.repositories):
            try:
                dests = resolve_destinations(cfg, f"{org}/{repo_name}")
            except Exception as e:  # noqa: BLE001 - a named refusal, not a crash
                rows.append({"org": org, "repo": repo_name, "class": "UNMEASURED",
                             "src": path, "dest_or_note": f"could not resolve: {e}"})
                continue
            gh_dests = [d for d in dests if d.forge == "github"]
            if not gh_dests:
                rows.append({"org": org, "repo": repo_name, "class": "4",
                             "src": path,
                             "dest_or_note": "no github destination declared by "
                                             "config (not a config github mirror)"})
                continue
            # Any github destination is judged by reachability; a mirror role
            # and a release role are both published to github, so both are
            # classed the same way the class text promises ("declared").
            for d in gh_dests:
                cls, detail = classify_declared(d.repo, gh)
                rows.append({"org": org, "repo": repo_name, "class": cls,
                             "src": path, "dest_or_note": detail, "dest": d.repo})
    return rows


def emit(rows, as_json, out):
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
        header = ["# Mirror destination audit (config-first)", "",
                  f"Config-declared repositories in census: **{len(rows)}**"
                  + (f" (of which {unmeasured} UNMEASURED)" if unmeasured else ""),
                  "",
                  "Classes: ",
                  "  1 declared & reachable",
                  "  2 declared & unreachable",
                  "  3 name drift (declared name is a variant github carries)",
                  "  4 no github destination declared by config",
                  "  5 refused before any GitHub request (not owner/name)",
                  "", "## Counts per class", "| class | count |", "|-------|-------|"]
        for c in ("1", "2", "3", "4", "5"):
            header.append(f"| {c} | {class_count.get(c, 0)} |")
        if unmeasured:
            header.append(f"| UNMEASURED | {unmeasured} |")
        show_um = unmeasured > 0
        lines = header + ["", "## Per org (class counts)", "",
                          "| org | 1 | 2 | 3 | 4 | 5" + (" | UM |" if show_um else " |"),
                          "|-----|---|---|---|---|---" + ("|----|" if show_um else "|")]
        for o in sorted(org_counts):
            c = org_counts[o]
            cells = [o, str(c.get('1', 0)), str(c.get('2', 0)), str(c.get('3', 0)),
                     str(c.get('4', 0)), str(c.get('5', 0))]
            if show_um:
                cells.append(str(c.get("UNMEASURED", 0)))
            lines.append("| " + " | ".join(cells) + " |")
        lines += ["", "| org | repo | class | destination / note |",
                  "|-----|------|-------|----------------------|"]
        for r in sorted(rows, key=lambda x: (x["org"], x["repo"])):
            note = r["dest_or_note"]
            if r.get("dest"):
                note = f"{note}"
            lines.append(f"| {r['org']} | {r['repo']} | {r['class']} | {note} |")
        text = "\n".join(lines) + "\n"
    if out:
        with open(out, "w") as f:
            f.write(text)
        print(f"wrote {out}", file=sys.stderr)
    else:
        sys.stdout.write(text)


def _selftest():
    """Decision-function proofs (no forge is touched)."""
    # split_owner recognises an owner/name destination vs a malformed one
    o, n = split_owner("LangeVC/ops-engine")
    assert o == "LangeVC" and n == "ops-engine"
    o, n = split_owner("no-slash-owner")
    assert o == "no-slash-owner" and not n
    # _slug folds the documented drift specimen
    assert _slug("txt-humanizer") == _slug("txtHumanizer")
    # a malformed config destination is class 5 and never touches the network

    class _NoNetReader:
        def repo_status(self, f):
            raise AssertionError("class-5 refusal must not reach GitHub")
        def owner_repo_names(self, o):
            raise AssertionError("class-5 refusal must not reach GitHub")

    cls, _ = classify_declared("not-a-destination", _NoNetReader())
    assert cls == "5", cls
    print("selftest PASS: owner/name split | slug drift fold | malformed github "
          "destination refused (class 5) before any GitHub request")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", action="append", default=[],
                    help="Layover config.yml path (repeatable); the org set is "
                         "supplied here, never discovered")
    ap.add_argument("--orgs", help="restrict canonical orgs (comma list)")
    ap.add_argument("--out", help="write markdown report to PATH")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if not a.config:
        print("mirror-destination-audit: ERROR: need at least one --config PATH "
              "(the org set arrives at the call site; no registry, no discovery)",
              file=sys.stderr)
        return 2

    token = _gh_token()
    if not token:
        print("mirror-destination-audit: ERROR: need a GitHub read credential "
              "(GITHUB_TOKEN, or an authenticated `gh`) for the reachability "
              "reads", file=sys.stderr)
        return 2
    gh = GitHubReader(token)

    rows = []
    any_err = False
    for path in a.config:
        try:
            rows.extend(census_from_config(path, gh))
        except SystemExit as e:
            print(e, file=sys.stderr)
            any_err = True
    if (any_err and not rows) or (not rows and not any_err):
        if not rows:
            print("mirror-destination-audit: ERROR: no config-declared "
                  "repositories found", file=sys.stderr)
            return 2
    if a.orgs:
        orgs_only = set(a.orgs.split(","))
        rows = [r for r in rows if r["org"] in orgs_only]
    emit(rows, a.json, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
