"""FFR-200-4: release name comes from a configured name_template.

Covers the three criteria of this lane:

1. ReleaseHandler takes the release name from a configured ``name_template``
   with placeholders.
2. The product prefix lives in the layover; the generic template carries no
   product name.
3. One configured convention renders the identical name on Forgejo and GitHub.
"""

from unittest.mock import AsyncMock

import pytest

from ops_engine.modules.release import ReleaseHandler, render_release_name


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
