"""Tests for CORE-003: MergeHandler."""

import pytest
from unittest.mock import AsyncMock

from ops_engine.modules.merge import MergeHandler
from ops_engine.config_loader import MergeConfig


@pytest.fixture
def mock_adapter():
    adapter = AsyncMock()
    adapter.get_pull_request = AsyncMock(return_value={
        "number": 42,
        "state": "open",
        "merged": False,
        "head": {"sha": "abc123", "ref": "feat/branch"},
        "labels": [{"name": "auto-merge"}],
    })
    adapter.get_ci_status = AsyncMock(return_value={
        "state": "success",
        "statuses": [
            {"context": "test", "state": "success"},
            {"context": "lint", "state": "success"},
        ],
    })
    adapter.merge_pull_request = AsyncMock(return_value={"merged": True})
    return adapter


@pytest.fixture
def merge_config():
    return MergeConfig(
        enabled=True,
        trigger_label="auto-merge",
        merge_method="squash",
        required_checks=["test", "lint"],
    )


@pytest.mark.asyncio
async def test_merge_on_ci_success(mock_adapter, merge_config, sample_check_suite_event):
    await MergeHandler.process_event(mock_adapter, sample_check_suite_event, merge_config)
    mock_adapter.merge_pull_request.assert_called_once_with(
        "TestOrg/test-repo", 42, merge_method="squash", delete_branch=True
    )


@pytest.mark.asyncio
async def test_merge_skipped_when_disabled(mock_adapter, sample_check_suite_event):
    config = MergeConfig(enabled=False)
    await MergeHandler.process_event(mock_adapter, sample_check_suite_event, config)
    mock_adapter.merge_pull_request.assert_not_called()


@pytest.mark.asyncio
async def test_merge_skipped_without_label(mock_adapter, merge_config, sample_check_suite_event):
    mock_adapter.get_pull_request.return_value["labels"] = [{"name": "other-label"}]
    await MergeHandler.process_event(mock_adapter, sample_check_suite_event, merge_config)
    mock_adapter.merge_pull_request.assert_not_called()


@pytest.mark.asyncio
async def test_merge_skipped_on_ci_failure(mock_adapter, merge_config):
    event = {
        "event_type": "check_suite",
        "repo": "TestOrg/test-repo",
        "raw": {
            "action": "completed",
            "check_suite": {
                "conclusion": "failure",
                "head_sha": "abc123",
                "pull_requests": [{"number": 42}],
            },
        },
    }
    await MergeHandler.process_event(mock_adapter, event, merge_config)
    mock_adapter.merge_pull_request.assert_not_called()


@pytest.mark.asyncio
async def test_merge_skipped_when_already_merged(mock_adapter, merge_config, sample_check_suite_event):
    mock_adapter.get_pull_request.return_value["merged"] = True
    mock_adapter.get_pull_request.return_value["state"] = "closed"
    await MergeHandler.process_event(mock_adapter, sample_check_suite_event, merge_config)
    mock_adapter.merge_pull_request.assert_not_called()


@pytest.mark.asyncio
async def test_merge_on_label_event(mock_adapter, merge_config):
    event = {
        "event_type": "pull_request",
        "action": "labeled",
        "repo": "TestOrg/test-repo",
        "raw": {
            "action": "labeled",
            "label": {"name": "auto-merge"},
            "pull_request": {"number": 42},
        },
    }
    await MergeHandler.process_event(mock_adapter, event, merge_config)
    mock_adapter.merge_pull_request.assert_called_once()
