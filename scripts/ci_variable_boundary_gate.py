#!/usr/bin/env python3
"""ADP-009 — refuse a forge destination that bypasses the config layer.

A release or mirror workflow under ``.forgejo/`` reaches a forge repository or a
forge API host. Where that destination *comes from* decides whether it is inside
the config layer or outside it. This gate refuses, across every workflow file it
is pointed at, the two shapes that took the destination OUT of the config layer:

* *a user-defined CI variable* — a reference to ``vars.<NAME>`` (repo-/org-level
  Forgejo Actions *variables*). ``vars`` is the CI system's user variable store;
  a destination carried there is not committed, is not reviewable, and is not a
  single source of truth. The config layer carries destinations instead
  (``.ops.yaml`` read through ``ops_engine.config_loader.load_ops_yaml``).

* *a hardcoded repository or API host* — a literal ``HOST/OWNER/REPO`` (with or
  without a trailing ``.git``) in http(s) or scp form, or a forge API host
  (``api.github.com`` / ``uploads.github.com``) named by value, instead of the
  destination being read from the config layer.

The shapes are real history, not invented examples: the pre-ADP-004 release
workflow set ``GH_API="https://api.github.com"`` and ``GH_REPO="LangeVC/ops-engine"``
as two literals and POSTed the GitHub release object at
``${GH_API}/repos/${GH_REPO}/releases`` (the original instance this gate exists
to stop recurring); the pre-ADP-008 release workflow rendered the destinations
into ``vars.RELEASE_DESTINATIONS`` and read it back out; and the mirror workflow
hardcoded ``github.com/LangeVC/ops-engine`` as its push remote. All looked
reasonable and all bypassed the config layer.

What is NOT refused — these are separate from the two shapes:

* *secrets are not destinations.* ``secrets.<NAME>`` carries a credential (a
  token), never the repository it authenticates to. A token must stay in the
  credential store; only the repository it pushes to moves into the config
  layer. Forgejo-provided event context — ``github.repository``,
  ``github.ref_name``, ``github.server_url`` — is the *event*, not a user
  variable store, and is permitted. ``github.*`` identifies the run; ``vars.*``
  carries user-managed data.

* *comment text is documentation, never a destination.* A forge host or
  repository that appears only in a YAML comment is a note about the workflow,
  not a reach the workflow executes, so it is skipped — exactly as the
  ``mirror.yml`` resolver skips ``#`` comment lines. The scan removes the
  comment portion of each line (from a ``#`` that is outside a ``${{ }}``
  expression or a quoted scalar and is at the start of the line or preceded by
  whitespace) before any rule runs. A destination in an executable position —
  inside ``${{ }}``, inside a quoted URL, or an unquoted literal — is still
  scanned even on a line that also carries a comment, so skipping a comment can
  never hide a live destination.

* *tool-fetch suppliers.* A destination workflow still downloads tooling from a
  supply host (``actions/checkout`` fetches a released action, the OSV scanner
  fetches a released binary). Those are bytes the build consumes, not a release
  or mirror it produces, so the handful of explicit suppliers are whitelisted.
  The whitelist is committed here, exactly like the organisation-vocabulary
  allowlist in the release-notes-audience gate (REL-011): ops-engine ships the
  bare set of suppliers its own workflows use, and an adopting layover widens it
  only by editing this same list.

The gate is stdlib-only and never imports ``ops_engine`` (REL-006): the bare
runner carries neither yaml nor pydantic, so nothing outside the standard
library may execute here, and the gate's tests import nothing outside it either.

Usage::

    ci_variable_boundary_gate.py [--dir PATH] [WORKFLOW ...]

With one or more ``WORKFLOW`` paths, check only those files. With ``--dir``,
scan every ``.yml``/``.yaml`` file below that directory. With neither, scan the
``.forgejo/workflows`` directory of the current working directory if it exists.

Every refusal names the file, the 1-based line, and the offending token. Exit
codes: ``0`` no offence anywhere, ``1`` at least one offence, ``2`` usage /
read error.
"""

import argparse
import re
import sys
from pathlib import Path

# Hosts whose ``OWNER/REPO`` is a forge **destination** this kind of workflow is
# prohibited from naming as a literal. github.com covers the mirror's git remote
# and any github.com/OWNER/REPO fetch that is not a whitelisted supplier;
# gitlab.com, codeberg.org, git.sr.ht and the Forgejo host cover the shape a
# layover that later points a second (web/git-form) destination would write, so
# the refusal is explicit rather than a github-only special case. GitLab's API
# rides on gitlab.com itself and Forgejo's on its own host, so their API paths
# are refused through this same set; only github.com keeps its REST/upload
# endpoints on dedicated subdomains, which is why ``api.github.com`` and
# ``uploads.github.com`` are a separate, host-name-only refusal (below).
_DESTINATION_HOSTS = (
    "github.com",
    "www.github.com",
    "gitlab.com",
    "codeberg.org",
    "git.sr.ht",
    "git.langevc.com",
)

# Forge **API** hosts, refused by host name alone. Naming an API host by value is
# the half of an API destination that carries the forge identity: the pre-ADP-004
# release workflow set ``GH_API="https://api.github.com"`` and relied on the
# upload URL ``https://uploads.github.com/repos/...`` — neither line carries an
# OWNER/REPO of its own, so no HOST/OWNER/REPO rule can see them. The repository
# half travelled beside them (``GH_REPO="LangeVC/ops-engine"``), and the two were
# concatenated into the request. Refusing the host names outright catches that
# split-destination shape at its forge-identity end.
_API_HOSTS = ("api.github.com", "uploads.github.com")
_API_HOST_REF = re.compile("|".join(re.escape(h) for h in _API_HOSTS))

# Tool-fetch suppliers a destination workflow legitimately downloads tool bytes
# from, named as ``(host, owner, repo)``. They are **not** a destination the
# workflow produces a release/mirror toward; they are the source of a released
# tool. ``actions/checkout`` fetches a released action; ``google/osv-scanner``
# fetches a released scanner binary. A literal whose ``host/owner/repo`` equals
# one of these suppliers is a whitelisted tool fetch, never a destination. An
# adopting layover widens the set only by editing this same committed list.
_TOOL_SUPPLIERS = (("github.com", "actions", "checkout"),
                   ("github.com", "google", "osv-scanner"))

# A user-defined CI variable reference: ``vars.NAME`` inside ``${{ }}`` or bare.
_VARS_REF = re.compile(r"\bvars\.[A-Za-z_][A-Za-z0-9_]*")

# A literal ``HOST/OWNER/REPO`` destination path, optionally ``.git``-suffixed,
# as it is written in a push remote or a URL that targets a forge repository.
_DEST_PATH = re.compile(
    r"(?P<host>[A-Za-z0-9._-]+\.[A-Za-z]{2,})(?:[:0-9]+)?/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?(?=[/\"'#?]|\s|\\|$)"
)

# The scp-form git remote, ``git@HOST:OWNER/REPO[.git]``. The destination is the
# same repository literal as the http(s) form; only the transport prefix differs
# (``git@`` and a ``:`` instead of a ``/`` after the host), so the HOST/OWNER/REPO
# rule above never sees it. The optional ``.git`` is folded into the token.
_SCP_PATH = re.compile(
    r"(?<![\w])git@(?P<host>[A-Za-z0-9._-]+\.[A-Za-z]{2,}):"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?(?=[/\"'#?\s\\]|$)"
)


def _scan_line(line):
    """Return the executable portion of one workflow line, comment text removed.

    A YAML comment begins at a ``#`` that is at the start of the line or
    preceded by whitespace, and is NOT inside a quoted scalar or a ``${{ }}``
    expression — a ``#`` in those positions is content (a URL fragment, a
    literal in an expression), never a comment marker. Everything from a
    comment marker to the end of the line is documentation the workflow runner
    never executes, so no visitor below may refuse on it; mirror.yml's own
    resolver skips ``#`` comment lines the same way, and two readers of one
    tree must agree on what a comment is.

    Quoted scalars are walked with their escapes (``\\`` in double quotes,
    ``''`` in single quotes) so a destination inside a quoted URL stays
    visible even when the same line carries a comment after it.
    """
    n = len(line)
    i = 0
    while i < n:
        ch = line[i]
        if ch == "'":
            i += 1
            while i < n:
                if line[i] == "'":
                    if i + 1 < n and line[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif ch == '"':
            i += 1
            while i < n:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == '"':
                    i += 1
                    break
                i += 1
        elif ch == "$" and line.startswith("{{", i + 1):
            i += 3
            depth = 1
            while i < n and depth:
                if line.startswith("{{", i):
                    depth += 1
                    i += 2
                elif line.startswith("}}", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
        else:
            i += 1
    return line


def _is_tool_supplier(path_token):
    """True when a HOST/OWNER/REPO literal is a whitelisted tool-supplier fetch.

    ``path_token`` comes in either transport form: ``host/owner/repo`` (http) or
    ``host:owner/repo`` after a ``git@`` (scp). Normalise the scp form onto a
    slash before comparing. A supplier namespace carries only released tool bytes,
    never a release or mirror the workflow produces.
    """
    if path_token.startswith("git@"):
        path_token = path_token[len("git@"):].replace(":", "/", 1)
    parts = path_token.split("/")
    if len(parts) < 3:
        return False
    triple = tuple(parts[:3])
    return triple in _TOOL_SUPPLIERS


def _iter_multipart_host_literals(line):
    """Yield all forge repository literals in one line (http or scp transport).

    Yields the full ``HOST/OWNER/REPO[...]`` matched run per http-scoped match,
    and the full ``git@HOST:OWNER/REPO[.git]`` run per scp match, each as a
    single reportable token.
    """
    for m in _DEST_PATH.finditer(line):
        host = m.group("host")
        if host not in _DESTINATION_HOSTS:
            continue
        yield line[m.start("host"):m.end()]
    for m in _SCP_PATH.finditer(line):
        host = m.group("host")
        if host not in _DESTINATION_HOSTS:
            continue
        yield line[m.start():m.end()]


def _vis_vars_offence(line, lineno):
    for m in _VARS_REF.finditer(line):
        yield lineno, m.group(0)


def _vis_api_host_offence(line, lineno):
    """Refuse a forge API host name by value, even without an OWNER/REPO beside
    it. An API call takes its transport from the host and its target from a
    split path/owner, so only refusing both half-destinations closes the shape."""
    for m in _API_HOST_REF.finditer(line):
        yield lineno, m.group(0)


def _vis_dest_offence(line, lineno):
    for token in _iter_multipart_host_literals(line):
        if _is_tool_supplier(token):
            continue
        yield lineno, token


def _scan_text(path, text):
    """Return a sorted list of (path, lineno, token, kind) offences in one file."""
    offences = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = _scan_line(line)
        for ln, token in _vis_vars_offence(line, lineno):
            offences.append((path, ln, token, "ci-variable"))
        for ln, token in _vis_api_host_offence(line, lineno):
            offences.append((path, ln, token, "api-host"))
        for ln, token in _vis_dest_offence(line, lineno):
            offences.append((path, ln, token, "destination"))
    return sorted(offences)


def _scan_file(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ci_variable_boundary_gate: ERROR reading {path}: {exc}", file=sys.stderr)
        raise
    return _scan_text(str(path), text)


def _collect_targets(args):
    if args.workflow:
        targets = [Path(p) for p in args.workflow]
    elif args.dir:
        targets = sorted(
            p
            for p in Path(args.dir).rglob("*")
            if p.is_file() and p.suffix in (".yml", ".yaml")
        )
    else:
        default = Path(".forgejo/workflows")
        targets = (
            sorted(
                p
                for p in default.rglob("*")
                if p.is_file() and p.suffix in (".yml", ".yaml")
            )
            if default.is_dir()
            else []
        )
    for t in targets:
        if t.is_dir():
            print(
                f"ci_variable_boundary_gate: ERROR: {t} is a directory, not a workflow file",
                file=sys.stderr,
            )
            raise SystemExit(2)
    return targets


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Refuse a forge destination that bypasses the config layer."
    )
    parser.add_argument(
        "--dir",
        metavar="PATH",
        default=None,
        help="scan every .yml/.yaml below PATH",
    )
    parser.add_argument(
        "workflow",
        nargs="*",
        metavar="WORKFLOW",
        help="one or more workflow files to check (default: .forgejo/workflows)",
    )
    args = parser.parse_args(argv)

    try:
        targets = _collect_targets(args)
    except SystemExit:
        raise

    if not targets:
        print(
            "ci_variable_boundary_gate: no workflow files to check "
            "(default .forgejo/workflows is absent and --dir/--workflow unset)",
            file=sys.stderr,
        )
        return 2

    all_offences = []
    for path in targets:
        try:
            all_offences.extend(_scan_file(path))
        except OSError:
            return 2

    if all_offences:
        for filepath, lineno, token, kind in sorted(all_offences):
            if kind == "ci-variable":
                sys.stderr.write(
                    "CiVariableBoundaryError: %s:%d: user-defined CI variable "
                    "%r. 'vars' is the CI system's user variable store; a forge "
                    "destination carried there bypasses the config layer. Move "
                    "the destination into .ops.yaml (read through "
                    "config_loader.load_ops_yaml). secrets.* (a credential) and "
                    "github.* (Forgejo-provided event context) are permitted.\n"
                    % (filepath, lineno, token)
                )
            elif kind == "api-host":
                sys.stderr.write(
                    "DestinationBoundaryError: %s:%d: hardcoded forge API host "
                    "%r. Naming an API host by value splits the destination away "
                    "from the config layer; read the destination from .ops.yaml "
                    "instead.\n"
                    % (filepath, lineno, token)
                )
            else:
                sys.stderr.write(
                    "DestinationBoundaryError: %s:%d: hardcoded forge destination "
                    "literal %r. A destination named by value here bypasses the "
                    "config layer; read it from .ops.yaml instead. Tool-fetch "
                    "suppliers (actions/*, the OSV scanner) are whitelisted.\n"
                    % (filepath, lineno, token)
                )
        return 1

    sys.stdout.write(
        "ci-variable boundary gate: PASS - no workflow names a forge "
        "destination outside the config layer (no vars.* destination variable, "
        "no hardcoded forge repository, git remote or API host on a "
        "non-tool-supplier).\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
