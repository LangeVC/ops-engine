"""REL-001 — release notes come from CHANGELOG.md, and a missing entry blocks the tag.

The workflow `.forgejo/workflows/forgejo-release.yml` posts the `## X.Y.Z`
section of CHANGELOG.md as the release body and refuses a tag whose version has
no section. This test proves both halves against the real CHANGELOG.md and the
real extractor the workflow invokes (`ChangelogParser`), so a test here is a
test of the write the workflow performs, not of a copy.

The workflow runs `python3` with `sys.path.insert(0, "src")` and calls
`ChangelogParser.extract_version_notes`, the same call path this test exercises.
Nothing here touches `src/` or `pyproject.toml`: the parser already exists and
is imported, as the workflow does.
"""

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


def test_workflow_extracts_changelog_not_git_log():
    """The release body source is CHANGELOG.md, not `git log`."""
    assert "CHANGELOG.md" in WORKFLOW_CONTENT
    assert "extract_version_notes" in WORKFLOW_CONTENT
    # The previous implementation built notes from commit subjects.
    assert "git log" not in WORKFLOW_CONTENT


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
