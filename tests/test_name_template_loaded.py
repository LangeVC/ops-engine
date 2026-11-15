"""P36-021 criteria 1-3: a configured ``name_template`` is loaded and used.

Criterion 1: a ``name_template`` declared in the release block of a layover
``config.yml`` and of a repository ``.ops.yaml`` is loaded and reaches
``render_release_name`` and ``check_release_title``; a repository without one
keeps the derived default.

Criterion 2: a ``name_template`` that does not render to the gated title form
fails that repository by name and leaves the others running.

Criterion 3: the fusionaize-ops, lvc-ops and capacium-ops style blocks (carried
as fixtures) load and render the expected title, asserted through the engine's
own loader and not through the YAML text.

Nothing here reads a template out of a YAML string: every expectation goes
through ``OpsEngineConfig.load`` / ``load_ops_yaml`` and the pydantic model.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from ops_engine.config_loader import (
    OpsEngineConfig,
    ReleaseConfig,
    load_ops_yaml,
)
from ops_engine.modules import release as release_module
from ops_engine.modules.release import (
    DEFAULT_NAME_TEMPLATE,
    ReleaseContext,
    ReleaseHandler,
    TagIdentitySpec,
    render_release_name,
)
from ops_engine.modules.release_gates import ReleaseTitleError, check_release_title

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ORG = "langevc"
REPO_NAME = "ops-engine"
REPO = f"{ORG}/{REPO_NAME}"
TAG = "v3.6.2"
CHANGELOG = "# Changes\n\n## v3.6.2\n\n- Faster start.\n\n## v3.6.1\n\n- Older.\n"


# --- helpers -----------------------------------------------------------------


def _release_block(**overrides) -> dict:
    block = {
        "enabled": True,
        "trigger": "tag_push",
        "tag_pattern": "v*",
        "changelog_path": "CHANGELOG.md",
    }
    block.update(overrides)
    return block


def _layover_config(*, org=ORG, repos=None):
    """Load a layover mapping with the given ``{repo: release_block}`` entries."""
    if repos is None:
        repos = {REPO_NAME: _release_block(name_template="Ops Engine {tag_name}")}
    return OpsEngineConfig.load(
        {"orgs": {org: {"repositories": {name: {"release": block} for name, block in repos.items()}}}}
    )


def _fixture_config(filename: str) -> OpsEngineConfig:
    """Load a fixture exactly as the layover loads its config file."""
    data = yaml.safe_load((FIXTURES / filename).read_text(encoding="utf-8"))
    return OpsEngineConfig.load(data)


def _destination(repo: str) -> str:
    return (
        "destinations:\n"
        f"  - {{forge: forgejo, repo: {repo}, role: release, "
        "credential_variable: FORGEJO_TOKEN}\n"
    )


def _event(repo: str = REPO, tag: str = TAG) -> dict:
    return {
        "event_type": "push",
        "action": None,
        "repo": repo,
        "raw": {"ref": f"refs/tags/{tag}"},
    }


def _release_context(tmp_path, monkeypatch, *, config, ops_yaml, context_template=None):
    """Drive the tag push with stubs; returns (source, target, identity, context)."""
    (tmp_path / "dist").mkdir(exist_ok=True)
    identity = AsyncMock()
    monkeypatch.setattr(release_module, "push_canonical_tag", identity)

    source = AsyncMock()

    async def get_file_content(repo, path, ref="main"):
        if path == ".ops.yaml":
            return ops_yaml(repo) if callable(ops_yaml) else ops_yaml
        return CHANGELOG

    source.get_file_content = get_file_content
    target = AsyncMock()
    target.release_exists = AsyncMock(return_value=False)
    target.create_release = AsyncMock(return_value={"id": 1})

    context = ReleaseContext(
        ops_config=config,
        artifact_dir=tmp_path / "dist",
        identity=TagIdentitySpec(
            repo_dir=str(tmp_path),
            canonical_url="https://forge.example/langevc/ops-engine.git",
            canonical_token="tok",
            mirror_remote="https://mirror.example/langevc/ops-engine.git",
        ),
        adapters=[target],
        name_template=context_template,
    )
    return source, target, identity, context


# --- criterion 1: the layer-2 release block loads the template ----------------


def test_layover_release_block_loads_the_name_template():
    config = _layover_config()
    resolved = config.get_repo_config(ORG, REPO_NAME)

    assert isinstance(resolved.release, ReleaseConfig)
    assert resolved.release.name_template == "Ops Engine {tag_name}"

    # It reaches the rendering and the title gate, not just the model.
    assert render_release_name(resolved.release.name_template, REPO, TAG) == "Ops Engine v3.6.2"
    assert (
        check_release_title(TAG, name_template=resolved.release.name_template)
        == "Ops Engine v3.6.2"
    )


def test_repository_without_a_template_keeps_the_derived_default():
    config = _layover_config(repos={REPO_NAME: _release_block()})
    resolved = config.get_repo_config(ORG, REPO_NAME)

    assert resolved.release.name_template is None
    assert DEFAULT_NAME_TEMPLATE == "{repo_name} {tag_name}"
    assert (
        render_release_name(
            resolved.release.name_template or DEFAULT_NAME_TEMPLATE, REPO, TAG
        )
        == "ops-engine v3.6.2"
    )


def test_repo_ops_yaml_release_block_loads_a_name_template(tmp_path):
    (tmp_path / ".ops.yaml").write_text(
        "release:\n  name_template: \"Ops Engine LTS {tag_name}\"\n", encoding="utf-8"
    )
    layer3 = load_ops_yaml(tmp_path)

    assert isinstance(layer3.release, ReleaseConfig)
    assert layer3.release.name_template == "Ops Engine LTS {tag_name}"
    assert (
        check_release_title(TAG, name_template=layer3.release.name_template)
        == "Ops Engine LTS v3.6.2"
    )


@pytest.mark.asyncio
async def test_the_layover_template_is_the_published_name_and_body_heading(
    tmp_path, monkeypatch
):
    config = _layover_config()
    source, target, identity, context = _release_context(
        tmp_path, monkeypatch, config=config, ops_yaml=_destination(REPO)
    )

    await ReleaseHandler.process_event(
        source, _event(), config.get_repo_config(ORG, REPO_NAME).release, context
    )

    kwargs = target.create_release.await_args.kwargs
    assert kwargs["name"] == "Ops Engine v3.6.2"
    assert "## Ops Engine v3.6.2" in kwargs["body"]
    identity.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_repository_declaration_wins_over_the_layover_template(
    tmp_path, monkeypatch
):
    """The ``.ops.yaml`` release block is read at the tag and outranks Layer 2."""
    config = _layover_config()
    ops_yaml = (
        _destination(REPO)
        + "release:\n  name_template: \"Ops Engine LTS {tag_name}\"\n"
    )
    source, target, identity, context = _release_context(
        tmp_path, monkeypatch, config=config, ops_yaml=ops_yaml
    )

    await ReleaseHandler.process_event(
        source, _event(), config.get_repo_config(ORG, REPO_NAME).release, context
    )

    assert target.create_release.await_args.kwargs["name"] == "Ops Engine LTS v3.6.2"


@pytest.mark.asyncio
async def test_a_repository_without_a_template_keeps_the_layover_default(
    tmp_path, monkeypatch
):
    """A ``.ops.yaml`` that declares no template does not reset the layover one."""
    config = _layover_config()
    source, target, identity, context = _release_context(
        tmp_path, monkeypatch, config=config, ops_yaml=_destination(REPO)
    )

    await ReleaseHandler.process_event(
        source, _event(), config.get_repo_config(ORG, REPO_NAME).release, context
    )

    assert target.create_release.await_args.kwargs["name"] == "Ops Engine v3.6.2"


@pytest.mark.asyncio
async def test_an_explicit_context_template_still_wins(tmp_path, monkeypatch):
    """The workflow passes NAME_TEMPLATE through the context; it keeps priority."""
    config = _layover_config()
    source, target, identity, context = _release_context(
        tmp_path,
        monkeypatch,
        config=config,
        ops_yaml=_destination(REPO),
        context_template="Ops Engine (workflow) {tag_name}",
    )

    await ReleaseHandler.process_event(
        source, _event(), config.get_repo_config(ORG, REPO_NAME).release, context
    )

    assert (
        target.create_release.await_args.kwargs["name"]
        == "Ops Engine (workflow) v3.6.2"
    )


# --- criterion 2: a non-conforming template fails that repository by name -----

# The gated title form is built around exactly one ``{tag_name}``. A template
# without it renders a title that carries no tag at all, so the gate refuses it
# for this repository before any release object exists.
BAD_TEMPLATE = "Ops Engine (unreleased)"


@pytest.mark.asyncio
async def test_a_non_conforming_template_fails_that_repository_by_name(
    tmp_path, monkeypatch
):
    config = _layover_config(
        repos={
            REPO_NAME: _release_block(name_template=BAD_TEMPLATE),
            "envctl": _release_block(name_template="Ops Engine {tag_name}"),
        }
    )
    source, target, identity, context = _release_context(
        tmp_path, monkeypatch, config=config, ops_yaml=_destination(REPO)
    )

    with pytest.raises(ReleaseTitleError) as excinfo:
        await ReleaseHandler.process_event(
            source, _event(), config.get_repo_config(ORG, REPO_NAME).release, context
        )

    assert REPO in str(excinfo.value)
    assert "unreleased" in str(excinfo.value)
    identity.assert_not_awaited()
    target.create_release.assert_not_called()

    # The other repository of the same loaded config still runs and publishes.
    good_repo = "langevc/envctl"
    source2, target2, identity2, context2 = _release_context(
        tmp_path, monkeypatch, config=config, ops_yaml=_destination(good_repo)
    )
    await ReleaseHandler.process_event(
        source2, _event(repo=good_repo), config.get_repo_config(ORG, "envctl").release, context2
    )
    assert target2.create_release.await_args.kwargs["name"] == "Ops Engine v3.6.2"
    identity2.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_ops_yaml_template_is_the_one_the_title_gate_checks(
    tmp_path, monkeypatch
):
    """A bad template declared in the repository's own release block is refused
    by the gate, naming the repository — the layover's good one does not mask it."""
    config = _layover_config()
    ops_yaml = _destination(REPO) + f"release:\n  name_template: \"{BAD_TEMPLATE}\"\n"
    source, target, identity, context = _release_context(
        tmp_path, monkeypatch, config=config, ops_yaml=ops_yaml
    )

    with pytest.raises(ReleaseTitleError) as excinfo:
        await ReleaseHandler.process_event(
            source, _event(), config.get_repo_config(ORG, REPO_NAME).release, context
        )

    assert REPO in str(excinfo.value)
    target.create_release.assert_not_called()
    identity.assert_not_awaited()


# --- criterion 3: the owner-style blocks, through the engine's loader ---------


OWNER_STYLE_BLOCKS = [
    (
        "name_template_fusionaize.yml",
        "fusionaize",
        "faigrid",
        "fusionAIze Grid {tag_name}",
        "v1.10.0",
        "fusionAIze Grid v1.10.0",
    ),
    (
        "name_template_fusionaize.yml",
        "fusionaize",
        "failens",
        "fusionAIze Lens {tag_name}",
        "v1.8.0",
        "fusionAIze Lens v1.8.0",
    ),
    (
        "name_template_lvc.yml",
        "langevc",
        "ops-engine",
        "Ops Engine {tag_name}",
        "v3.6.2",
        "Ops Engine v3.6.2",
    ),
    (
        "name_template_capacium.yml",
        "capacium",
        "capacium",
        "Capacium {tag_name}",
        "v0.9.0",
        "Capacium v0.9.0",
    ),
]


@pytest.mark.parametrize(
    "fixture, org, repo_name, template, tag, expected",
    OWNER_STYLE_BLOCKS,
    ids=["fusionaize-grid", "fusionaize-lens", "lvc-ops", "capacium"],
)
def test_owner_style_blocks_load_and_render_the_expected_title(
    fixture, org, repo_name, template, tag, expected
):
    config = _fixture_config(fixture)
    resolved = config.get_repo_config(org, repo_name)

    assert isinstance(resolved.release, ReleaseConfig)
    assert resolved.release.name_template == template
    # Both halves of the criterion, through the loader's model:
    assert render_release_name(resolved.release.name_template, f"{org}/{repo_name}", tag) == expected
    assert check_release_title(tag, name_template=resolved.release.name_template) == expected


def test_the_lvc_style_block_tolerates_its_undeclared_key():
    """``assets: none`` is not an engine field and must stay an ignored extra."""
    resolved = _fixture_config("name_template_lvc.yml").get_repo_config("langevc", "ops-engine")
    assert "assets" not in ReleaseConfig.model_fields
    assert resolved.release.name_template == "Ops Engine {tag_name}"
    assert resolved.release.enabled is True


@pytest.mark.parametrize(
    "fixture, org, repo_name, template, tag, expected",
    OWNER_STYLE_BLOCKS,
    ids=["fusionaize-grid", "fusionaize-lens", "lvc-ops", "capacium"],
)
def test_the_same_block_without_the_key_renders_the_derived_default(
    fixture, org, repo_name, template, tag, expected
):
    """Contrast: drop only the key and the same block is back to the default."""
    data = yaml.safe_load((FIXTURES / fixture).read_text(encoding="utf-8"))
    for repos in (org_cfg["repositories"] for org_cfg in data["orgs"].values()):
        for repo_cfg in repos.values():
            repo_cfg.get("release", {}).pop("name_template", None)

    resolved = OpsEngineConfig.load(data).get_repo_config(org, repo_name)
    assert resolved.release.name_template is None
    rendered = render_release_name(DEFAULT_NAME_TEMPLATE, f"{org}/{repo_name}", tag)
    assert rendered == f"{repo_name} {tag}"
    assert rendered != expected
