"""OE-010 criterion 1: the reconcile entry point publishes the missing releases.

Per release-enabled repository it compares the tags that match the release
pattern with the release objects on every release destination and publishes
each missing one through the OE-009 path (gates, tag identity, publication).
It never touches the mirror state sync.
"""

from unittest.mock import AsyncMock

import pytest

from ops_engine.config_loader import OpsEngineConfig, ReleaseConfig
from ops_engine.modules import mirror as mirror_module
from ops_engine.modules import release as release_module
from ops_engine.modules.reconcile import ReconcileHandler
from ops_engine.modules.release import ReleaseContext, TagIdentitySpec

REPO = "exampleorg/engine"
CHANGELOG = (
    "# Changes\n\n## v1.2.3\n\n- Faster start.\n\n## v1.2.2\n\n- Older.\n"
)
OPS_YAML_ONE = """\
destinations:
  - {forge: forgejo, repo: exampleorg/engine, role: release}
  - {forge: github, repo: exampleorg/mirror-only, role: mirror, visibility: private}
"""
OPS_YAML_TWO = """\
destinations:
  - {forge: forgejo, repo: exampleorg/engine, role: release}
  - {forge: github, repo: exampleorg/engine-pub, role: release, visibility: public}
"""
CONFIG = ReleaseConfig(enabled=True, trigger="tag_push", tag_pattern="v*")


def _ops_config() -> OpsEngineConfig:
    return OpsEngineConfig.load({"orgs": {"exampleorg": {"repositories": {"engine": {}}}}})


def _source(tags, ops_yaml=OPS_YAML_ONE):
    source = AsyncMock()
    source.list_tags = AsyncMock(return_value={t: "a" * 40 for t in tags})

    async def get_file_content(repo, path, ref="main"):
        content = {"CHANGELOG.md": CHANGELOG, ".ops.yaml": ops_yaml}[path]
        if content is None:
            raise FileNotFoundError(path)
        return content

    source.get_file_content = get_file_content
    return source


def _target(existing=()):
    target = AsyncMock()

    async def release_exists(repo, tag):
        return tag in existing

    target.release_exists = AsyncMock(side_effect=release_exists)
    target.create_release = AsyncMock(return_value={"id": 7})
    return target


def _context(tmp_path, adapters, monkeypatch):
    (tmp_path / "dist").mkdir(exist_ok=True)
    identity = AsyncMock()
    monkeypatch.setattr(release_module, "push_canonical_tag", identity)
    context = ReleaseContext(
        ops_config=_ops_config(),
        artifact_dir=tmp_path / "dist",
        identity=TagIdentitySpec(
            repo_dir=str(tmp_path),
            canonical_url="https://forge.example/exampleorg/engine.git",
            canonical_token="tok",
            mirror_remote="https://mirror.example/exampleorg/engine.git",
        ),
        adapters=adapters,
    )
    return context, identity


@pytest.mark.asyncio
async def test_publishes_only_the_matching_tag_that_has_no_release(tmp_path, monkeypatch):
    source = _source(["v1.2.2", "v1.2.3", "nightly"])
    target = _target(existing={"v1.2.2"})
    context, identity = _context(tmp_path, [target], monkeypatch)

    report = await ReconcileHandler.reconcile_release_tags(source, REPO, CONFIG, context)

    assert report.published == ["v1.2.3"]
    assert report.failed == {}
    target.create_release.assert_called_once()
    assert target.create_release.call_args.kwargs["tag_name"] == "v1.2.3"
    assert target.create_release.call_args.kwargs["body"].strip() == "- Faster start."
    identity.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_tag_missing_on_one_of_two_destinations_is_published_only_there(
    tmp_path, monkeypatch
):
    source = _source(["v1.2.3"], ops_yaml=OPS_YAML_TWO)
    has_it = _target(existing={"v1.2.3"})
    lacks_it = _target()
    # Destination order follows .ops.yaml: forgejo first, github second.
    context, _ = _context(tmp_path, [has_it, lacks_it], monkeypatch)

    report = await ReconcileHandler.reconcile_release_tags(source, REPO, CONFIG, context)

    assert report.published == ["v1.2.3"]
    has_it.create_release.assert_not_called()
    lacks_it.create_release.assert_called_once()


@pytest.mark.asyncio
async def test_nothing_missing_publishes_nothing_and_pushes_no_tag(tmp_path, monkeypatch):
    source = _source(["v1.2.2", "v1.2.3"])
    target = _target(existing={"v1.2.2", "v1.2.3"})
    context, identity = _context(tmp_path, [target], monkeypatch)

    report = await ReconcileHandler.reconcile_release_tags(source, REPO, CONFIG, context)

    assert report.published == []
    target.create_release.assert_not_called()
    identity.assert_not_called()


@pytest.mark.asyncio
async def test_repository_without_release_enabled_is_not_read(tmp_path, monkeypatch):
    source = _source(["v1.2.3"])
    target = _target()
    context, _ = _context(tmp_path, [target], monkeypatch)

    report = await ReconcileHandler.reconcile_release_tags(
        source, REPO, ReleaseConfig(enabled=False), context
    )

    assert report.published == []
    source.list_tags.assert_not_called()
    target.release_exists.assert_not_called()


@pytest.mark.asyncio
async def test_a_refused_tag_is_recorded_and_the_others_still_publish(tmp_path, monkeypatch):
    # v1.2.1 has no CHANGELOG section: the gate refuses it, v1.2.3 still goes out.
    source = _source(["v1.2.1", "v1.2.3"])
    target = _target()
    context, _ = _context(tmp_path, [target], monkeypatch)

    report = await ReconcileHandler.reconcile_release_tags(source, REPO, CONFIG, context)

    assert report.published == ["v1.2.3"]
    assert list(report.failed) == ["v1.2.1"]
    assert "MissingChangelogEntryError" in report.failed["v1.2.1"]


@pytest.mark.asyncio
async def test_the_release_cycle_never_calls_the_mirror_state_sync(tmp_path, monkeypatch):
    boom = AsyncMock(side_effect=AssertionError("mirror sync must not run"))
    monkeypatch.setattr(mirror_module.MirrorHandler, "sync_state", boom)
    monkeypatch.setattr(ReconcileHandler, "reconcile_mirror", boom)
    source = _source(["v1.2.3"])
    context, _ = _context(tmp_path, [_target()], monkeypatch)

    report = await ReconcileHandler.reconcile_release_tags(source, REPO, CONFIG, context)

    assert report.published == ["v1.2.3"]
    boom.assert_not_called()
