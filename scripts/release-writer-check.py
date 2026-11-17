#!/usr/bin/env python3
"""release-writer-check - refuse a release-object writer outside the engine path.

RFL-021. A release object is created, edited, or deleted by exactly ONE code
path in this repository: ``ReleaseHandler.publish_release``, called from the
"Create Release" step of ``.forgejo/workflows/forgejo-release.yml``. Every other
workflow -- on either forge, Forgejo ``.forgejo/workflows/`` and GitHub
``.github/workflows/`` -- may READ a release and may DESCRIBE one in a comment,
but must not write one. A second writer is a second release object, which is
the defect this lane exists to prevent.

Two shapes are scanned for in every workflow file under the given directories:

  engine call   ``publish_release(`` / ``ReleaseHandler.publish_release``. The
                sanctioned path; reported as evidence, never an offence.
  direct writer a statement that creates, edits, or deletes a release object
                without going through the engine. Recognised shapes:
                  - a forge CLI release verb (``gh release create|edit|delete|
                    upload``, ``tea release ...``, ``forgejo release ...``)
                  - a known release-publishing action in ``uses:``
                    (``action-gh-release``, ``create-release``,
                    ``upload-release-asset``, ``release-action``,
                    ``goreleaser-action``)
                  - ``goreleaser release``
                  - an HTTP write -- curl ``-X``/``--request`` POST/PATCH/PUT/
                    DELETE, ``requests``/``httpx``/``session``/``client``
                    ``.post|.patch|.put|.delete(``, or a urllib request with a
                    write ``method=`` -- aimed at a ``/releases`` endpoint. The
                    endpoint may sit on the write statement or on one of the
                    next three lines, because forge calls are often wrapped.

A line whose first non-space character is ``#`` is a comment and is ignored; a
trailing unquoted comment is cut before a statement is judged. Backslash-
continued shell lines are joined first. Together those rules keep a comment
that names a release endpoint, and a GET download of a release asset (for
example the OSV scanner binary under ``/releases/download/``), out of the
census.

This is a line-oriented census of known writer shapes, not a parser of the
forge API. It recognises the shapes enumerated above and nothing else: a
publication written some other way (``curl -d`` without ``-X``,
``curl --request=POST``, ``curl -T``, ``wget --post-data``, or an endpoint
further than three rows below the write marker) passes it. Its job is to keep
the sanctioned path from regressing, not to prove that no writer exists in
any form.

A named directory that does not exist contributes no file, and is announced
on its own line rather than passed over: a repository may publish to only one
forge, so its absence is not an offence, but a census must never read as
green over a tree it never looked at. The census refuses only when no
directory yielded any file at all.

Exit codes:
  0  no direct release-object writer outside the engine path
  1  at least one direct writer; each offence is printed as file:line
  2  refused (named, no traceback): nothing to scan, or a path cannot be read

Usage:
    release-writer-check.py
    release-writer-check.py --root . --dir .forgejo/workflows --dir .github/workflows
    release-writer-check.py --dir some/other/workflows

The script is stdlib-only and reads files; it writes no file and touches no
forge.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

# Both forges this repository publishes to. Every workflow file under these
# directories is in the census; neither directory may be skipped silently.
DEFAULT_DIRS = (".forgejo/workflows", ".github/workflows")

_WORKFLOW_SUFFIXES = (".yml", ".yaml")

# A release object is written by one of these without the engine in between.
_CLI_WRITER = re.compile(
    r"\b(?:"
    r"gh\s+release\s+(?:create|edit|delete|upload)"
    r"|tea\s+releases?\s+(?:create|edit|delete|close)"
    r"|forgejo\s+releases?\s+(?:create|edit|delete)"
    r"|goreleaser\s+release"
    r")\b",
    re.IGNORECASE,
)

_WRITER_ACTION = re.compile(
    r"uses:\s*['\"]?[^\s'\"]*"
    r"(?:action-gh-release|create-release|upload-release-asset|release-action"
    r"|goreleaser-action)",
    re.IGNORECASE,
)

# HTTP write markers, one shape each. A marker alone is not an offence: it
# becomes one only when a /releases endpoint is in the same window.
_WRITE_MARKERS = (
    re.compile(r"(?:^|\s)(?:-X|--request)\s*['\"]?(?:POST|PATCH|PUT|DELETE)\b",
               re.IGNORECASE),
    re.compile(r"(?:^|\s)-X(?:POST|PATCH|PUT|DELETE)\b", re.IGNORECASE),
    re.compile(r"\b(?:requests|httpx|session|client|aiohttp)"
               r"\.(?:post|patch|put|delete)\s*\(", re.IGNORECASE),
    re.compile(r"method\s*=\s*['\"](?:POST|PATCH|PUT|DELETE)['\"]",
               re.IGNORECASE),
)

_GH_API = re.compile(r"\bgh\s+api\b", re.IGNORECASE)
_GH_API_WRITE = re.compile(
    r"(?:^|\s)(?:-f|-F|--field|--raw-field|--input)"
    r"|--method\s+(?:POST|PATCH|PUT|DELETE)",
    re.IGNORECASE,
)

_RELEASES_ENDPOINT = re.compile(r"/releases(?:/|\?|\b)", re.IGNORECASE)

_ENGINE_CALL = re.compile(r"\bpublish_release\s*\(")

# How far past a write marker a /releases endpoint may sit. Forge calls are
# routinely wrapped over several lines.
_WINDOW = 3


def _strip_trailing_comment(line: str) -> str:
    """Cut an unquoted trailing ``#`` comment; leave URL fragments alone."""
    quote = ""
    escaped = False
    for i, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def statements(path: str) -> list[tuple[int, str]]:
    """Return ``(line_number, joined_statement)`` for one workflow file.

    Comment lines are dropped, trailing comments are cut, and backslash-
    continued lines are joined so a wrapped forge call is judged as one
    statement.
    """
    with open(path, encoding="utf-8", errors="replace") as handle:
        raw = handle.read().splitlines()
    joined: list[tuple[int, str]] = []
    buffer = ""
    start_line = 0
    for number, line in enumerate(raw, start=1):
        if line.lstrip().startswith("#"):
            continue
        text = _strip_trailing_comment(line).rstrip()
        if not text:
            continue
        if not buffer:
            start_line = number
        if text.endswith("\\"):
            buffer += text[:-1] + " "
            continue
        buffer += text
        joined.append((start_line, buffer))
        buffer = ""
    if buffer:
        joined.append((start_line, buffer))
    return joined


def _is_http_write(text: str) -> bool:
    return any(marker.search(text) for marker in _WRITE_MARKERS)


def _is_gh_api_write(text: str) -> bool:
    return bool(_GH_API.search(text) and _GH_API_WRITE.search(text))


def inspect_file(path: str) -> tuple[list[str], list[str]]:
    """Return ``(offences, engine_calls)`` for one workflow file."""
    rows = statements(path)
    offences: list[str] = []
    engine_calls: list[str] = []
    for index, (number, text) in enumerate(rows):
        if _ENGINE_CALL.search(text):
            engine_calls.append(f"{path}:{number} engine call publish_release")
            continue
        cli = _CLI_WRITER.search(text)
        if cli:
            offences.append(
                f"{path}:{number} `{cli.group(0).strip()}` writes a release "
                f"object outside the engine path")
            continue
        action = _WRITER_ACTION.search(text)
        if action:
            offences.append(
                f"{path}:{number} action `{action.group(0).strip()}` writes a "
                f"release object outside the engine path")
            continue
        if _is_http_write(text) or _is_gh_api_write(text):
            window = rows[index:index + _WINDOW]
            if any(_RELEASES_ENDPOINT.search(later) for _, later in window):
                offences.append(
                    f"{path}:{number} HTTP write aimed at a /releases endpoint "
                    f"writes a release object outside the engine path")
    return offences, engine_calls


def _workflow_files(root: str, directories) -> tuple[list[str], list[str]]:
    """Return ``(files, absent_roots)``; refusal on a path that cannot be read."""
    files: list[str] = []
    absent: list[str] = []
    for directory in directories:
        full = directory if os.path.isabs(directory) else os.path.join(root, directory)
        if not os.path.exists(full):
            # A repository may publish to only one forge, so an absent root is
            # not an offence -- but it is never silent: the caller announces
            # every root that contributed nothing, and the total of zero is a
            # refusal. A census must not read as green over files it never saw.
            absent.append(full)
            continue
        if not os.path.isdir(full):
            raise _Refusal(f"{full}: not a directory")
        try:
            names = sorted(os.listdir(full))
        except OSError as exc:
            raise _Refusal(f"{full}: cannot list: {exc}") from exc
        for name in names:
            path = os.path.join(full, name)
            if name.endswith(_WORKFLOW_SUFFIXES) and os.path.isfile(path):
                files.append(path)
    return files, absent


class _Refusal(Exception):
    """A named refusal: printed, exit 2, never a traceback."""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Refuse a release-object writer outside the engine path.")
    parser.add_argument("--root", default=".",
                        help="repository root the --dir paths are relative to")
    parser.add_argument("--dir", action="append", dest="dirs",
                        default=None,
                        help="workflow directory to scan (repeatable; default: "
                             "both forges: .forgejo/workflows and "
                             ".github/workflows)")
    args = parser.parse_args(argv)
    directories = args.dirs or list(DEFAULT_DIRS)

    try:
        files, absent = _workflow_files(args.root, directories)
        if not files:
            raise _Refusal(
                "no workflow files found under "
                + ", ".join(directories)
                + " (nothing was checked)")
        offences: list[str] = []
        engine_calls: list[str] = []
        for path in files:
            found, calls = inspect_file(path)
            offences.extend(found)
            engine_calls.extend(calls)
    except _Refusal as exc:
        print(f"release-writer-check: ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"release-writer-check: ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"release-writer-check: scanned {len(files)} workflow file(s) under "
          f"{', '.join(directories)}")
    for root in absent:
        print(f"release-writer-check: absent root not scanned: {root}")
    for call in engine_calls:
        print(f"ENGINE {call} (sanctioned)")
    if offences:
        for offence in offences:
            print(f"OFFENCE {offence}")
        print(f"release-writer-check: REFUSED: {len(offences)} direct "
              f"release-object writer(s) outside the engine path")
        return 1
    print("release-writer-check: OK: no release-object writer outside the "
          "engine path")
    return 0


if __name__ == "__main__":
    sys.exit(main())
