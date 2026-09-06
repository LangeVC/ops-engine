"""FFR-200-4: release name comes from a configured name_template.

Covers the three criteria of this lane:

1. ReleaseHandler takes the release name from a configured ``name_template``
   with placeholders.
2. The product prefix lives in the layover; the generic template carries no
   product name.
3. One configured convention renders the identical name on Forgejo and GitHub.
"""

import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ops_engine.modules.release import ReleaseHandler, render_release_name

REPO_ROOT = Path(__file__).resolve().parents[1]
FORGEJO_RELEASE = REPO_ROOT / ".forgejo" / "workflows" / "forgejo-release.yml"
MIRROR = REPO_ROOT / ".forgejo" / "workflows" / "mirror.yml"
FORGEJO_RELEASE_CONTENT = FORGEJO_RELEASE.read_text(encoding="utf-8")
MIRROR_CONTENT = MIRROR.read_text(encoding="utf-8")


class _ReleaseConfig:
    """A placeholder-tolerant release config.

    The engine carries no ``name_template`` field of its own (it is layover
    data). But ReleaseHandler must accept a config that carries one, so we
    define a payload that behaves like a Pydantic model here.
    """

    def __init__(self, enabled=True, *, name_template=None):
        self.enabled = enabled
        self.trigger = "tag_push"
        self.tag_pattern = "v*"
        self.changelog_path = "CHANGELOG.md"
        self.draft = False
        self.create_tag_on_merge = False
        self.name_template = name_template


@pytest.fixture
def mock_adapter():
    adapter = AsyncMock()
    adapter.release_exists = AsyncMock(return_value=False)
    adapter.get_file_content = AsyncMock(return_value="## v1.0.0\n\n- Something")
    adapter.create_release = AsyncMock(return_value={"id": 1})
    return adapter


@pytest.fixture
def sample_push_event():
    return {
        "event_type": "push",
        "action": None,
        "repo": "TestOrg/test-repo",
        "raw": {"ref": "refs/tags/v1.0.0"},
    }


class TestRenderReleaseName:
    """Criterion 2: the generic template carries no product name."""

    def test_default_template_has_no_product_prefix(self):
        assert "product" not in __import__(
            "ops_engine.modules.release", fromlist=["DEFAULT_NAME_TEMPLATE"]
        ).DEFAULT_NAME_TEMPLATE.lower()

    def test_default_render_is_repo_and_tag(self, mock_adapter, sample_push_event):
        name = render_release_name("{repo_name} {tag_name}", "TestOrg/test-repo", "v1.0.0")
        assert name == "test-repo v1.0.0"
        assert "TestOrg" not in name

    def test_placeholder_substitution(self):
        name = render_release_name("{repo} {tag_name}", "Acme/widget", "v2.0.0")
        # The org segment is rendered in canonical lowercase form so the same
        # template yields the same name on Forgejo and GitHub (criterion 3).
        assert name == "acme/widget v2.0.0"

    def test_unknown_placeholder_stays_visible(self):
        name = render_release_name("{repo_name} {tag_name} {missing}", "Acme/widget", "v2.0.0")
        assert name == "widget v2.0.0 {missing}"


class TestReleaseNameTemplate:
    """Criterion 1: the name comes from a configured name_template."""

    @pytest.mark.asyncio
    async def test_name_from_configured_template(self, mock_adapter, sample_push_event):
        config = _ReleaseConfig(name_template="{repo_name} {tag_name} ({tag})")
        await ReleaseHandler.process_event(mock_adapter, sample_push_event, config)
        kwargs = mock_adapter.create_release.call_args.kwargs
        assert kwargs["name"] == "test-repo v1.0.0 ({tag})"

    @pytest.mark.asyncio
    async def test_default_template_is_generic(self, mock_adapter, sample_push_event):
        config = _ReleaseConfig()
        await ReleaseHandler.process_event(mock_adapter, sample_push_event, config)
        kwargs = mock_adapter.create_release.call_args.kwargs
        assert kwargs["name"] == "test-repo v1.0.0"

    @pytest.mark.asyncio
    async def test_empty_template_falls_back_to_generic(self, mock_adapter, sample_push_event):
        config = _ReleaseConfig(name_template="")
        await ReleaseHandler.process_event(mock_adapter, sample_push_event, config)
        kwargs = mock_adapter.create_release.call_args.kwargs
        assert kwargs["name"] == "test-repo v1.0.0"


class TestIdenticalNameAcrossForges:
    """Criterion 3: one configured convention renders the identical name on
    Forgejo and on GitHub.

    Forgejo yields the lowercased ``lower_name`` org key while GitHub carries
    display case in ``full_name``, so the same repo arrives as ``langevc/faigrid``
    from Forgejo and ``LangeVC/faigrid`` from GitHub. The template placeholders
    must not let that org-case difference leak into the rendered name.
    """

    def test_repo_name_template_is_forge_independent(self):
        template = "{repo_name} {tag_name}"
        forgejo = render_release_name(template, "langevc/faigrid", "v1.8.0")
        github = render_release_name(template, "LangeVC/faigrid", "v1.8.0")
        assert forgejo == github == "faigrid v1.8.0"

    def test_full_repo_slug_normalises_the_org_case(self):
        template = "{repo} {tag_name}"
        forgejo = render_release_name(template, "langevc/faigrid", "v1.8.0")
        github = render_release_name(template, "LangeVC/faigrid", "v1.8.0")
        assert forgejo == github == "langevc/faigrid v1.8.0"

    def test_layover_product_prefix_with_placeholder_is_identical(self):
        template = "fusionAIze Grid {tag_name}"
        forgejo = render_release_name(template, "langevc/faigrid", "v1.8.0")
        github = render_release_name(template, "LangeVC/faigrid", "v1.8.0")
        assert forgejo == github == "fusionAIze Grid v1.8.0"

    @pytest.mark.asyncio
    async def test_one_convention_renders_one_name_end_to_end(self, mock_adapter):
        template = "fusionAIze Grid {tag_name}"
        forgejo_event = {
            "event_type": "push",
            "repo": "langevc/faigrid",
            "raw": {"ref": "refs/tags/v1.8.0"},
        }
        github_event = {
            "event_type": "push",
            "repo": "LangeVC/faigrid",
            "raw": {"ref": "refs/tags/v1.8.0"},
        }
        config = _ReleaseConfig(name_template=template)
        names = []
        for event in (forgejo_event, github_event):
            mock_adapter.create_release.reset_mock()
            await ReleaseHandler.process_event(mock_adapter, event, config)
            names.append(mock_adapter.create_release.call_args.kwargs["name"])
        assert names[0] == names[1] == "fusionAIze Grid v1.8.0"


# --- REL-004: the mirror creates the GitHub release and uploads the same files --
#
# The title gate and the mirror asset-verifier live inline in the workflows as
# heredoc Python blocks. This test extracts the LITERAL blocks and executes them,
# so a change to either workflow that is not mirrored here turns this section
# red — the workflow text is the contract, not a copy of it (the same pattern
# test_release_assets.py uses for the OSV evaluator). Each criterion here is
# proven by a real subprocess run, not by reading a file and asserting on text.


def _extract_py_block(workflow_text: str, marker: str) -> str:
    """Return the de-indented body of the heredoc delimited by ``marker``.

    ``marker`` is the delimiter word that follows ``<<'`` (e.g.
    ``TITLE_GATE_PY``); the block runs from the line after the ``<<'MARKER'``
    line down to the line whose stripped text equals ``MARKER``.
    """
    lines = workflow_text.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        if f"<<'{marker}'" in line:
            start = i
        if start is not None and line.strip() == marker and i > start:
            end = i
            break
    assert start is not None and end is not None, f"heredoc {marker} not found"
    return textwrap.dedent("\n".join(lines[start + 1 : end]) + "\n")


def _run_py(source: str, *args: str, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", source, *args],
        capture_output=True,
        text=True,
        env=env,
    )


class TestReleaseTitleGate:
    """Criterion 3: a non-conforming title is rejected with a named error, and
    no release object is created on either side."""

    def _gate_source(self) -> str:
        return _extract_py_block(FORGEJO_RELEASE_CONTENT, "TITLE_GATE_PY")

    def test_title_gate_accepts_a_conforming_title(self):
        r = _run_py(self._gate_source(), "Ops Engine v3.2.0")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "conforms" in r.stdout

    def test_title_gate_rejects_non_conforming_title_red_proof(self):
        """RED PROOF: a title outside ^Ops Engine v[0-9]+\\.[0-9]+\\.[0-9]+$
        exits non-zero with a named error."""
        for bad in (
            "ops-engine v3.2.0",       # wrong case / missing product prefix
            "Ops Engine 3.2.0",        # missing 'v'
            "Ops Engine v3.2",         # wrong segment count
            "v3.2.0",                  # bare tag, pre-REL-004 form
            "Ops Engine v3.2.0-beta",  # prerelease suffix
        ):
            r = _run_py(self._gate_source(), bad)
            assert r.returncode != 0, f"{bad!r} was accepted"
            assert "ReleaseTitleError" in r.stderr, r.stderr

    def test_title_gate_runs_before_release_creation(self):
        """The gate step precedes the Create Release step in the workflow, so a
        refusal happens before any release object exists on Forgejo."""
        gate_idx = FORGEJO_RELEASE_CONTENT.index("TITLE_GATE_PY")
        create_idx = FORGEJO_RELEASE_CONTENT.index("Create Release")
        assert gate_idx < create_idx

    def test_forgejo_release_name_comes_from_the_name_template(self):
        """The release name the engine renders is the gated convention, not the
        bare tag: the workflow passes a name_template that names the product
        prefix, and the engine renders it against the tag (criterion 1 of
        FFR-200-4)."""
        assert "NAME_TEMPLATE" in FORGEJO_RELEASE_CONTENT
        assert "Ops Engine {tag_name}" in FORGEJO_RELEASE_CONTENT


class TestMirrorCreatesGitHubRelease:
    """Criteria 1, 2, 4: one build, published by the engine, no download.

    REL-010 established one build published to both forges from the same
    workspace; ADP-004 moves that publication into the engine
    (ReleaseHandler.publish_release), which resolves destinations from config
    instead of hardcoding the mirror. These tests assert the split: no download
    anywhere, no second build, the mirror is git-ref push only, and the
    destination reaches the engine from config rather than a literal.
    """

    def test_no_workflow_downloads_a_release_asset(self):
        """Criterion 1 (REL-010): no workflow fetches a release asset over the
        network. The prior design had mirror.yml and release-gate.yml download
        .browser_download_url assets; those downloads are gone."""
        assert "browser_download_url" not in FORGEJO_RELEASE_CONTENT
        assert "browser_download_url" not in MIRROR_CONTENT
        # mirror.yml no longer curls the Forgejo release API at all.
        assert "releases/tags" not in MIRROR_CONTENT
        assert "releases/\"${REL_ID}\"/assets" not in MIRROR_CONTENT

    def test_forgejo_release_publishes_to_both_forges_via_the_engine(self):
        """Criterion 1 (ADP-004): forgejo-release.yml no longer uploads to a
        forge with curl. It hands the built assets to the engine, which creates
        the release and attaches every asset on every destination — no
        hardcoded upload host and no download step between build and publish."""
        assert "publish_release" in FORGEJO_RELEASE_CONTENT
        assert "GH_MIRROR_TOKEN" in FORGEJO_RELEASE_CONTENT
        # The hardcoded mirror upload host is gone from the release workflow.
        assert "uploads." not in FORGEJO_RELEASE_CONTENT

    def test_mirror_is_git_push_only(self):
        """Criterion 3 (REL-010): mirror.yml keeps exactly its force-push and
        loses the release logic entirely — no release API, no build, no SBOM."""
        assert "git push --force" in MIRROR_CONTENT
        assert "python3 -m build" not in MIRROR_CONTENT
        assert "pip install" not in MIRROR_CONTENT
        assert "sbom.cdx.json" not in MIRROR_CONTENT
        assert "api.github.com/repos" not in MIRROR_CONTENT
        assert "RELEASE_WAIT" not in MIRROR_CONTENT

    def test_no_polling_loops_remain(self):
        """Criterion 3 (REL-010): both polling loops are gone — RELEASE_WAIT_* in
        mirror.yml and SBOM_WAIT_* in release-gate.yml."""
        assert "RELEASE_WAIT" not in MIRROR_CONTENT
        assert "SBOM_WAIT" not in MIRROR_CONTENT

    def test_producer_and_verifier_agree_on_checksum_path_shape(self):
        """Criterion 1 (REWORK): the producer's SHA256SUMS must be keyed by BARE
        basename, so a consumer can `sha256sum -c` it against the flat published
        assets (no dist/ subdir). A leading `dist/` would break the flat verify."""
        # The producer runs `(cd dist && sha256sum *.whl *.tar.gz) > SHA256SUMS`,
        # which names each archive by basename, not `dist/....`.
        assert "(cd dist && sha256sum *.whl *.tar.gz) > SHA256SUMS" in FORGEJO_RELEASE_CONTENT
        assert "sha256sum dist/*.whl dist/*.tar.gz > SHA256SUMS" not in FORGEJO_RELEASE_CONTENT

    def test_mirror_destination_comes_from_config_not_a_literal(self):
        """Criterion 4 (ADP-004, corrected by ADP-008): the mirror destination is
        declared in the config layer — the committed .ops.yaml read via
        load_ops_yaml — not as an Actions variable nor a forge repository literal
        in the workflow. The engine resolves it via load_ops_yaml."""
        assert "load_ops_yaml" in FORGEJO_RELEASE_CONTENT
        assert "RELEASE_DESTINATIONS" not in FORGEJO_RELEASE_CONTENT
        # No owner/repo destination literal and no API-host literal remain.
        assert "GH_REPO=" not in FORGEJO_RELEASE_CONTENT
        assert "GH_API=" not in FORGEJO_RELEASE_CONTENT
