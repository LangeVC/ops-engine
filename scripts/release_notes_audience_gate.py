#!/usr/bin/env python3
"""REL-011 — refuse a release description addressed at an internal audience.

The release body of ``.forgejo/workflows/forgejo-release.yml`` is the CHANGELOG
entry for the tag being released, sliced out as the version's notes. That body
is read by external consumers — operators of other repositories — not by the
team that wrote the release. A body that names an internal ticket reference
(``[A-Z]{2,5}-[0-9]+``, e.g. ``LVC-248``) addresses the author's tracker, so
this gate refuses it with a named error that quotes each offending token and
the line it appears on, exiting non-zero BEFORE any release object exists.

The gate is stdlib-only and never imports ``ops_engine`` (REL-006): the bare
release runner carries neither yaml nor pydantic, so nothing outside the
standard library may execute here.

Usage::

    release_notes_audience_gate.py [--forbid-file PATH] [NOTES_FILE]

``NOTES_FILE`` holds the extracted release notes; when omitted the notes are
read from stdin. ``--forbid-file`` is optional and supplies
organisation-supplied vocabulary terms, one per line. ops-engine ships no such
vocabulary, so the release workflow never passes the flag: an absent forbid
file is the proven normal case, and the gate then checks internal ticket
references alone. A forbid file that is present but malformed (a line that is
not a single term, or a path that does not resolve) is a named refusal, never a
silent skip that would release without the vocabulary the organisation chose.
"""

import argparse
import re
import sys
from pathlib import Path

INTERNAL_TICKET_RE = re.compile(r"[A-Z]{2,5}-[0-9]+")

GREEN_LINE = (
    "release-notes audience gate: PASS - the release description names no "
    "internal ticket reference and carries no forbidden organisation term."
)


class _MalformedVocabulary(Exception):
    """Internal signal: a forbid file is present but unusable."""


def _read_notes(path):
    if path is None:
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def _load_forbid_terms(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    terms = []
    for lineno, line in enumerate(lines, start=1):
        term = line.strip()
        if not term:
            continue
        if re.search(r"\s", term):
            raise _MalformedVocabulary(
                "line %d is not one term per line: %r contains whitespace"
                % (lineno, line)
            )
        terms.append(term)
    return terms


def _find_offences(notes, forbid_terms):
    offences = set()
    for lineno, line in enumerate(notes.splitlines(), start=1):
        for match in INTERNAL_TICKET_RE.finditer(line):
            offences.add((lineno, match.group(0), "internal"))
        for term in forbid_terms:
            if term in line:
                offences.add((lineno, term, "forbidden"))
    return sorted(offences)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Refuse release notes addressed at an internal audience."
    )
    parser.add_argument(
        "--forbid-file",
        metavar="PATH",
        default=None,
        help="optional file of organisation-supplied terms, one per line",
    )
    parser.add_argument(
        "notes_file",
        nargs="?",
        default=None,
        metavar="NOTES_FILE",
        help="file holding the extracted release notes (default: stdin)",
    )
    args = parser.parse_args(argv)

    forbid_terms = []
    if args.forbid_file is not None:
        try:
            forbid_terms = _load_forbid_terms(args.forbid_file)
        except FileNotFoundError:
            sys.stderr.write(
                "ForbiddenVocabularyError: --forbid-file %r does not resolve to a "
                "file. A forbid file that is named must be present; refusing "
                "rather than silently releasing without the organisation "
                "vocabulary.\n" % args.forbid_file
            )
            return 2
        except _MalformedVocabulary as exc:
            sys.stderr.write(
                "ForbiddenVocabularyError: --forbid-file %r is malformed: %s.\n"
                % (args.forbid_file, exc)
            )
            return 2

    notes = _read_notes(args.notes_file)
    offences = _find_offences(notes, forbid_terms)
    if offences:
        for lineno, token, kind in offences:
            if kind == "internal":
                sys.stderr.write(
                    "ReleaseNotesAudienceError: line %d: internal ticket "
                    "reference %r in the release description. The release body "
                    "is read by external consumers and must not name the "
                    "author's tracker; rewrite the entry for an external "
                    "reader.\n" % (lineno, token)
                )
            else:
                sys.stderr.write(
                    "ForbiddenVocabularyError: line %d: forbidden organisation "
                    "term %r in the release description; the organisation "
                    "withholds this vocabulary from an external reader.\n"
                    % (lineno, token)
                )
        return 1

    sys.stdout.write(GREEN_LINE + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
