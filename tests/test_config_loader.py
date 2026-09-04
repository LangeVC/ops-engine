"""Tests for config_loader — v1 compatibility + v2 new models."""

import warnings

from ops_engine.config_loader import (
    OpsEngineConfig,
    OrgConfig,
    RepoConfig,
    ReleaseConfig,
    MergeConfig,
    MirrorConfig,
    Destination,
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


def test_mirror_destination_fields_are_received():
    """mirror.github, mirror.visibility and repo-level github_name must not be
    silently dropped (LVC-247 / CFG-001). Without the declarations on
    MirrorConfig and RepoConfig, pydantic discards all three and this test fails.
    """
    config = OpsEngineConfig.model_validate(
        {
            "orgs": {
                "langevc": {
                    "repositories": {
                        "ops-engine": {
                            "mirror": {
                                "enabled": True,
                                "github": "LangeVC/ops-engine",
                                "visibility": "public",
                                "verify_on_push": True,
                            }
                        },
                        "txt-humanizer": {
                            "github_name": "txtHumanizer",
                            "mirror": {
                                "enabled": True,
                                "github": "LangeVC/txtHumanizer",
                                "visibility": "public",
                            },
                        },
                    }
                }
            }
        }
    )

    resolved = config.get_repo_config("langevc", "ops-engine")
    assert resolved.mirror is not None
    assert resolved.mirror.github == "LangeVC/ops-engine"
    assert resolved.mirror.visibility == "public"

    renamed = config.get_repo_config("langevc", "txt-humanizer")
    assert renamed.github_name == "txtHumanizer"


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


# ── DST-001: the Destination model and the three mirror shapes ────────────────


def _repo_with_destinations(destinations, mirror=None):
    return OpsEngineConfig(
        orgs={
            "langevc": OrgConfig(
                repositories={
                    "ops-engine": RepoConfig(
                        destinations=destinations,
                        mirror=mirror,
                    )
                }
            )
        }
    )


def test_destination_model_fields():
    """A Destination carries forge, repo, role and visibility (DST-001 AC1)."""
    d = Destination(forge="github", repo="LangeVC/ops-engine", role="mirror", visibility="public")
    assert d.forge == "github"
    assert d.repo == "LangeVC/ops-engine"
    assert d.role == "mirror"
    assert d.visibility == "public"


def test_destination_defaults():
    """forge defaults to github, role to mirror, visibility to empty."""
    d = Destination(repo="LangeVC/ops-engine")
    assert d.forge == "github"
    assert d.role == "mirror"
    assert d.visibility == ""


def test_destinations_list_is_preserved():
    """A repo with an explicit destinations list resolves it verbatim, no warning."""
    resolved = _repo_with_destinations(
        [
            Destination(forge="gitlab", repo="langevc/ops-engine", role="mirror", visibility="private"),
            Destination(forge="forgejo", repo="langevc/ops-engine", role="release", visibility="public"),
        ]
    ).get_repo_config("langevc", "ops-engine")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        out = resolved.resolve_destinations()
    assert len(out) == 2
    assert out[0].forge == "gitlab"
    assert out[1].role == "release"


def test_mirror_github_visibility_shape_migrates_and_warns():
    """The lvc-ops shape (mirror.github + visibility) resolves into one
    github/mirror Destination and emits a DeprecationWarning naming 4.0.0."""
    config = _repo_with_destinations(
        [],
        mirror=MirrorConfig(enabled=True, github="LangeVC/ops-engine", visibility="public"),
    )
    resolved = config.get_repo_config("langevc", "ops-engine")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = resolved.resolve_destinations()
    assert len(out) == 1
    assert out[0].forge == "github"
    assert out[0].repo == "LangeVC/ops-engine"
    assert out[0].role == "mirror"
    assert out[0].visibility == "public"
    assert caught, "mirror.github did not emit a DeprecationWarning"
    assert any(
        issubclass(w.category, DeprecationWarning) and "4.0.0" in str(w.message)
        for w in caught
    )


def test_mirror_url_primary_forge_shape_migrates_and_warns():
    """The elementeer/skillweave shape (mirror_url + primary_forge) resolves into
    one Destination with the forge from primary_forge and emits a warning."""
    config = _repo_with_destinations(
        [],
        mirror=MirrorConfig(enabled=True, primary_forge="forgejo", mirror_url="github.com/elementeer/elementeer-mcp"),
    )
    resolved = config.get_repo_config("langevc", "ops-engine")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = resolved.resolve_destinations()
    assert len(out) == 1
    assert out[0].forge == "forgejo"
    assert out[0].repo == "github.com/elementeer/elementeer-mcp"
    assert out[0].role == "mirror"
    assert out[0].visibility == ""
    assert caught, "mirror_url did not emit a DeprecationWarning"
    assert any(
        issubclass(w.category, DeprecationWarning) and "4.0.0" in str(w.message)
        for w in caught
    )


def test_absent_mirror_section_resolves_empty():
    """The capacium/fusionaize shape (no mirror section) resolves to an empty
    list without warning — the deliberate unmirrored case."""
    config = OpsEngineConfig(
        orgs={"capacium": OrgConfig(repositories={"capacium": RepoConfig()})}
    )
    resolved = config.get_repo_config("capacium", "capacium")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        out = resolved.resolve_destinations()
    assert out == []


def test_destinations_and_deprecated_alias_coexist():
    """A repo with an explicit list AND a deprecated alias resolves both, the
    list first, with the alias warning still firing."""
    config = _repo_with_destinations(
        [Destination(forge="local", repo="/srv/git/ops-engine.git", role="replica")],
        mirror=MirrorConfig(github="LangeVC/ops-engine", visibility="public"),
    )
    resolved = config.get_repo_config("langevc", "ops-engine")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = resolved.resolve_destinations()
    assert len(out) == 2
    assert out[0].forge == "local"
    assert out[1].forge == "github"
    assert caught


def test_github_only_destination_without_forgejo():
    """P1 (DST-001 AC4): a GitHub-only adopter expresses a destination with no
    Forgejo involvement — a destination whose forge value is github and whose
    config never mentions forgejo."""
    config = OpsEngineConfig(
        orgs={
            "solo": OrgConfig(
                repositories={
                    "solo-repo": RepoConfig(
                        destinations=[
                            Destination(forge="github", repo="solo/solo-repo", role="mirror", visibility="public"),
                        ]
                    )
                }
            )
        }
    )
    resolved = config.get_repo_config("solo", "solo-repo")
    out = resolved.resolve_destinations()
    assert len(out) == 1
    assert out[0].forge == "github"
    assert out[0].repo == "solo/solo-repo"
    assert "forgejo" not in out[0].model_dump().values()
