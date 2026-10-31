"""OE-007 - the release gates, as public functions.

Three gates stand between a tag and a release object. Each is a function that
refuses with a named error, so a caller (a workflow step, a handler, a test)
imports the gate instead of carrying a copy of it:

* :func:`check_release_title` - the rendered title must be the layover's
  ``name_template`` (or ``<prefix> <tag>``) around a ``vX.Y.Z`` or
  ``vX.Y.Z-rcN`` tag. Refuses with :class:`ReleaseTitleError`.
* :func:`extract_changelog_section` - the release body is the CHANGELOG section
  of the tag; a tag with no section (or an empty one) is refused with
  :class:`MissingChangelogEntryError` instead of falling back to commit
  subjects.
* :func:`check_release_notes_audience` - a body that names one of the
  organisation's OWN tracker prefixes addresses the author's tracker and is
  refused with :class:`ReleaseNotesAudienceError`. The engine ships no
  organisation vocabulary: with no prefixes supplied the gate refuses nothing.

The module imports only the standard library (D19). The release runner has
neither ``yaml`` nor ``pydantic`` and ``import ops_engine`` pulls both in, so
the workflow loads this file by its path and never imports the package. The
CHANGELOG heading pattern is therefore held here as well as in
``ChangelogParser``; ``tests/test_release_gates.py`` pins the two to the same
pattern. Its name differs from every name it exports.
"""

import re
from typing import Iterable, Optional

__all__ = [
    "ReleaseTitleError",
    "MissingChangelogEntryError",
    "ReleaseNotesAudienceError",
    "check_release_title",
    "extract_changelog_section",
    "check_release_notes_audience",
]

TAG_NAME_PLACEHOLDER = "{tag_name}"
_TAG_RE = r"v[0-9]+\.[0-9]+\.[0-9]+(?:-rc[0-9]+)?"
# Same pattern as ChangelogParser.HEADER_PATTERN: ``## v1.0.0``, ``## [1.0.0]``,
# ``## Package v1.0.0 - Title (date)``.
_HEADER_RE = re.compile(
    r"^##\s+"
    r"(?:\[)?(?:[\w-]+\s+)?v?"
    r"(\d+\.\d+\.\d+(?:[-.\w]*)?)"
    r"(?:\])?"
    r".*$",
    re.MULTILINE,
)
_TICKET_SHAPE_RE = re.compile(r"[A-Z]{2,5}-[0-9]+")
_TICKET_PREFIX_RE = re.compile(r"[A-Z]{2,5}")


class ReleaseTitleError(ValueError):
    """The rendered release title does not conform to the release convention."""


class MissingChangelogEntryError(ValueError):
    """The CHANGELOG has no (or an empty) section for the tag being released."""


class ReleaseNotesAudienceError(ValueError):
    """The release notes address an internal audience, or the vocabulary is unusable.

    ``offences`` lists ``(line_number, token)`` for each tracker reference.
    """

    def __init__(self, message: str, offences: Optional[list] = None):
        super().__init__(message)
        self.offences = offences or []


def check_release_title(
    tag_name: str,
    name_template: Optional[str] = None,
    prefix: Optional[str] = None,
) -> str:
    """Gate the release title; return the rendered title when it conforms.

    Exactly one of ``name_template`` (containing ``{tag_name}`` once) or
    ``prefix`` (rendered as ``"<prefix> <tag_name>"``) names the title form.
    The rendered title must be that form around a tag ``vX.Y.Z`` or the
    prerelease form ``vX.Y.Z-rcN`` (lowercase ``rc`` and at least one digit).

    Raises:
        ReleaseTitleError: the form is not named unambiguously, or the tag does
            not conform. The message names the offending title.
    """
    if (name_template is None) == (prefix is None):
        raise ReleaseTitleError(
            "ReleaseTitleError: pass exactly one of name_template or prefix; "
            "got name_template=%r prefix=%r." % (name_template, prefix)
        )
    if name_template is None:
        if not prefix or not prefix.strip():
            raise ReleaseTitleError(
                "ReleaseTitleError: the title prefix is empty; a release title "
                "needs the layover's prefix."
            )
        name_template = prefix + " " + TAG_NAME_PLACEHOLDER
    if name_template.count(TAG_NAME_PLACEHOLDER) != 1:
        raise ReleaseTitleError(
            "ReleaseTitleError: name_template %r must contain %s exactly once."
            % (name_template, TAG_NAME_PLACEHOLDER)
        )
    head, tail = name_template.split(TAG_NAME_PLACEHOLDER)
    rendered = head + tag_name + tail
    conforms = re.compile(
        "^%s%s%s$" % (re.escape(head), _TAG_RE, re.escape(tail))
    )
    if not conforms.match(rendered):
        raise ReleaseTitleError(
            "ReleaseTitleError: release name %r does not match the required "
            "convention %r around vX.Y.Z or its prerelease form vX.Y.Z-rcN. "
            "Refusing before any release object exists." % (rendered, name_template)
        )
    return rendered


def _section_body(changelog: str, version: str) -> str:
    """The text between the heading of ``version`` and the next version heading."""
    headings = list(_HEADER_RE.finditer(changelog))
    for idx, heading in enumerate(headings):
        if heading.group(1) == version:
            end = headings[idx + 1].start() if idx + 1 < len(headings) else len(changelog)
            return changelog[heading.end():end].strip()
    return ""


def extract_changelog_section(changelog: str, tag_name: str) -> str:
    """Return the CHANGELOG notes of ``tag_name``; refuse a missing section.

    Both ``v1.2.3`` and ``1.2.3`` name the version. Every heading form
    :class:`ChangelogParser` understands is accepted, including the bracketed
    link form ``## [1.2.3](...) (date)``.

    Raises:
        MissingChangelogEntryError: no section, or a section with an empty body.
            A release with no authored notes would post commit subjects.
    """
    version = tag_name.lstrip("v")
    notes = _section_body(changelog or "", version)
    if not notes:
        raise MissingChangelogEntryError(
            "MissingChangelogEntryError: no non-empty '## %s' section in "
            "CHANGELOG.md; a release with no notes would post commit subjects, "
            "so the release is refused." % version
        )
    return notes


def check_release_notes_audience(
    notes: str, ticket_prefixes: Optional[Iterable[str]] = None
) -> None:
    """Refuse notes that name one of the organisation's own tracker prefixes.

    A ticket-shaped token (``[A-Z]{2,5}-[0-9]+``) is refused only when its
    prefix is among ``ticket_prefixes``: the shape alone cannot tell an internal
    ticket from UTF-8, SHA-256 or CVE-2026-1234. With no prefixes the gate
    refuses nothing - the engine holds no organisation vocabulary.

    Raises:
        ReleaseNotesAudienceError: a tracker reference is present (the message
            quotes each token and its line), or a supplied prefix is malformed
            (never skipped silently).
    """
    prefixes = list(ticket_prefixes or [])
    for prefix in prefixes:
        if not _TICKET_PREFIX_RE.fullmatch(prefix):
            raise ReleaseNotesAudienceError(
                "ReleaseNotesAudienceError: tracker prefix %r is malformed; it "
                "must be one uppercase [A-Z]{2,5} token. Refusing rather than "
                "releasing without the vocabulary the organisation chose."
                % (prefix,)
            )
    declared = frozenset(prefixes)
    offences = []
    for lineno, line in enumerate((notes or "").splitlines(), start=1):
        for match in _TICKET_SHAPE_RE.finditer(line):
            token = match.group(0)
            if token.split("-", 1)[0] in declared:
                offences.append((lineno, token))
    if offences:
        raise ReleaseNotesAudienceError(
            "ReleaseNotesAudienceError: the release description names the "
            "author's tracker: "
            + "; ".join("line %d: %r" % (n, t) for n, t in offences)
            + ". The body is read by external consumers; rewrite the entry "
            "for an external reader.",
            offences,
        )
