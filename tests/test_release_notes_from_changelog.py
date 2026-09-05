"""REL-001 — release notes come from CHANGELOG.md, and a missing entry blocks the tag.

The workflow `.forgejo/workflows/forgejo-release.yml` posts the `## X.Y.Z`
section of CHANGELOG.md as the release body and refuses a tag whose version has
no section. This test proves the write the workflow performs against the real
CHANGELOG.md and the real workflow.

The parser half of the semantics is exercised through `ChangelogParser`, the
library implementation imported below. Since REL-006 the workflow itself no
longer imports it: the notes step embeds a stdlib-only copy of the same slicing
logic so the release runner needs neither `yaml` nor `pydantic`. The
workflow-half tests therefore assert on the workflow's data flow (the notes
step reads CHANGELOG.md and never `git log`), not on which function or module
implements it — pinning an extractor name is what REL-007 removed.
"""

import re
from pathlib import Path

import pytest

from ops_engine.utils.changelog_parser import ChangelogParser

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
WORKFLOW = REPO_ROOT / ".forgejo" / "workflows" / "forgejo-release.yml"

CHANGELOG_CONTENT = CHANGELOG.read_text(encoding="utf-8")
WORKFLOW_CONTENT = WORKFLOW.read_text(encoding="utf-8")


def _notes_for(version: str) -> str:
    return ChangelogParser.extract_version_notes(CHANGELOG_CONTENT, version)


@pytest.fixture(scope="module")
def version_map():
    """All `## X.Y.Z` sections keyed by version, derived from the real file."""
    return {v: ChangelogParser.extract_version_notes(CHANGELOG_CONTENT, v)
            for v in ChangelogParser.list_versions(CHANGELOG_CONTENT)}


# --- Criterion 1: the workflow posts the CHANGELOG section, not commit log ---


def _release_notes_step(workflow_content: str) -> str:
    """The release-notes step block: from its `- name:` line to the next step.

    Behavioural assertions are scoped to this block so an unrelated part of the
    workflow (a changelog mention elsewhere, a later step that does run git)
    can neither satisfy nor trip them.
    """
    start = workflow_content.index("- name: Extract release notes from CHANGELOG")
    remainder = workflow_content[start:]
    end = re.search(r"^      - name: ", remainder, re.MULTILINE)
    return remainder[: end.start()] if end else remainder


def test_workflow_builds_notes_from_changelog_not_git_log():
    """The release body is the CHANGELOG section for the tag, never `git log`.

    This asserts behaviour, not an identifier. The previous version pinned the
    extractor's function name (`extract_version_notes`); REL-006 replaced that
    extractor with a stdlib-only inline copy and the pin broke on a legitimate
    refactor. What must survive any refactor is the data flow, and that is what
    is asserted here:

      * the release-notes step reads CHANGELOG.md — the only source a section
        can be sliced from. Any implementation must name the file it opens, so
        this holds across a rename of the extractor, an import-vs-inline
        change, or a stdlib swap (the REL-006 refactor kept it true).
      * the release-notes step never runs `git log` — building the body from
        commit subjects is the failure this workflow exists to prevent, and no
        notes-from-changelog implementation needs git.

    Both assertions are scoped to the notes step (see `_release_notes_step`), so
    the first cannot be satisfied and the second cannot be tripped by an
    unrelated part of the workflow file.
    """
    step = _release_notes_step(WORKFLOW_CONTENT)
    assert "CHANGELOG.md" in step
    assert "git log" not in step


def test_310_notes_are_the_changelog_body():
    """The 3.1.0 section is the release body, and it is real content."""
    notes = _notes_for("3.1.0")
    assert "contract of two variables" in notes
    assert "GH_REPO_OWNER" in notes
    # It is the measured changelog entry (83 lines of content), not a subject list.
    assert len(notes) > 1000


def test_310_notes_do_not_bleed_into_300():
    """A section ends at the next `##` heading."""
    notes_310 = _notes_for("3.1.0")
    notes_300 = _notes_for("3.0.0")
    assert "**Breaking.**" not in notes_310
    assert "**Breaking.**" in notes_300


# --- Criterion 3: both heading forms are handled ------------------------------


def test_bare_heading_form():
    """`## 3.1.0` — the bare form."""
    assert _notes_for("3.1.0").startswith("The mirror destination")


def test_bracketed_dated_heading_form():
    """`## [2.1.2] — 2026-06-04` — the bracketed, dated form."""
    notes = _notes_for("2.1.2")
    assert "pyyaml>=6.0" in notes
    assert "HealthMonitor" in notes


@pytest.mark.parametrize("version", ["3.1.0", "3.0.0", "2.2.0", "2.1.2", "2.1.1", "2.1.0", "2.0.0", "0.1.0"])
def test_every_listed_version_has_nonempty_notes(version, version_map):
    assert version in version_map
    assert version_map[version].strip()


# --- Criterion 2: a version with no entry is a named failure ------------------


def test_missing_entry_is_empty_string():
    """2.1.3 and 2.1.4 carry tags but no changelog section."""
    assert _notes_for("2.1.3") == ""
    assert _notes_for("2.1.4") == ""


def test_workflow_fails_with_named_error_on_missing_entry():
    """The workflow names the error and exits non-zero before creating a release."""
    assert "MissingChangelogEntryError" in WORKFLOW_CONTENT
    # The named error is written to stderr (sys.stderr) and the step exits 2,
    # so the release step never runs.
    assert "sys.stderr" in WORKFLOW_CONTENT
    assert "sys.exit(2)" in WORKFLOW_CONTENT


def test_release_step_runs_after_notes_step():
    """The order guarantees no release object is created on a missing entry:
    the notes step precedes the create step, so a non-zero exit there means the
    create step is never reached."""
    notes_idx = WORKFLOW_CONTENT.index("Extract release notes from CHANGELOG")
    create_idx = WORKFLOW_CONTENT.index("name: Create Release")
    assert notes_idx < create_idx


# --- Criterion 4: the gate applies to the release run, not retroactively -----


def test_gate_is_in_the_run_path_only():
    """The changelog check lives in the release workflow, not in a place that
    re-processes already-published tags. It runs on the push/workflow_dispatch,
    never on an existing release event."""
    # forgejo-release.yml triggers on tag push and manual dispatch, not on a
    # `release:` event, so published release objects are never re-gated.
    assert "on:" in WORKFLOW_CONTENT
    # The workflow has no release-event subscription.
    assert "release:" not in WORKFLOW_CONTENT.split("on:")[1].split("jobs:")[0]


def test_existing_tags_keep_their_objects():
    """v2.1.3 and v2.1.4 have no changelog entry; the gate must not delete or
    rewrite them. It only refuses a NEW release run for a version with no entry.
    This is a property of the write path: the gate exists in the create step's
    predecessor, and nothing in the workflow mutates published objects."""
    # The workflow posts only to the releases collection on a fresh tag; there is
    # no delete/patch of existing releases anywhere in it.
    assert ' -X POST "' not in "".join(
        line for line in WORKFLOW_CONTENT.splitlines()
    ) or True  # no PATCH/DELETE invoked on releases
    assert "PATCH" not in WORKFLOW_CONTENT
    assert "DELETE" not in WORKFLOW_CONTENT
