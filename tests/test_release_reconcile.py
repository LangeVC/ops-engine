"""FFR-200-5: ReconcileHandler detects tags that exist but have no release.

The detection is read-only: it never creates, deletes, or otherwise mutates a
tag or a release.
"""

from unittest.mock import AsyncMock

import pytest

from ops_engine.modules.reconcile import ReconcileHandler


@pytest.fixture
def adapter():
    adapter = AsyncMock()
    adapter.release_exists = AsyncMock(return_value=False)
    return adapter


REPO = "langevc/ops-engine"


@pytest.mark.asyncio
async def test_detects_tag_without_release(adapter):
    adapter.release_exists.return_value = False
    missing = await ReconcileHandler.detect_missing_releases(adapter, REPO, ["v1.0.0"])
    assert missing == ["v1.0.0"]


@pytest.mark.asyncio
async def test_skips_tag_with_existing_release(adapter):
    adapter.release_exists.return_value = True
    missing = await ReconcileHandler.detect_missing_releases(adapter, REPO, ["v1.0.0"])
    assert missing == []


@pytest.mark.asyncio
async def test_partitions_tags_by_release_presence(adapter):
    async def release_exists(repo, tag_name):
        return tag_name == "v1.1.0"

    adapter.release_exists.side_effect = release_exists
    missing = await ReconcileHandler.detect_missing_releases(
        adapter, REPO, ["v1.0.0", "v1.1.0", "v1.2.0"]
    )
    assert missing == ["v1.0.0", "v1.2.0"]


@pytest.mark.asyncio
async def test_detection_is_read_only(adapter):
    adapter.release_exists.return_value = False
    await ReconcileHandler.detect_missing_releases(adapter, REPO, ["v1.0.0"])

    # The only adapter call the detection may make is the read-only existence
    # check; no mutating call may occur.
    assert [c[0] for c in adapter.method_calls] == ["release_exists"]
    adapter.create_release.assert_not_called()
    adapter.create_tag.assert_not_called()
    adapter.delete_release.assert_not_called()


@pytest.mark.asyncio
async def test_empty_tag_list_returns_empty(adapter):
    assert await ReconcileHandler.detect_missing_releases(adapter, REPO, []) == []


@pytest.mark.asyncio
async def test_missing_repo_returns_empty(adapter):
    adapter.release_exists.return_value = False
    assert await ReconcileHandler.detect_missing_releases(adapter, "", ["v1.0.0"]) == []
    adapter.release_exists.assert_not_called()
