"""OE-010 criterion 1: the reconcile entry point publishes the missing releases.

Per release-enabled repository it compares the tags that match the release
pattern with the release objects on every release destination and publishes
each missing one through the OE-009 path (gates, tag identity, publication).
It never touches the mirror state sync.
"""

from unittest.mock import AsyncMock

import pytest

from ops_engine.config_loader import OpsEngineConfig, ReconcileConfig, ReleaseConfig
from ops_engine.modules import mirror as mirror_module
from ops_engine.modules import release as release_module
from ops_engine.modules.reconcile import ReconcileHandler, ReconcileSinceError
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
SINCE = ReconcileConfig(since="v1.0.0")


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

    report = await ReconcileHandler.reconcile_release_tags(
        source, REPO, CONFIG, context, SINCE
    )

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

    report = await ReconcileHandler.reconcile_release_tags(
        source, REPO, CONFIG, context, SINCE
    )

    assert report.published == ["v1.2.3"]
    has_it.create_release.assert_not_called()
    lacks_it.create_release.assert_called_once()


@pytest.mark.asyncio
async def test_nothing_missing_publishes_nothing_and_pushes_no_tag(tmp_path, monkeypatch):
    source = _source(["v1.2.2", "v1.2.3"])
    target = _target(existing={"v1.2.2", "v1.2.3"})
    context, identity = _context(tmp_path, [target], monkeypatch)

    report = await ReconcileHandler.reconcile_release_tags(
        source, REPO, CONFIG, context, SINCE
    )

    assert report.published == []
    target.create_release.assert_not_called()
    identity.assert_not_called()


@pytest.mark.asyncio
async def test_repository_without_release_enabled_is_not_read(tmp_path, monkeypatch):
    source = _source(["v1.2.3"])
    target = _target()
    context, _ = _context(tmp_path, [target], monkeypatch)

    report = await ReconcileHandler.reconcile_release_tags(
        source, REPO, ReleaseConfig(enabled=False), context, SINCE
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

    report = await ReconcileHandler.reconcile_release_tags(
        source, REPO, CONFIG, context, SINCE
    )

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

    report = await ReconcileHandler.reconcile_release_tags(
        source, REPO, CONFIG, context, SINCE
    )

    assert report.published == ["v1.2.3"]
    boom.assert_not_called()


def _tagged_source(tags):
    source = _source(list(tags))
    source.list_tags = AsyncMock(return_value=dict(tags))
    return source


@pytest.mark.asyncio
async def test_refuses_without_since_and_names_the_setting(tmp_path, monkeypatch):
    source = _source(["v1.2.3"])
    context, _ = _context(tmp_path, [_target()], monkeypatch)

    for missing in (None, ReconcileConfig(), ReconcileConfig(since="")):
        with pytest.raises(ReconcileSinceError, match=r"reconcile\.since"):
            await ReconcileHandler.reconcile_release_tags(
                source, REPO, CONFIG, context, missing
            )
    source.list_tags.assert_not_called()


@pytest.mark.asyncio
async def test_since_tag_excludes_earlier_tags(tmp_path, monkeypatch):
    source = _source(["v1.2.2", "v1.2.3", "v1.10.0"])
    target = _target()
    context, _ = _context(tmp_path, [target], monkeypatch)

    report = await ReconcileHandler.reconcile_release_tags(
        source, REPO, CONFIG, context, ReconcileConfig(since="v1.2.3")
    )

    # v1.10.0 sorts after v1.2.3 by version; its section is absent, so it is refused.
    assert report.published == ["v1.2.3"]
    assert list(report.failed) == ["v1.10.0"]
    assert "v1.2.2" not in report.up_to_date + report.published + list(report.failed)
    assert [c.args[1] for c in target.release_exists.call_args_list].count("v1.2.2") == 0


@pytest.mark.asyncio
async def test_since_without_a_version_number_is_refused(tmp_path, monkeypatch):
    source = _source(["v1.2.3"])
    context, _ = _context(tmp_path, [_target()], monkeypatch)

    with pytest.raises(ReconcileSinceError, match="latest"):
        await ReconcileHandler.reconcile_release_tags(
            source, REPO, CONFIG, context, ReconcileConfig(since="latest")
        )


@pytest.mark.asyncio
async def test_since_date_keeps_only_tags_committed_on_or_after_it(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    source = _tagged_source({"v1.2.2": "b" * 40, "v1.2.3": "c" * 40})
    dates = {
        "b" * 40: datetime(2026, 1, 1, tzinfo=timezone.utc),
        "c" * 40: datetime(2026, 6, 1, tzinfo=timezone.utc),
    }

    async def commit_date(repo, sha):
        return dates[sha]

    context, _ = _context(tmp_path, [_target()], monkeypatch)
    report = await ReconcileHandler.reconcile_release_tags(
        source, REPO, CONFIG, context, ReconcileConfig(since="2026-03-01"), commit_date
    )

    assert report.published == ["v1.2.3"]
    assert report.failed == {}


@pytest.mark.asyncio
async def test_since_date_without_a_commit_date_source_is_refused(tmp_path, monkeypatch):
    source = _source(["v1.2.3"])
    context, _ = _context(tmp_path, [_target()], monkeypatch)

    with pytest.raises(ReconcileSinceError, match="2026-03-01"):
        await ReconcileHandler.reconcile_release_tags(
            source, REPO, CONFIG, context, ReconcileConfig(since="2026-03-01")
        )


def test_reconcile_since_resolves_from_the_layover_config():
    config = OpsEngineConfig.load(
        {"orgs": {"exampleorg": {"reconcile": {"since": "v1.0.0"}, "repositories": {"engine": {}}}}}
    )
    assert config.get_repo_config("exampleorg", "engine").reconcile.since == "v1.0.0"


@pytest.mark.asyncio
async def test_a_refused_tag_is_reported_once_with_its_reason_and_not_published(
    tmp_path, monkeypatch, caplog
):
    source = _source(["v1.2.1", "v1.2.3"])
    target = _target()
    context, _ = _context(tmp_path, [target], monkeypatch)

    with caplog.at_level("ERROR"):
        report = await ReconcileHandler.reconcile_release_tags(
            source, REPO, CONFIG, context, SINCE
        )

    assert list(report.failed) == ["v1.2.1"]
    assert "MissingChangelogEntryError" in report.failed["v1.2.1"]
    assert [c.kwargs["tag_name"] for c in target.create_release.call_args_list] == ["v1.2.3"]
    refused = [r for r in caplog.records if "v1.2.1" in r.getMessage()]
    assert len(refused) == 1
    assert "MissingChangelogEntryError" in refused[0].getMessage()


@pytest.mark.asyncio
async def test_a_tag_already_released_is_left_alone_over_two_cycles(tmp_path, monkeypatch):
    source = _source(["v1.2.3"])
    created: set[str] = set()
    target = _target()

    async def release_exists(repo, tag):
        return tag in created

    async def create_release(**kwargs):
        created.add(kwargs["tag_name"])
        return {"id": 1}

    target.release_exists = AsyncMock(side_effect=release_exists)
    target.create_release = AsyncMock(side_effect=create_release)
    context, identity = _context(tmp_path, [target], monkeypatch)

    first = await ReconcileHandler.reconcile_release_tags(source, REPO, CONFIG, context, SINCE)
    second = await ReconcileHandler.reconcile_release_tags(source, REPO, CONFIG, context, SINCE)

    assert first.published == ["v1.2.3"]
    assert second.published == [] and second.up_to_date == ["v1.2.3"]
    target.create_release.assert_called_once()
    identity.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_tag_pushed_while_stopped_gets_a_release_on_every_release_destination(
    tmp_path, monkeypatch
):
    """RED proof (criterion 3): one cycle publishes on both release destinations."""
    source = _source(["v1.2.3"], ops_yaml=OPS_YAML_TWO)
    forgejo, github = _target(), _target()
    context, _ = _context(tmp_path, [forgejo, github], monkeypatch)

    report = await ReconcileHandler.reconcile_release_tags(
        source, REPO, CONFIG, context, SINCE
    )

    assert report.published == ["v1.2.3"]
    forgejo.create_release.assert_called_once()
    github.create_release.assert_called_once()


@pytest.mark.asyncio
async def test_with_reconcile_disabled_the_same_tag_gets_no_release(tmp_path, monkeypatch):
    source = _source(["v1.2.3"], ops_yaml=OPS_YAML_TWO)
    forgejo, github = _target(), _target()
    context, identity = _context(tmp_path, [forgejo, github], monkeypatch)
    disabled = ReleaseConfig(enabled=True, tag_pattern="v*", reconcile=False)

    report = await ReconcileHandler.reconcile_release_tags(
        source, REPO, disabled, context, SINCE
    )

    assert report.published == [] and report.failed == {}
    source.list_tags.assert_not_called()
    forgejo.create_release.assert_not_called()
    github.create_release.assert_not_called()
    identity.assert_not_called()
