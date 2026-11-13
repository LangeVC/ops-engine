"""P36-017 F3 proof: RC tag regex covers both -rcN and -rc.N forms.

The engine's ``_RC_TAG_RE`` and the workflow's inline copy must both be
driven with the same list of tags. Tests go red when either regex is reverted
to the form ``r"^v\d+\.\d+\.\d+-rc\d+$"`` (no optional dot).

The list of tags from the owner's NOTES:
  RC (must be skipped):  v1.2.3-rc1, v1.2.3-rc.1, v1.2.3-rc.10
  Non-RC (must NOT be skipped):  v1.2.3, v1.2.3-beta.1, v1.2.30
"""

import os
import re

import pytest

from ops_engine.modules.release import _RC_TAG_RE

# ---------------------------------------------------------------------------
# Shared tag catalogue — same list drives both the engine and the workflow
# ---------------------------------------------------------------------------

RC_TAGS = [
    "v1.2.3-rc1",     # no dot between -rc and the number
    "v1.2.3-rc.1",    # dot between -rc and the number
    "v1.2.3-rc.10",   # multi-digit after the dot
]

NON_RC_TAGS = [
    "v1.2.3",          # plain semver, no pre-release suffix
    "v1.2.3-beta.1",   # non-RC pre-release suffix
    "v1.2.30",         # different version, no suffix
]

ALL_TAGS = [(t, True) for t in RC_TAGS] + [(t, False) for t in NON_RC_TAGS]

WORKFLOW_PATH = ".forgejo/workflows/forgejo-release.yml"


# ---------------------------------------------------------------------------
# Engine path: _RC_TAG_RE  (release.py)
# ---------------------------------------------------------------------------


class TestEngineRCRegex:
    """The engine's ``_RC_TAG_RE`` must match all RC forms and reject
    non-RC forms.  Goes red when the regex is reverted."""

    @pytest.mark.parametrize("tag", RC_TAGS)
    def test_rc_tags_match(self, tag: str) -> None:
        """Every RC tag form matches the engine regex."""
        assert _RC_TAG_RE.match(tag), (
            f"RC tag {tag!r} should match _RC_TAG_RE"
        )

    @pytest.mark.parametrize("tag", NON_RC_TAGS)
    def test_non_rc_tags_do_not_match(self, tag: str) -> None:
        """Every non-RC tag form is rejected by the engine regex."""
        assert not _RC_TAG_RE.match(tag), (
            f"Non-RC tag {tag!r} should not match _RC_TAG_RE"
        )


# ---------------------------------------------------------------------------
# Workflow path: inline regex in forgejo-release.yml
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow_rc_re() -> re.Pattern:
    """Read the workflow file and compile the inline RC regex.

    The workflow builds the regex as::

        __import__("re").match(r"...", tag_name)

    We extract the pattern from the source to ensure the test tracks
    the actual file content.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    workflow_file = os.path.join(repo_root, WORKFLOW_PATH)
    with open(workflow_file) as f:
        content = f.read()

    m = re.search(
        r'__import__\("re"\)\.match\(r"([^"]+)"',
        content,
    )
    assert m, (
        f"Could not find __import__('re').match(r'...') in {workflow_file}"
    )
    pattern = m.group(1)
    compiled = re.compile(pattern)
    return compiled


class TestWorkflowInlineRegex:
    """The inline regex in ``forgejo-release.yml`` must match all RC forms
    and reject non-RC forms.  Goes red when the workflow copy is reverted."""

    @pytest.mark.parametrize("tag", RC_TAGS)
    def test_rc_tags_match(self, workflow_rc_re: re.Pattern, tag: str) -> None:
        """Every RC tag form matches the workflow inline regex."""
        assert workflow_rc_re.match(tag), (
            f"RC tag {tag!r} should match the workflow inline regex"
        )

    @pytest.mark.parametrize("tag", NON_RC_TAGS)
    def test_non_rc_tags_do_not_match(
        self, workflow_rc_re: re.Pattern, tag: str,
    ) -> None:
        """Every non-RC tag form is rejected by the workflow inline regex."""
        assert not workflow_rc_re.match(tag), (
            f"Non-RC tag {tag!r} should not match the workflow inline regex"
        )
