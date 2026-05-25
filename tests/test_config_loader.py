"""Tests for config_loader — v1 compatibility + v2 new models."""

from ops_engine.config_loader import (
    OpsEngineConfig,
    OrgConfig,
    RepoConfig,
    ReleaseConfig,
    MergeConfig,
    MirrorConfig,
    NotificationConfig,
    NotificationChannel,
)


def test_v1_backward_compat():
    """Existing v1 configs should still work without v2 fields."""
    config = OpsEngineConfig(
        orgs={
            "TestOrg": OrgConfig(
                repositories={
                    "repo-a": RepoConfig(
                        dependency_triggers=[],
                        workflow_dispatches=[],
                    )
                }
            )
        }
    )
    resolved = config.get_repo_config("TestOrg", "repo-a")
    assert resolved.stale_management is not None
    assert resolved.auto_triage is not None
    # v2 fields should be None
    assert resolved.release is None
    assert resolved.auto_merge is None
    assert resolved.mirror is None


def test_v2_release_config():
    config = ReleaseConfig(
        enabled=True,
        trigger="tag_push",
        tag_pattern="v*",
        changelog_path="CHANGELOG.md",
    )
    assert config.enabled is True
    assert config.tag_pattern == "v*"


def test_v2_merge_config():
    config = MergeConfig(
        enabled=True,
        trigger_label="auto-merge",
        required_checks=["test", "lint"],
        merge_method="squash",
    )
    assert config.merge_method == "squash"
    assert len(config.required_checks) == 2


def test_v2_mirror_config():
    config = MirrorConfig(
        enabled=True,
        primary_forge="forgejo",
        mirror_url="github.com/org/repo",
        max_drift_seconds=300,
    )
    assert config.mirror_url == "github.com/org/repo"


def test_v2_notification_config():
    config = NotificationConfig(
        enabled=True,
        channels=[
            NotificationChannel(type="slack", url="https://hooks.slack.com/xxx", events=["release"]),
        ],
    )
    assert len(config.channels) == 1
    assert config.channels[0].type == "slack"


def test_org_level_release_default():
    """Org-level release config should be inherited by repos without override."""
    config = OpsEngineConfig(
        orgs={
            "TestOrg": OrgConfig(
                release=ReleaseConfig(enabled=True, trigger="tag_push"),
                repositories={
                    "repo-a": RepoConfig(),
                    "repo-b": RepoConfig(
                        release=ReleaseConfig(enabled=False),
                    ),
                },
            )
        }
    )

    # repo-a inherits org default
    resolved_a = config.get_repo_config("TestOrg", "repo-a")
    assert resolved_a.release is not None
    assert resolved_a.release.enabled is True

    # repo-b overrides
    resolved_b = config.get_repo_config("TestOrg", "repo-b")
    assert resolved_b.release is not None
    assert resolved_b.release.enabled is False


def test_mirror_is_repo_specific_only():
    """Mirror config should NOT inherit from org level."""
    config = OpsEngineConfig(
        orgs={
            "TestOrg": OrgConfig(
                repositories={
                    "repo-a": RepoConfig(
                        mirror=MirrorConfig(enabled=True, mirror_url="github.com/org/a"),
                    ),
                    "repo-b": RepoConfig(),
                },
            )
        }
    )
    assert config.get_repo_config("TestOrg", "repo-a").mirror is not None
    assert config.get_repo_config("TestOrg", "repo-b").mirror is None


def test_full_41_repo_config_validates():
    """A config with many repos should validate without errors."""
    repos = {}
    for i in range(41):
        repos[f"repo-{i}"] = RepoConfig(
            release=ReleaseConfig(enabled=True),
            auto_merge=MergeConfig(enabled=True),
        )

    config = OpsEngineConfig(
        orgs={"BigOrg": OrgConfig(repositories=repos)}
    )
    assert len(config.orgs["BigOrg"].repositories) == 41
    resolved = config.get_repo_config("BigOrg", "repo-0")
    assert resolved.release.enabled is True
