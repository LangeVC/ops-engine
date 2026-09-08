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
import ast
import re
import sys
from pathlib import Path

# The UNIVERSAL set of hosts whose ``OWNER/REPO`` is a forge **destination** this
# kind of workflow is prohibited from naming as a literal — the public forges any
# adopter recognises, no organisation knowledge. github.com covers the mirror's
# git remote and any github.com/OWNER/REPO fetch that is not a whitelisted
# supplier; gitlab.com, codeberg.org and git.sr.ht cover the shape a layover that
# later points a second (web/git-form) destination would write, so the refusal is
# explicit rather than a github-only special case. GitLab's API rides on
# gitlab.com itself, so its API paths are refused through this same set; only
# github.com keeps its REST/upload endpoints on dedicated subdomains, which is why
# ``api.github.com`` and ``uploads.github.com`` are a separate, host-name-only
# refusal (below).
#
# A self-hosted instance is NOT in this set: an organisation's own forge host is
# organisation knowledge and arrives from the config layer via ``--dest-hosts``
# (a file of hosts, one per line), exactly as the org vocabulary arrives via
# ``--org-vocab`` and the CI variable names via ``--ci-env`` on the ``src/`` side.
# With no ``--dest-hosts`` file the gate still refuses only the universal set,
# and an organisation that declares no forge host gets no check for one.
_DESTINATION_HOSTS = (
    "github.com",
    "www.github.com",
    "gitlab.com",
    "codeberg.org",
    "git.sr.ht",
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

# A tracker prefix standing ALONE as a literal (REL-021): an uppercase
# ``[A-Z]{2,5}`` token that is not part of a longer word, not part of a
# hyphenated identifier (``REL-021``), and not a variable reference (``$REL``,
# ``${REL}``). Only a token whose value IS one of the caller-supplied
# ``--ticket-prefixes`` is refused; the token shape alone names nothing.
_TRACKER_PREFIX_RE = re.compile(r"(?<![\w${}.-])(?P<prefix>[A-Z]{2,5})(?![\w-])")
_TRACKER_PREFIX_FORM = re.compile(r"[A-Z]{2,5}")

# A forge host must carry a dot: a hostname names a domain. A dotless label is a
# forge type (`forgejo`), a header prefix (`x-forgejo-event`) or a package name
# before it is anybody's hostname, and handing the gate such a label makes it
# refuse an ordinary word everywhere in src/ (REL-023). The register boundary
# refuses a dotless entry by name, before any scan runs.
_HOST_HAS_DOT = re.compile(r"\.")

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


def _iter_multipart_host_literals(line, dest_hosts):
    """Yield all forge repository literals in one line (http or scp transport).

    ``dest_hosts`` is the destination-host set to refuse against: the universal
    set plus any organisation hosts the caller supplied via ``--dest-hosts``.
    Yields the full ``HOST/OWNER/REPO[...]`` matched run per http-scoped match,
    and the full ``git@HOST:OWNER/REPO[.git]`` run per scp match, each as a
    single reportable token.
    """
    for m in _DEST_PATH.finditer(line):
        host = m.group("host")
        if host not in dest_hosts:
            continue
        yield line[m.start("host"):m.end()]
    for m in _SCP_PATH.finditer(line):
        host = m.group("host")
        if host not in dest_hosts:
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


def _vis_dest_offence(line, lineno, dest_hosts):
    for token in _iter_multipart_host_literals(line, dest_hosts):
        if _is_tool_supplier(token):
            continue
        yield lineno, token


def _vis_ticket_prefix_offence(line, lineno, ticket_prefixes):
    """Refuse a tracker prefix that stands alone as a literal on one line.

    REL-021 — a template cannot know one organisation's tracker. ``--ticket-prefixes``
    supplies that organisation's prefixes the same way ``--dest-hosts`` supplies its
    forge hosts; a workflow line that writes one of them as a bare literal (the
    ``printf '%s\\n' LVC OME CORE ...`` shape) took that vocabulary OUT of the config
    layer. A prefix that is part of a longer word, a hyphenated identifier
    (``REL-021``), or a variable reference (``$REL`` / ``${REL}``) is not a literal
    and is not refused here. Comment text never reaches this visitor
    (``_scan_line`` removed it)."""
    if not ticket_prefixes:
        return
    for m in _TRACKER_PREFIX_RE.finditer(line):
        token = m.group("prefix")
        if token in ticket_prefixes:
            yield lineno, token


def _scan_text(path, text, dest_hosts, ticket_prefixes):
    """Return a sorted list of (path, lineno, token, kind) offences in one file."""
    offences = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = _scan_line(line)
        for ln, token in _vis_vars_offence(line, lineno):
            offences.append((path, ln, token, "ci-variable"))
        for ln, token in _vis_api_host_offence(line, lineno):
            offences.append((path, ln, token, "api-host"))
        for ln, token in _vis_dest_offence(line, lineno, dest_hosts):
            offences.append((path, ln, token, "destination"))
        for ln, token in _vis_ticket_prefix_offence(line, lineno, ticket_prefixes):
            offences.append((path, ln, token, "tracker-prefix"))
    return sorted(offences)


def _scan_file(path, dest_hosts, ticket_prefixes):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ci_variable_boundary_gate: ERROR reading {path}: {exc}", file=sys.stderr)
        raise
    return _scan_text(str(path), text, dest_hosts, ticket_prefixes)


# ── Python layer-boundary scan (ADP-010) ────────────────────────────────────
#
# The destination boundary above covers WHERE a workflow may take a forge
# destination from. This second scan covers the ENGINE's own Python source:
# Layer 1 (the ``src/`` tree) must know no organisation and no CI system. Two
# shapes are refused in executing Python, both located by parsing the source
# with ``ast`` — never by regex, so the distinction between a documentation
# mention and a live value is a node in the parse tree, not a pattern:
#
# * *an organisation name as a live value.* A string literal (``ast.Constant``
#   of ``str``) whose value equals a term the caller supplies via
#   ``--org-vocab``. The historical case is the rate-limit decorator
#   ``@track_rate_limit(namespace="capacium-ops")`` in ``health_monitor.py`` —
#   an organisation name baked into executing code, where it must instead
#   arrive as configuration or an argument.
#
# * *a direct CI-environment read.* ``os.environ.get("NAME")`` (or
#   ``os.getenv("NAME")``) whose variable name is a term the caller supplies via
#   ``--ci-env``. The historical case is ``os.environ.get("GITHUB_REPOSITORY")``
#   (and ``GITHUB_TOKEN``) in ``health_monitor.py`` — the engine reaching into
#   one CI system's variable store, where the value must instead arrive from the
#   workflow that invokes it.
#
# * *an organisation forge host as a live value* (ADP-014). A string constant
#   whose value CONTAINS a host the caller supplies via ``--dest-hosts`` — the
#   same file the destination boundary above already uses. The historical case
#   is ``FORGEJO_API_DEFAULT = "https://git.langevc.com/api/v1"`` in
#   ``mirror-destination-propose.py``: the operator's own forge base URL baked
#   into executing code. A public forge's API host (``api.github.com``,
#   ``uploads.github.com``) is legitimately known by a template that ships
#   adapters for that forge and is NOT in this refusal; an organisation's own
#   instance is organisation knowledge and must arrive as input, never as a
#   literal. The check is a substring test against each supplied host, so a
#   port or a path on the same host still refuses.
#
# A documentation mention is NOT refused: a module/class/function docstring is a
# specific ``ast`` node (the first ``Expr`` holding a ``Constant`` string in its
# body) and its value is skipped, so the same organisation name, organisation
# host, or CI variable that is refused as a live value passes when it appears in
# a docstring. Comments never reach the AST at all. This is the parsing
# distinction, not a pattern: a docstring is a node, and ``ast`` locates the
# exact line of every offence.
#
# A documentation mention is NOT refused: a module/class/function docstring is a
# specific ``ast`` node (the first ``Expr`` holding a ``Constant`` string in its
# body) and its value is skipped, so the same organisation name, organisation
# host, or CI variable that is refused as a live value passes when it appears in
# a docstring. Comments never reach the AST at all. This is the parsing
# distinction, not a pattern: a docstring is a node, and ``ast`` locates the
# exact line of every offence.
#
# The two scans do not reach every tree at once. The org-name and ci-env-read
# shapes are Layer-1 constraints, so they are enforced over ``src/`` and carved
# out for ``scripts/`` and ``tests/``: an operator tool is the calling layer and
# may legitimately name the organisation it operates on or read a CI variable the
# workflow hands it, exactly what a template may not. The org-host shape is not
# Layer-1-only: an operator tool must still receive the organisation's forge base
# URL as input (ADP-014), never default it, so the org-host check is enforced
# over ``src/`` AND ``scripts/`` and carved out only for ``tests/`` fixtures that
# name the organisation's own forge to assert the gate itself. The wiring that
# exercised this scope (release-gate.yml) scans ``src/`` and ``scripts/``; the
# test tree is not scanned, and the carve-outs here record that tree decision
# rather than leaving an unscanned tree to drift.
#
# The vocabulary is supplied from outside, never shipped in the gate (REL-011's
# principle, applied to the engine): ``--org-vocab`` is a file of organisation
# names, ``--ci-env`` a file of CI environment variable names, and
# ``--dest-hosts`` a file of organisation forge hosts. With none of them, the
# scan refuses nothing — an adopting engine supplies the vocabulary that matches
# its own ecosystem. The gate remains stdlib-only (``ast`` is standard library)
# and never imports ``ops_engine``.


def _load_vocab(path):
    """Read one term per line; blank lines skipped; terms whitespace-stripped."""
    terms = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        term = line.strip()
        if term:
            terms.append(term)
    return terms


def _load_ticket_prefixes(path):
    """Read tracker prefixes, one per line, refusing a malformed line by name.

    REL-021 — a tracker prefix is one uppercase ``[A-Z]{2,5}`` token (the prefix
    of the code-and-number shape the audience gate matches). A file that carries a
    non-prefix line is a named refusal, never a silent partial vocabulary that
    would let a reintroduced literal prefix walk past the gate ungated."""
    prefixes = set()
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        term = line.strip()
        if not term:
            continue
        if not _TRACKER_PREFIX_FORM.fullmatch(term):
            raise ValueError(
                "line %d is not a tracker prefix: %r must be one uppercase "
                "[A-Z]{2,5} token" % (lineno, term)
            )
        prefixes.add(term)
    return prefixes


def _load_dest_hosts(path):
    """Read organisation forge hosts, one per line, refusing a dotless label.

    REL-023 — a forge host names a domain, so it must carry a dot. A dotless
    label (`forgejo`, `localhost`) is a forge type, a header prefix or a package
    name before it is anybody's hostname, and an internal container address must
    never silently become an organisation's forge. Refusing a dotless entry here,
    at the register boundary and before the scan runs, is what turns that wrong
    input into a named refusal instead of a false rule the gate would apply
    everywhere."""
    hosts = []
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        term = line.strip()
        if not term:
            continue
        if not _HOST_HAS_DOT.search(term):
            raise ValueError(
                "line %d is not a forge host: %r carries no dot, so it is a forge "
                "type, a header prefix or a package name rather than a hostname; "
                "an internal address must not silently become an organisation's "
                "forge" % (lineno, term)
            )
        hosts.append(term)
    return hosts


def _docstring_value_ids(tree):
    """Ids of the string ``Constant`` nodes that are a docstring's value.

    A docstring is the first statement of a module, class, or function body, and
    its value is the ``Constant`` string node. Recording those nodes lets the
    scan skip exactly them — a documentation mention is that node, never a
    ``re`` pattern.
    """
    ids = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            if node.body and isinstance(node.body[0], ast.Expr):
                value = node.body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    ids.add(id(value))
    return ids


def _ci_env_literal(node):
    """The literal variable name of ``os.environ.get(...)``/``os.getenv(...)``.

    Returns the string the call reads from the CI environment, or ``None`` when
    the node is not such a call with a literal first argument. A read whose name
    comes from configuration (``os.environ.get(cfg.var_name)``) is not a literal
    and is not refused here.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in ("get", "getenv"):
        return None
    if func.attr == "get":
        if not (
            isinstance(func.value, ast.Attribute)
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "os"
            and func.value.attr == "environ"
        ):
            return None
    else:  # getenv
        if not (isinstance(func.value, ast.Name) and func.value.id == "os"):
            return None
    if (
        node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return None


def _scan_python(text, path, org_terms, ci_env_vars, dest_hosts):
    """Return a sorted list of (path, lineno, token, kind) offences in one file.

    Locates offences by parsing with ``ast``, not by pattern: a docstring's value
    node is skipped (documentation), a live string constant equal to an
    organisation term or CONTAINING an organisation forge host is refused, and an
    ``os.environ.get``/``os.getenv`` call with a literal CI variable name is
    refused. Comments never reach the AST.

    The three shapes do not all reach the same trees. Layer 1 is the engine's own
    ``src/`` tree; only Layer 1 knows no organisation and no CI system, so the
    org-name and CI-environment-read shapes are enforced on ``src/`` and carved
    out for the operator tools (``scripts/``) and the test tree (``tests/``). An
    operator tool is the calling layer, not the engine: it legitimately names the
    organisation it operates on and reads a CI environment variable the workflow
    hands it, so it may carry what a template may not. The org-host shape is
    different and is enforced one tree wider: an operator tool must still receive
    the organisation's forge base URL as input, never as a literal default (the
    ADP-014 defect lived in ``scripts/mirror-destination-propose.py``), so it is
    enforced on ``src/`` AND ``scripts/`` and carved out only for the test tree,
    whose fixtures name the organisation's own forge to assert the gate itself.
    """
    tree = ast.parse(text)
    docstring_ids = _docstring_value_ids(tree)
    offences = []
    org_set = set(org_terms)
    host_set = set(dest_hosts)
    parts = Path(path).parts
    is_operator_tool = "scripts" in parts
    is_test_fixture = "tests" in parts
    for node in ast.walk(tree):
        if (
            not is_operator_tool
            and not is_test_fixture
            and isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_ids
            and node.value in org_set
        ):
            offences.append((path, node.lineno, node.value, "org-name"))
        elif (
            not is_test_fixture
            and isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_ids
            and host_set
            and any(h in node.value for h in host_set)
        ):
            offences.append((path, node.lineno, node.value, "org-host"))
        elif (
            not is_operator_tool
            and not is_test_fixture
            and isinstance(node, ast.Call)
        ):
            varname = _ci_env_literal(node)
            if varname is not None and varname in ci_env_vars:
                offences.append((path, node.lineno, varname, "ci-env-read"))
    return sorted(offences)


def _run_python_scan(args):
    """Scan every ``.py`` below ``--py-dir`` for a Layer-1 boundary bypass."""
    org_terms = _load_vocab(args.org_vocab) if args.org_vocab else []
    ci_env_vars = set(_load_vocab(args.ci_env)) if args.ci_env else set()
    try:
        dest_hosts = _load_dest_hosts(args.dest_hosts) if args.dest_hosts else []
    except ValueError as exc:
        sys.stderr.write("ForgeHostRegisterError: %s\n" % exc)
        return 2
    targets = sorted(p for p in Path(args.py_dir).rglob("*.py") if p.is_file())
    if not targets:
        print(
            "ci_variable_boundary_gate: ERROR: --py-dir %r contains no .py files"
            % args.py_dir,
            file=sys.stderr,
        )
        return 2

    all_offences = []
    for path in targets:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            print(
                f"ci_variable_boundary_gate: ERROR reading {path}: {exc}",
                file=sys.stderr,
            )
            return 2
        all_offences.extend(
            _scan_python(text, str(path), org_terms, ci_env_vars, dest_hosts)
        )

    if all_offences:
        for filepath, lineno, token, kind in sorted(all_offences):
            if kind == "org-name":
                sys.stderr.write(
                    "Layer1BoundaryError: %s:%d: organisation name %r appears as a "
                    "live value in executing Python. Layer 1 (the engine) must "
                    "know no organisation; the name must arrive as configuration "
                    "or an argument, never as a literal. A docstring or comment "
                    "carrying the same name is documentation and is permitted.\n"
                    % (filepath, lineno, token)
                )
            elif kind == "org-host":
                sys.stderr.write(
                    "Layer1BoundaryError: %s:%d: organisation forge host appears "
                    "as a live value %r in executing Python. Layer 1 (the engine) "
                    "must know no organisation; the operator's own forge base URL "
                    "must arrive as input, never as a literal. A docstring or "
                    "comment carrying the same host is documentation and is "
                    "permitted.\n"
                    % (filepath, lineno, token)
                )
            else:
                sys.stderr.write(
                    "Layer1BoundaryError: %s:%d: direct CI-environment read of %r "
                    "in executing Python. Layer 1 must know no CI system; the "
                    "value must arrive from the caller (the workflow), never read "
                    "from the environment here.\n"
                    % (filepath, lineno, token)
                )
        return 1

    sys.stdout.write(
        "layer-1 boundary gate: PASS - no organisation name, organisation forge "
        "host, or CI environment variable appears as a live value in the scanned "
        "Python.\n"
    )
    return 0


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
        "--py-dir",
        metavar="PATH",
        default=None,
        help="scan every .py below PATH for a Layer-1 boundary bypass",
    )
    parser.add_argument(
        "--org-vocab",
        metavar="PATH",
        default=None,
        help="file of organisation names (one per line) refused as live values",
    )
    parser.add_argument(
        "--ci-env",
        metavar="PATH",
        default=None,
        help="file of CI environment variable names (one per line) refused as direct reads",
    )
    parser.add_argument(
        "--dest-hosts",
        metavar="PATH",
        default=None,
        help="file of organisation forge hosts (one per line) refused as destinations "
        "and as live Python values under --py-dir, added to the universal set "
        "(github.com, www.github.com, gitlab.com, codeberg.org, git.sr.ht)",
    )
    parser.add_argument(
        "--ticket-prefixes",
        metavar="PATH",
        default=None,
        help="file of the organisation's own tracker prefixes (one per line, each an "
        "uppercase [A-Z]{2,5} token) refused as a bare literal in a workflow file "
        "(the printf '%%s\\n' LVC OME ... shape)",
    )
    parser.add_argument(
        "workflow",
        nargs="*",
        metavar="WORKFLOW",
        help="one or more workflow files to check (default: .forgejo/workflows)",
    )
    args = parser.parse_args(argv)

    if args.py_dir is not None:
        return _run_python_scan(args)

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

    dest_hosts = set(_DESTINATION_HOSTS)
    if args.dest_hosts is not None:
        try:
            dest_hosts.update(_load_dest_hosts(args.dest_hosts))
        except FileNotFoundError:
            print(
                "ci_variable_boundary_gate: ERROR: --dest-hosts %r does not "
                "resolve to a file. A host file that is named must be present."
                % args.dest_hosts,
                file=sys.stderr,
            )
            return 2
        except ValueError as exc:
            print(
                "ForgeHostRegisterError: --dest-hosts %r is malformed: %s"
                % (args.dest_hosts, exc),
                file=sys.stderr,
            )
            return 2

    ticket_prefixes = set()
    if args.ticket_prefixes is not None:
        try:
            ticket_prefixes = _load_ticket_prefixes(args.ticket_prefixes)
        except FileNotFoundError:
            print(
                "ci_variable_boundary_gate: ERROR: --ticket-prefixes %r does not "
                "resolve to a file. A prefix file that is named must be present."
                % args.ticket_prefixes,
                file=sys.stderr,
            )
            return 2
        except ValueError as exc:
            print(
                "ci_variable_boundary_gate: ERROR: --ticket-prefixes %r is malformed: %s"
                % (args.ticket_prefixes, exc),
                file=sys.stderr,
            )
            return 2

    all_offences = []
    for path in targets:
        try:
            all_offences.extend(_scan_file(path, dest_hosts, ticket_prefixes))
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
            elif kind == "tracker-prefix":
                sys.stderr.write(
                    "TrackerPrefixBoundaryError: %s:%d: tracker prefix %r written "
                    "as a literal. A template cannot know one organisation's "
                    "tracker; the prefix must arrive from the config layer (the "
                    "release workflow reads .ops.yaml), never as a literal in a "
                    "workflow every adopter inherits.\n"
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
