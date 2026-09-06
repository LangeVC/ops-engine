#!/usr/bin/env python3
"""REL-011 — refuse a release description addressed at an internal audience.

The release body of ``.forgejo/workflows/forgejo-release.yml`` is the CHANGELOG
entry for the tag being released, sliced out as the version's notes. That body
is read by external consumers — operators of other repositories — not by the
team that wrote the release. A body that names an internal ticket reference
addresses the author's tracker, so this gate refuses it with a named error that
quotes each offending token and the line it appears on, exiting non-zero BEFORE
any release object exists.

The code-and-number shape ``[A-Z]{2,5}-[0-9]+`` is shared by an internal ticket
reference and by identifiers the external reader legitimately needs — a CVE
advisory id, an RFC or ISO number, PEP-8, gRPC, and ordinary technical prose
(UTF-8, SHA-256, TLS-1.3, HTTP-2, AES-256). The shape alone therefore cannot
tell an internal project code from an external identifier, and no denylist of
universal prefixes can make it sound: that set is open-ended (UTF, SHA, TLS,
HTTP, SSL, AES, RSA, IEEE, PNG, JPEG, ...). Layer 1 does not classify the
prefix. The only discriminator is the organisation's OWN tracker prefixes,
which are Layer-2 vocabulary: a ticket-shaped token is refused only when its
prefix is among the prefixes the caller declares via ``--ticket-prefixes``.

ops-engine is the template and ships no organisation vocabulary. With no
``--ticket-prefixes`` and no ``--forbid-file`` the gate refuses NOTHING — an
organisation that supplies no vocabulary gets no vocabulary check. That is the
correct default, not a hole. This repository's own release workflow supplies
this organisation's tracker prefixes, so its own notes stay gated: the
vocabulary arrives from the workflow (the config layer), never from the engine.

The gate is stdlib-only and never imports ``ops_engine`` (REL-006): the bare
release runner carries neither yaml nor pydantic, so nothing outside the
standard library may execute here.

Usage::

    release_notes_audience_gate.py [--forbid-file PATH] [--ticket-prefixes PATH] [NOTES_FILE]

``NOTES_FILE`` holds the extracted release notes; when omitted the notes are
read from stdin.

``--ticket-prefixes`` is optional and supplies the organisation's own tracker
prefixes, one per line. Each prefix is an uppercase ``[A-Z]{2,5}`` token (the
prefix length of the code-and-number shape). A shape-match whose prefix is in
this set is refused as an internal ticket reference. An absent flag means no
ticket check — the engine itself holds no organisation vocabulary. A prefix
file that is NAMED but missing or malformed (a line that is not one uppercase
2-5 letter token) is a named refusal, never a silent skip that would release
without the vocabulary the organisation chose.

``--forbid-file`` is optional and supplies organisation-supplied vocabulary
terms, one per line, that are withheld from an external reader (product names,
project codenames — terms, not prefixes). ops-engine ships no such vocabulary.
A forbid file that is present but malformed (a line that is not a single term,
or a path that does not resolve) is a named refusal, never a silent skip that
would release without the vocabulary the organisation chose.
"""

import argparse
import re
import sys
from pathlib import Path

INTERNAL_TICKET_RE = re.compile(r"[A-Z]{2,5}-[0-9]+")
TICKET_PREFIX_RE = re.compile(r"[A-Z]{2,5}")


class _MalformedVocabulary(Exception):
    """Internal signal: a vocabulary file is present but unusable."""


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


def _load_ticket_prefixes(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    prefixes = []
    for lineno, line in enumerate(lines, start=1):
        term = line.strip()
        if not term:
            continue
        if not TICKET_PREFIX_RE.fullmatch(term):
            raise _MalformedVocabulary(
                "line %d is not a tracker prefix: %r must be one uppercase "
                "[A-Z]{2,5} token (the prefix length of the code-and-number "
                "shape), with no whitespace and no digits" % (lineno, line)
            )
        prefixes.append(term)
    return prefixes


def _find_offences(notes, forbid_terms, ticket_prefixes):
    offences = set()
    for lineno, line in enumerate(notes.splitlines(), start=1):
        if ticket_prefixes:
            for match in INTERNAL_TICKET_RE.finditer(line):
                prefix = match.group(0).split("-", 1)[0]
                if prefix in ticket_prefixes:
                    offences.add((lineno, match.group(0), "internal"))
        for term in forbid_terms:
            if term in line:
                offences.add((lineno, term, "forbidden"))
    return sorted(offences)


def _green_line(ticket_prefixes, forbid_terms):
    checks = []
    if ticket_prefixes:
        checks.append("names no internal ticket reference")
    if forbid_terms:
        checks.append("carries no forbidden organisation term")
    if checks:
        return (
            "release-notes audience gate: PASS - the release description "
            + " and ".join(checks) + "."
        )
    return (
        "release-notes audience gate: PASS - no organisation vocabulary was "
        "supplied (no --ticket-prefixes, no --forbid-file), so no vocabulary "
        "check ran."
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Refuse release notes addressed at an internal audience."
    )
    parser.add_argument(
        "--forbid-file",
        metavar="PATH",
        default=None,
        help="optional file of organisation-supplied withheld terms, one per line",
    )
    parser.add_argument(
        "--ticket-prefixes",
        metavar="PATH",
        default=None,
        help="optional file of the organisation's own tracker prefixes, one per line",
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

    ticket_prefixes = []
    if args.ticket_prefixes is not None:
        try:
            ticket_prefixes = _load_ticket_prefixes(args.ticket_prefixes)
        except FileNotFoundError:
            sys.stderr.write(
                "ForbiddenVocabularyError: --ticket-prefixes %r does not resolve "
                "to a file. A prefix file that is named must be present; "
                "refusing rather than silently releasing without the tracker "
                "prefixes the organisation chose.\n" % args.ticket_prefixes
            )
            return 2
        except _MalformedVocabulary as exc:
            sys.stderr.write(
                "ForbiddenVocabularyError: --ticket-prefixes %r is malformed: "
                "%s.\n" % (args.ticket_prefixes, exc)
            )
            return 2

    notes = _read_notes(args.notes_file)
    offences = _find_offences(notes, forbid_terms, frozenset(ticket_prefixes))
    if offences:
        for lineno, token, kind in offences:
            if kind == "internal":
                sys.stderr.write(
                    "ReleaseNotesAudienceError: line %d: internal ticket "
                    "reference %r in the release description. Its prefix is "
                    "among the organisation's declared tracker prefixes, so the "
                    "body names the author's tracker. The release body is read "
                    "by external consumers and must not name the author's "
                    "tracker; rewrite the entry for an external reader.\n"
                    % (lineno, token)
                )
            else:
                sys.stderr.write(
                    "ForbiddenVocabularyError: line %d: forbidden organisation "
                    "term %r in the release description; the organisation "
                    "withholds this vocabulary from an external reader.\n"
                    % (lineno, token)
                )
        return 1

    sys.stdout.write(_green_line(ticket_prefixes, forbid_terms) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
