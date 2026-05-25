"""Tests for CORE-002: ReleaseHandler."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from ops_engine.modules.release import ReleaseHandler
from ops_engine.config_loader import ReleaseConfig


@pytest.fixture
def mock_adapter():
    adapter = AsyncMock()
    adapter.release_exists = AsyncMock(return_value=False)
    adapter.get_file_content = AsyncMock(return_value="## v1.0.0\n\n### Features\n- New stuff")
    adapter.create_release = AsyncMock(return_value={"id": 1})
    return adapter


@pytest.fixture
def release_config():
    return ReleaseConfig(enabled=True, trigger="tag_push", tag_pattern="v*")


@pytest.mark.asyncio
async def test_release_on_tag_push(mock_adapter, release_config, sample_push_event):
    await ReleaseHandler.process_event(mock_adapter, sample_push_event, release_config)
    mock_adapter.create_release.assert_called_once()
    call_kwargs = mock_adapter.create_release.call_args
    assert call_kwargs.kwargs["tag_name"] == "v1.0.0"
    assert call_kwargs.kwargs["repo_full_name"] == "TestOrg/test-repo"


@pytest.mark.asyncio
async def test_release_skipped_when_disabled(mock_adapter, sample_push_event):
    config = ReleaseConfig(enabled=False)
    await ReleaseHandler.process_event(mock_adapter, sample_push_event, config)
    mock_adapter.create_release.assert_not_called()


@pytest.mark.asyncio
async def test_release_idempotent_when_exists(mock_adapter, release_config, sample_push_event):
    mock_adapter.release_exists.return_value = True
    await ReleaseHandler.process_event(mock_adapter, sample_push_event, release_config)
    mock_adapter.create_release.assert_not_called()


@pytest.mark.asyncio
async def test_release_with_changelog_notes(mock_adapter, release_config, sample_push_event):
    mock_adapter.get_file_content.return_value = "## v1.0.0\n\n### Added\n- Great feature"
    await ReleaseHandler.process_event(mock_adapter, sample_push_event, release_config)
    call_kwargs = mock_adapter.create_release.call_args.kwargs
    assert "Great feature" in call_kwargs["body"]


@pytest.mark.asyncio
async def test_release_without_changelog(mock_adapter, release_config, sample_push_event):
    mock_adapter.get_file_content.side_effect = Exception("404 not found")
    await ReleaseHandler.process_event(mock_adapter, sample_push_event, release_config)
    call_kwargs = mock_adapter.create_release.call_args.kwargs
    assert "Release v1.0.0" in call_kwargs["body"]


@pytest.mark.asyncio
async def test_tag_pattern_filtering(mock_adapter, sample_push_event):
    config = ReleaseConfig(enabled=True, trigger="tag_push", tag_pattern="release-*")
    await ReleaseHandler.process_event(mock_adapter, sample_push_event, config)
    # v1.0.0 does not match release-* pattern
    mock_adapter.create_release.assert_not_called()


@pytest.mark.asyncio
async def test_non_tag_push_ignored(mock_adapter, release_config):
    event = {
        "event_type": "push",
        "repo": "TestOrg/test-repo",
        "raw": {"ref": "refs/heads/main"},
    }
    await ReleaseHandler.process_event(mock_adapter, event, release_config)
    mock_adapter.create_release.assert_not_called()
