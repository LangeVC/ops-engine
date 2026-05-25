"""Shared test fixtures for ops-engine."""

import pytest


@pytest.fixture
def sample_push_event():
    """A GitHub push event with a tag."""
    return {
        "source": "github",
        "event_type": "push",
        "action": None,
        "repo": "TestOrg/test-repo",
        "sender": "testuser",
        "delivery_id": "test-delivery-001",
        "raw": {
            "ref": "refs/tags/v1.0.0",
            "after": "abc123def456",
            "repository": {"full_name": "TestOrg/test-repo"},
            "sender": {"login": "testuser"},
            "head_commit": {"id": "abc123def456"},
        },
    }


@pytest.fixture
def sample_pr_event():
    """A GitHub pull_request event (opened)."""
    return {
        "source": "github",
        "event_type": "pull_request",
        "action": "opened",
        "repo": "TestOrg/test-repo",
        "sender": "testuser",
        "delivery_id": "test-delivery-002",
        "raw": {
            "action": "opened",
            "pull_request": {
                "number": 42,
                "title": "feat: add new feature",
                "state": "open",
                "merged": False,
                "head": {"sha": "abc123", "ref": "feat/new-feature"},
                "labels": [{"name": "auto-merge"}],
            },
            "repository": {"full_name": "TestOrg/test-repo"},
            "sender": {"login": "testuser"},
        },
    }


@pytest.fixture
def sample_check_suite_event():
    """A GitHub check_suite completed event."""
    return {
        "source": "github",
        "event_type": "check_suite",
        "action": "completed",
        "repo": "TestOrg/test-repo",
        "sender": "github-actions",
        "delivery_id": "test-delivery-003",
        "raw": {
            "action": "completed",
            "check_suite": {
                "conclusion": "success",
                "head_sha": "abc123",
                "pull_requests": [{"number": 42}],
            },
            "repository": {"full_name": "TestOrg/test-repo"},
        },
    }


@pytest.fixture
def sample_changelog():
    """Sample CHANGELOG.md content."""
    return """# Changelog

## test-repo v1.0.0 — Initial Release (2026-05-25)

### New Features
- Feature A: Something great
- Feature B: Something else

### Bug Fixes
- Fixed a crash on startup

## v0.9.0 — Beta (2026-05-01)

### New Features
- Beta feature
"""
