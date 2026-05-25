"""Tests for ForgeAdapter create_pull_request / list_pull_requests."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import httpx


# --- GitHub Adapter ---


@pytest.fixture
def github_adapter():
    from ops_engine.adapters.github_adapter import GithubAdapter
    adapter = GithubAdapter(token="test-token", webhook_secret="secret")
    return adapter


@pytest.mark.asyncio
async def test_github_create_pr(github_adapter):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "number": 42,
        "html_url": "https://github.com/org/repo/pull/42",
    }
    mock_resp.raise_for_status = MagicMock()

    with patch.object(github_adapter, "_request", new_callable=AsyncMock, return_value=mock_resp):
        result = await github_adapter.create_pull_request(
            "org/repo", "feat: new feature", "PR body", head="feat/branch", base="main",
        )

    assert result["number"] == 42
    assert "html_url" in result


@pytest.mark.asyncio
async def test_github_create_pr_with_labels(github_adapter):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"number": 1, "html_url": "https://github.com/org/repo/pull/1"}
    mock_resp.raise_for_status = MagicMock()

    with patch.object(github_adapter, "_request", new_callable=AsyncMock, return_value=mock_resp) as mock_req:
        await github_adapter.create_pull_request(
            "org/repo", "title", "body", head="feat/x", base="main", labels=["auto-merge"],
        )
        call_kwargs = mock_req.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert "labels" in payload
        assert payload["labels"] == ["auto-merge"]


@pytest.mark.asyncio
async def test_github_list_prs(github_adapter):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"number": 1, "head": {"ref": "feat/a"}, "base": {"ref": "main"}},
        {"number": 2, "head": {"ref": "feat/b"}, "base": {"ref": "main"}},
    ]
    mock_resp.raise_for_status = MagicMock()

    with patch.object(github_adapter, "_request", new_callable=AsyncMock, return_value=mock_resp):
        pulls = await github_adapter.list_pull_requests("org/repo", state="open")

    assert len(pulls) == 2


@pytest.mark.asyncio
async def test_github_list_prs_with_head_filter(github_adapter):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"number": 1, "head": {"ref": "feat/a"}, "base": {"ref": "main"}},
    ]
    mock_resp.raise_for_status = MagicMock()

    with patch.object(github_adapter, "_request", new_callable=AsyncMock, return_value=mock_resp) as mock_req:
        await github_adapter.list_pull_requests("org/repo", head="feat/a")
        call_kwargs = mock_req.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params")
        # GitHub expects owner:branch format
        assert params["head"] == "org:feat/a"


# --- Forgejo Adapter ---


@pytest.fixture
def forgejo_adapter():
    from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
    adapter = ForgejoAdapter(
        base_url="https://git.example.com", token="test-token", webhook_secret="secret",
    )
    return adapter


@pytest.mark.asyncio
async def test_forgejo_create_pr(forgejo_adapter):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "number": 7,
        "html_url": "https://git.example.com/org/repo/pulls/7",
    }
    mock_resp.raise_for_status = MagicMock()

    with patch.object(forgejo_adapter, "_request", new_callable=AsyncMock, return_value=mock_resp):
        result = await forgejo_adapter.create_pull_request(
            "org/repo", "feat: forgejo pr", "body", head="feat/branch", base="main",
        )

    assert result["number"] == 7


@pytest.mark.asyncio
async def test_forgejo_create_pr_with_labels(forgejo_adapter):
    # First call: GET labels, Second call: POST PR
    labels_resp = MagicMock()
    labels_resp.json.return_value = [
        {"id": 10, "name": "auto-merge"},
        {"id": 20, "name": "bug"},
    ]
    labels_resp.raise_for_status = MagicMock()

    pr_resp = MagicMock()
    pr_resp.json.return_value = {"number": 1, "html_url": "https://git.example.com/org/repo/pulls/1"}
    pr_resp.raise_for_status = MagicMock()

    with patch.object(
        forgejo_adapter, "_request", new_callable=AsyncMock,
        side_effect=[labels_resp, pr_resp],
    ) as mock_req:
        await forgejo_adapter.create_pull_request(
            "org/repo", "title", "body", head="feat/x", base="main", labels=["auto-merge"],
        )
        # Second call should have label IDs, not names
        pr_call = mock_req.call_args_list[1]
        payload = pr_call.kwargs.get("json") or pr_call[1].get("json")
        assert payload["labels"] == [10]


@pytest.mark.asyncio
async def test_forgejo_list_prs_filters_client_side(forgejo_adapter):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"number": 1, "head": {"ref": "feat/a"}, "base": {"ref": "main"}},
        {"number": 2, "head": {"ref": "feat/b"}, "base": {"ref": "main"}},
        {"number": 3, "head": {"ref": "feat/a"}, "base": {"ref": "develop"}},
    ]
    mock_resp.raise_for_status = MagicMock()

    with patch.object(forgejo_adapter, "_request", new_callable=AsyncMock, return_value=mock_resp):
        # Filter by head only
        pulls = await forgejo_adapter.list_pull_requests("org/repo", head="feat/a")
        assert len(pulls) == 2
        assert all(p["head"]["ref"] == "feat/a" for p in pulls)

        # Filter by head + base
        pulls = await forgejo_adapter.list_pull_requests("org/repo", head="feat/a", base="main")
        assert len(pulls) == 1
        assert pulls[0]["number"] == 1
