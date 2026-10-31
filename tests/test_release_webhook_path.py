"""OE-009 criterion 1: a tag-push webhook publishes through the gates.

``_create_release_for_tag`` is gone. A tag push runs, in this order: the OE-007
gates on the CHANGELOG and the tag, the OE-008 tag identity, then
``publish_release`` over the release-role destinations of the ``.ops.yaml`` read
at the tag. Every refusal happens before any release object exists.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ops_engine.config_loader import OpsEngineConfig, ReleaseConfig
from ops_engine.modules import release as release_module
from ops_engine.modules.release import (
    MissingOpsYamlError,
    MissingReleaseContextError,
    MissingReleaseDestinationsError,
    ReleaseContext,
    ReleaseHandler,
    TagIdentitySpec,
)
from ops_engine.modules.release_gates import (
    MissingChangelogEntryError,
    ReleaseNotesAudienceError,
    ReleaseTitleError,
)

REPO = "exampleorg/engine"
TAG = "v1.2.3"
CHANGELOG = "# Changes\n\n## v1.2.3\n\n- Faster start.\n\n## v1.2.2\n\n- Older.\n"
OPS_YAML = """\
destinations:
  - {forge: forgejo, repo: exampleorg/engine, role: release}
  - {forge: github, repo: exampleorg/mirror-only, role: mirror}
"""


def _event(tag: str = TAG) -> dict:
    return {
        "event_type": "push",
        "action": None,
        "repo": REPO,
        "raw": {"ref": f"refs/tags/{tag}"},
    }


def _ops_config() -> OpsEngineConfig:
    return OpsEngineConfig.load({"orgs": {"exampleorg": {"repositories": {"engine": {}}}}})


class _Recorder:
    """One shared call log so the order across adapters and identity is visible."""

    def __init__(self):
        self.calls: list[str] = []


def _setup(tmp_path, monkeypatch, *, changelog=CHANGELOG, ops_yaml=OPS_YAML, **ctx):
    rec = _Recorder()
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "pkg.whl").write_bytes(b"wheel")

    source = AsyncMock()
    reads: list[tuple[str, str]] = []

    async def get_file_content(repo, path, ref="main"):
        reads.append((path, ref))
        rec.calls.append(f"read:{path}")
        content = {"CHANGELOG.md": changelog, ".ops.yaml": ops_yaml}[path]
        if content is None:
            raise FileNotFoundError(path)
        return content

    source.get_file_content = get_file_content
    source.create_release = AsyncMock(side_effect=AssertionError("event forge must not be used"))

    target = AsyncMock()
    target.release_exists = AsyncMock(return_value=False)

    async def create_release(**kwargs):
        rec.calls.append("publish")
        return {"id": 7}

    target.create_release = AsyncMock(side_effect=create_release)

    async def identity(*args, **kwargs):
        rec.calls.append("identity")

    identity_mock = AsyncMock(side_effect=identity)
    monkeypatch.setattr(release_module, "push_canonical_tag", identity_mock)

    context = ReleaseContext(
        ops_config=_ops_config(),
        artifact_dir=tmp_path / "dist",
        identity=TagIdentitySpec(
            repo_dir=str(tmp_path),
            canonical_url="https://forge.example/exampleorg/engine.git",
            canonical_token="tok",
            mirror_remote="https://mirror.example/exampleorg/engine.git",
        ),
        adapters=[target],
        **ctx,
    )
    return rec, source, target, identity_mock, context, reads


CONFIG = ReleaseConfig(enabled=True, trigger="tag_push", tag_pattern="v*")


def test_create_release_for_tag_is_gone():
    assert not hasattr(release_module, "_create_release_for_tag")


@pytest.mark.asyncio
async def test_tag_push_runs_gates_then_identity_then_publish(tmp_path, monkeypatch):
    rec, source, target, identity, context, reads = _setup(tmp_path, monkeypatch)
    await ReleaseHandler.process_event(source, _event(), CONFIG, context)

    # Order: every read (the gates' and the destinations'), then identity, then publish.
    assert rec.calls == ["read:CHANGELOG.md", "read:.ops.yaml", "identity", "publish"]
    # Everything is read AT the tag.
    assert reads == [("CHANGELOG.md", TAG), (".ops.yaml", TAG)]
    identity.assert_awaited_once()
    assert identity.await_args.args[1] == TAG
    # Notes are the CHANGELOG section, not a fallback; only the release-role
    # destination is published to (a single adapter was injected for it).
    kwargs = target.create_release.await_args.kwargs
    assert kwargs["body"] == "- Faster start."
    assert kwargs["repo_full_name"] == REPO
    assert kwargs["tag_name"] == TAG
    source.create_release.assert_not_called()
    target.upload_release_asset.assert_awaited_once()


@pytest.mark.parametrize(
    "overrides, tag, error",
    [
        ({}, "v1.2", ReleaseTitleError),
        ({"changelog": "## v9.9.9\n\n- Other.\n"}, TAG, MissingChangelogEntryError),
        ({"changelog": None}, TAG, MissingChangelogEntryError),
        (
            {"changelog": "## v1.2.3\n\n- Fix ABC-12 internally.\n", "ticket_prefixes": ["ABC"]},
            TAG,
            ReleaseNotesAudienceError,
        ),
        ({"ops_yaml": None}, TAG, MissingOpsYamlError),
        (
            {"ops_yaml": "destinations:\n  - {forge: github, repo: exampleorg/m, role: mirror}\n"},
            TAG,
            MissingReleaseDestinationsError,
        ),
    ],
    ids=["title", "no-section", "no-changelog", "audience", "no-ops-yaml", "no-release-role"],
)
@pytest.mark.asyncio
async def test_refusal_happens_before_identity_and_any_release_object(
    tmp_path, monkeypatch, overrides, tag, error
):
    rec, source, target, identity, context, _ = _setup(tmp_path, monkeypatch, **overrides)
    with pytest.raises(error):
        await ReleaseHandler.process_event(source, _event(tag), CONFIG, context)
    identity.assert_not_called()
    target.create_release.assert_not_called()
    source.create_release.assert_not_called()
    assert "identity" not in rec.calls and "publish" not in rec.calls


@pytest.mark.asyncio
async def test_identity_refusal_stops_before_publish(tmp_path, monkeypatch):
    rec, source, target, identity, context, _ = _setup(tmp_path, monkeypatch)
    identity.side_effect = RuntimeError("identity refused")
    with pytest.raises(RuntimeError, match="identity refused"):
        await ReleaseHandler.process_event(source, _event(), CONFIG, context)
    target.create_release.assert_not_called()


@pytest.mark.asyncio
async def test_tag_push_without_context_creates_nothing():
    source = MagicMock()
    source.create_release = AsyncMock()
    with pytest.raises(MissingReleaseContextError):
        await ReleaseHandler.process_event(source, _event(), CONFIG)
    source.create_release.assert_not_called()


@pytest.mark.asyncio
async def test_redelivery_is_a_noop_for_a_destination_that_already_has_the_release(
    tmp_path, monkeypatch
):
    """A redelivered tag push must not fail or publish twice on a destination
    whose release exists: no create, no upload, recorded as skipped."""
    rec, source, target, identity, context, _ = _setup(tmp_path, monkeypatch)
    target.release_exists = AsyncMock(return_value=True)
    reports = []
    real = ReleaseHandler.publish_release

    async def spy(*args, **kwargs):
        report = await real(*args, **kwargs)
        reports.append(report)
        return report

    monkeypatch.setattr(ReleaseHandler, "publish_release", staticmethod(spy))
    await ReleaseHandler.process_event(source, _event(), CONFIG, context)

    target.release_exists.assert_awaited_once_with(REPO, TAG)
    target.create_release.assert_not_called()
    target.upload_release_asset.assert_not_called()
    assert reports[0].failed == []
    assert reports[0].skipped == ["forgejo:" + REPO]


@pytest.mark.asyncio
async def test_missing_release_is_still_published(tmp_path, monkeypatch):
    rec, source, target, identity, context, _ = _setup(tmp_path, monkeypatch)
    target.release_exists = AsyncMock(return_value=False)
    await ReleaseHandler.process_event(source, _event(), CONFIG, context)
    target.create_release.assert_awaited_once()


# --- OE-009 criterion 4: release.artifact_dir (D18) ---------------------------


def _config(**kw) -> ReleaseConfig:
    return ReleaseConfig(enabled=True, trigger="tag_push", tag_pattern="v*", **kw)


@pytest.mark.asyncio
async def test_artifact_dir_relative_directory_is_attached(tmp_path, monkeypatch):
    rec, source, target, identity, context, _ = _setup(tmp_path, monkeypatch)
    (tmp_path / "dist" / "sub").mkdir()
    (tmp_path / "dist" / "sub" / "only.txt").write_bytes(b"x")
    await ReleaseHandler.process_event(source, _event(), _config(artifact_dir="sub"), context)
    assert target.upload_release_asset.await_count == 1
    assert "only.txt" in str(target.upload_release_asset.await_args)
    assert "pkg.whl" not in str(target.upload_release_asset.await_args)


@pytest.mark.parametrize("configured", ["missing-dir", "empty"])
@pytest.mark.asyncio
async def test_artifact_dir_absent_or_empty_attaches_nothing(tmp_path, monkeypatch, configured):
    rec, source, target, identity, context, _ = _setup(tmp_path, monkeypatch)
    (tmp_path / "dist" / "empty").mkdir()
    await ReleaseHandler.process_event(source, _event(), _config(artifact_dir=configured), context)
    target.create_release.assert_awaited_once()
    target.upload_release_asset.assert_not_called()


@pytest.mark.asyncio
async def test_artifact_dir_unset_keeps_the_root(tmp_path, monkeypatch):
    rec, source, target, identity, context, _ = _setup(tmp_path, monkeypatch)
    await ReleaseHandler.process_event(source, _event(), _config(), context)
    target.upload_release_asset.assert_awaited_once()


@pytest.mark.parametrize("which", ["dotdot", "absolute", "symlink"])
@pytest.mark.asyncio
async def test_artifact_dir_outside_root_is_refused_and_attaches_nothing(
    tmp_path, monkeypatch, which
):
    rec, source, target, identity, context, _ = _setup(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"s")
    configured = {
        "dotdot": "../outside",
        "absolute": str(outside),
        "symlink": "link",
    }[which]
    if which == "symlink":
        (tmp_path / "dist" / "link").symlink_to(outside)
    with pytest.raises(release_module.ArtifactDirError):
        await ReleaseHandler.process_event(
            source, _event(), _config(artifact_dir=configured), context
        )
    assert rec.calls == []
    identity.assert_not_awaited()
    target.create_release.assert_not_called()
    target.upload_release_asset.assert_not_called()


@pytest.mark.asyncio
async def test_repo_ops_yaml_cannot_set_artifact_dir(tmp_path, monkeypatch):
    ops_yaml = OPS_YAML + "release:\n  artifact_dir: ../outside\n"
    rec, source, target, identity, context, _ = _setup(
        tmp_path, monkeypatch, ops_yaml=ops_yaml
    )
    await ReleaseHandler.process_event(source, _event(), _config(), context)
    # The value at the tag is ignored: the root's files are attached as usual.
    target.upload_release_asset.assert_awaited_once()


# --- OE-009 criterion 2: two release destinations, partial publication (D20) --

TWO_RELEASE_OPS_YAML = """\
destinations:
  - {forge: forgejo, repo: exampleorg/engine, role: release}
  - {forge: github, repo: exampleorg/engine-pub, role: release}
"""


def _notifying_config() -> OpsEngineConfig:
    return OpsEngineConfig.load(
        {
            "orgs": {
                "exampleorg": {
                    "repositories": {
                        "engine": {
                            "notifications": {
                                "enabled": True,
                                "channels": [
                                    {"type": "slack", "url": "https://hooks.example/x"}
                                ],
                            }
                        }
                    }
                }
            }
        }
    )


def _two_destination_setup(tmp_path, monkeypatch, second_fails=False, second_exists=False):
    import dataclasses

    from ops_engine.modules import notification

    rec, source, first, identity, context, _ = _setup(
        tmp_path, monkeypatch, ops_yaml=TWO_RELEASE_OPS_YAML
    )
    second = AsyncMock()
    second.release_exists = AsyncMock(return_value=second_exists)
    if second_fails:
        second.create_release = AsyncMock(side_effect=RuntimeError("second forge down"))
    else:
        second.create_release = AsyncMock(return_value={"id": 8})
    audit: list[str] = []
    context = dataclasses.replace(
        context,
        ops_config=_notifying_config(),
        adapters=[first, second],
        audit=audit,
    )
    sent: list[str] = []

    async def fake_send(channel, message):
        sent.append(message)

    monkeypatch.setattr(notification, "_send_notification", fake_send)
    notification.NotificationHandler.reset()
    return source, first, second, context, audit, sent


@pytest.mark.asyncio
async def test_two_release_destinations_are_both_published(tmp_path, monkeypatch, caplog):
    source, first, second, context, audit, sent = _two_destination_setup(tmp_path, monkeypatch)
    with caplog.at_level("WARNING"):
        await ReleaseHandler.process_event(source, _event(), CONFIG, context)

    first.create_release.assert_awaited_once()
    second.create_release.assert_awaited_once()
    second.upload_release_asset.assert_awaited_once()
    assert audit == [
        f"release-publish tag={TAG} dest=forgejo:{REPO} result=published",
        f"release-publish tag={TAG} dest=github:exampleorg/engine-pub result=published",
    ]
    assert sent == []
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


@pytest.mark.asyncio
async def test_partial_publication_is_audited_notified_and_warned_once(
    tmp_path, monkeypatch, caplog
):
    source, first, second, context, audit, sent = _two_destination_setup(
        tmp_path, monkeypatch, second_fails=True
    )
    with caplog.at_level("WARNING"):
        await ReleaseHandler.process_event(source, _event(), CONFIG, context)

    # The first destination was published; the failure did not stop or undo it.
    first.create_release.assert_awaited_once()
    assert audit == [
        f"release-publish tag={TAG} dest=forgejo:{REPO} result=published",
        f"release-publish tag={TAG} dest=github:exampleorg/engine-pub result=failed",
    ]
    # One failure notification through the notification channel.
    assert len(sent) == 1
    assert "FAILED" in sent[0] and TAG in sent[0] and "github:exampleorg/engine-pub" in sent[0]
    # Exactly one warning line in the layover's log.
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "release-partial" in warnings[0] and "github:exampleorg/engine-pub" in warnings[0]


@pytest.mark.asyncio
async def test_skipped_existing_destination_is_not_a_failure(tmp_path, monkeypatch, caplog):
    source, first, second, context, audit, sent = _two_destination_setup(
        tmp_path, monkeypatch, second_exists=True
    )
    with caplog.at_level("WARNING"):
        await ReleaseHandler.process_event(source, _event(), CONFIG, context)

    second.create_release.assert_not_called()
    assert audit[-1] == f"release-publish tag={TAG} dest=github:exampleorg/engine-pub result=skipped"
    assert sent == []
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
