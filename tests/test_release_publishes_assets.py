"""ADP-003: the release module publishes a built release to every destination.

The release module turns a tag, release notes and a directory of built artifacts
into a release on every destination resolved from config (REL-010: one build,
published outward, nothing re-downloaded). The three acceptance criteria:

1. The module creates the release and attaches every artifact on each destination
   resolved from config, for a two-destination config against stubbed adapters,
   asserting the calls and their order.
2. A failure on the second destination leaves the first published and is reported
   as a partial publication naming the failed destination; the test drives that
   path and asserts the report, not just the exception.
3. Nothing is downloaded: the artifacts come from the given directory, proven by
   running the module under a socket guard with only the stubbed adapters
   permitted to speak.
"""

import socket
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ops_engine.config_loader import OpsEngineConfig
from ops_engine.modules.release import (
    PublicationReport,
    ReleaseHandler,
    _destination_label,
)

ASSET_A = b"\x00wheel-bytes\xff"
ASSET_B = b"SHA256SUMS-bytes\n"


def _two_destination_config() -> OpsEngineConfig:
    """A config fixture carrying one repo with a GitHub and a Forgejo destination."""
    return OpsEngineConfig.load(
        {
            "orgs": {
                "exampleorg": {
                    "repositories": {
                        "engine": {
                            "destinations": [
                                {
                                    "forge": "github",
                                    "repo": "exampleorg/engine",
                                    "role": "release",
                                },
                                {
                                    "forge": "forgejo",
                                    "repo": "exampleorg/engine",
                                    "role": "release",
                                },
                            ]
                        }
                    }
                }
            }
        }
    )


def _artifact_dir(tmp_path: Path) -> Path:
    """A directory with two built artifacts, in known name order."""
    d = tmp_path / "dist"
    d.mkdir()
    (d / "aaa.whl").write_bytes(ASSET_A)
    (d / "bbb.tar.gz").write_bytes(ASSET_B)
    return d


def _make_adapter(*, release_id: int):
    adapter = AsyncMock()
    adapter.create_release = AsyncMock(return_value={"id": release_id})
    adapter.upload_release_asset = AsyncMock(return_value={"ok": True})
    return adapter


_REPO = "exampleorg/engine"


# --- Criterion 1: every artifact on every destination, calls in order ----------


@pytest.mark.asyncio
async def test_publishes_to_every_destination_in_order(tmp_path):
    config = _two_destination_config()
    first, second = _make_adapter(release_id=1), _make_adapter(release_id=2)

    report = await ReleaseHandler.publish_release(
        config,
        _REPO,
        "v1.0.0",
        "release notes",
        _artifact_dir(tmp_path),
        adapters=[first, second],
    )

    # Both destinations created a release, once each, in destination order.
    assert [c.kwargs["repo_full_name"] for c in first.create_release.call_args_list] == [_REPO]
    assert [c.kwargs["repo_full_name"] for c in second.create_release.call_args_list] == [_REPO]
    assert first.create_release.await_count == 1
    assert second.create_release.await_count == 1

    # The release id for each upload comes from that destination's release.
    first_uploads = first.upload_release_asset.call_args_list
    second_uploads = second.upload_release_asset.call_args_list
    assert all(c.kwargs["release_id"] == 1 for c in first_uploads)
    assert all(c.kwargs["release_id"] == 2 for c in second_uploads)

    # Every artifact is attached on every destination, in name order.
    assert [c.kwargs["asset_name"] for c in first_uploads] == ["aaa.whl", "bbb.tar.gz"]
    assert [c.kwargs["asset_name"] for c in second_uploads] == ["aaa.whl", "bbb.tar.gz"]
    assert [c.kwargs["data"] for c in first_uploads] == [ASSET_A, ASSET_B]
    assert [c.kwargs["data"] for c in second_uploads] == [ASSET_A, ASSET_B]

    # Every upload targets that destination's repo.
    for uploads in (first_uploads, second_uploads):
        assert all(c.kwargs["repo_full_name"] == _REPO for c in uploads)

    # The report records a full publication.
    assert report == PublicationReport(
        repo=_REPO,
        tag_name="v1.0.0",
        published=["github:exampleorg/engine", "forgejo:exampleorg/engine"],
        failed=[],
    )
    assert report.partial is False


# --- Criterion 2: failure on the second destination is a reported partial -------


@pytest.mark.asyncio
async def test_failure_on_second_destination_is_a_reported_partial(tmp_path):
    config = _two_destination_config()
    first = _make_adapter(release_id=1)
    second = _make_adapter(release_id=2)
    second.create_release.side_effect = RuntimeError("upstream refused")

    report = await ReleaseHandler.publish_release(
        config,
        _REPO,
        "v1.0.0",
        "release notes",
        _artifact_dir(tmp_path),
        adapters=[first, second],
    )

    # The first destination published fully.
    assert first.create_release.await_count == 1
    assert len(first.upload_release_asset.call_args_list) == 2

    # The second destination failed, and its failure is reported, not raised.
    assert second.create_release.await_count == 1
    assert second.upload_release_asset.await_count == 0

    assert report.partial is True
    assert report.published == ["github:exampleorg/engine"]
    assert report.failed == ["forgejo:exampleorg/engine"]

    # The report serializes the partial state for a cockpit/operator to read.
    assert report.to_dict() == {
        "repo": _REPO,
        "tag_name": "v1.0.0",
        "published_destinations": ["github:exampleorg/engine"],
        "failed_destinations": ["forgejo:exampleorg/engine"],
        "partial": True,
    }


def test_destination_label_carries_forge():
    config = _two_destination_config()
    destinations = _destinations_of(config)
    assert _destination_label(destinations[0]) == "github:exampleorg/engine"
    assert _destination_label(destinations[1]) == "forgejo:exampleorg/engine"


# --- Criterion 3: nothing is downloaded (socket guard) --------------------------


def _destinations_of(config: OpsEngineConfig):
    from ops_engine.modules.mirror import resolve_destinations

    return resolve_destinations(config, _REPO)


@pytest.mark.asyncio
async def test_nothing_is_downloaded(tmp_path, monkeypatch):
    """The artifacts come from the given directory, not the network.

    Every socket construction is blocked, then the module is run with stubbed
    adapters (the only thing permitted to speak). The constructors construct no
    socket, and the module's only inputs are the artifact directory on disk and
    the stubbed adapters, so a successful run under the guard proves no network
    call is made by the module.
    """

    def _blocked(*args, **kwargs):
        raise AssertionError("a socket was opened during publication")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)

    config = _two_destination_config()
    first, second = _make_adapter(release_id=1), _make_adapter(release_id=2)

    report = await ReleaseHandler.publish_release(
        config,
        _REPO,
        "v1.0.0",
        "release notes",
        _artifact_dir(tmp_path),
        adapters=[first, second],
    )

    assert report.partial is False
    assert len(first.upload_release_asset.call_args_list) == 2
    assert len(second.upload_release_asset.call_args_list) == 2
