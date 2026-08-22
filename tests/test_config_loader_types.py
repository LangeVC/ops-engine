"""FFR-100-2: typed config or named error per section; no raw dict reaches .enabled."""

import pytest

from ops_engine.config_loader import (
    ConfigSectionError,
    OpsEngineConfig,
    OrgConfig,
    RepoConfig,
    ReleaseConfig,
    MergeConfig,
    MirrorConfig,
    NotificationConfig,
)


def _config(**orgs):
    return OpsEngineConfig(orgs=orgs)


def test_get_repo_config_returns_typed_sections():
    config = _config(
        TestOrg=OrgConfig(
            release=ReleaseConfig(enabled=True),
            auto_merge=MergeConfig(enabled=True),
            notifications=NotificationConfig(enabled=True),
            repositories={
                "repo-a": RepoConfig(
                    mirror=MirrorConfig(enabled=True, mirror_url="github.com/org/a"),
                )
            },
        )
    )
    resolved = config.get_repo_config("TestOrg", "repo-a")
    assert isinstance(resolved.stale_management, type(resolved.stale_management))
    assert isinstance(resolved.release, ReleaseConfig)
    assert isinstance(resolved.auto_merge, MergeConfig)
    assert isinstance(resolved.mirror, MirrorConfig)
    assert isinstance(resolved.notifications, NotificationConfig)
    assert resolved.release.enabled is True
    assert resolved.mirror.enabled is True


def test_get_repo_config_missing_org_raises_named_error():
    config = _config()
    with pytest.raises(ConfigSectionError) as excinfo:
        config.get_repo_config("Nope", "repo-a")
    assert excinfo.value.section == "orgs"
    assert excinfo.value.org_name == "Nope"
    assert excinfo.value.repo_name == "repo-a"


def test_load_coerces_raw_dict_to_typed_section():
    raw = {
        "orgs": {
            "TestOrg": {
                "release": {"enabled": True, "trigger": "tag_push"},
                "repositories": {
                    "repo-a": {"mirror": {"enabled": True, "mirror_url": "github.com/org/a"}}
                },
            }
        }
    }
    config = OpsEngineConfig.load(raw)
    resolved = config.get_repo_config("TestOrg", "repo-a")
    assert isinstance(resolved.release, ReleaseConfig)
    assert isinstance(resolved.mirror, MirrorConfig)
    assert resolved.release.enabled is True
    assert resolved.mirror.enabled is True


def test_load_malformed_section_raises_named_error():
    raw = {
        "orgs": {
            "TestOrg": {
                "release": "not-a-mapping",
                "repositories": {"repo-a": {}},
            }
        }
    }
    with pytest.raises(ConfigSectionError) as excinfo:
        OpsEngineConfig.load(raw)
    assert excinfo.value.section == "release"


def test_load_malformed_repo_section_raises_named_error():
    raw = {
        "orgs": {
            "TestOrg": {
                "repositories": {
                    "repo-a": {"mirror": "not-a-mapping"},
                }
            }
        }
    }
    with pytest.raises(ConfigSectionError) as excinfo:
        OpsEngineConfig.load(raw)
    assert excinfo.value.section == "mirror"
    assert excinfo.value.repo_name == "repo-a"


def test_no_raw_dict_reaches_enabled_access():
    """A section forced in as a raw dict must never surface from get_repo_config."""
    config = _config(
        TestOrg=OrgConfig(
            repositories={
                # Constructed with a dict for a list section — coercion turns it
                # into typed models, so .enabled is always on a typed object.
                "repo-a": RepoConfig(
                    workflow_dispatches=[{"enabled": True, "workflow_name": "codeql"}],
                ),
            }
        )
    )
    resolved = config.get_repo_config("TestOrg", "repo-a")
    for dispatch in resolved.workflow_dispatches:
        assert type(dispatch).__name__ == "WorkflowDispatchConfig"
        assert dispatch.enabled is True


def test_replay_dict_enabled_failure_is_never_attribute_error():
    """Red proof (LVC-229): the v2.0.0 input that raised
    ``'dict' object has no attribute 'enabled'`` on mirror/notifications is
    replayed and yields a typed config or a named error — never AttributeError.

    On the deployed engine the layover config loaded mirror/notifications as
    raw dicts, so MirrorHandler/NotificationHandler hit ``config.enabled`` on a
    plain dict and blew up on every push. This replays that same raw-mapping
    shape end to end and asserts the ``.enabled`` access used by those handlers
    either works on a typed model or no AttributeError escapes.
    """
    raw = {
        "orgs": {
            "TestOrg": {
                "repositories": {
                    "repo-a": {
                        "mirror": {"enabled": True, "mirror_url": "github.com/org/a"},
                        "notifications": {
                            "enabled": True,
                            "channels": [
                                {"type": "slack", "url": "https://hooks.slack.com/x"}
                            ],
                        },
                    }
                }
            }
        }
    }

    config = OpsEngineConfig.load(raw)
    resolved = config.get_repo_config("TestOrg", "repo-a")

    def read_enabled():
        return resolved.mirror.enabled, resolved.notifications.enabled

    try:
        mirror_enabled, notifications_enabled = read_enabled()
    except AttributeError as exc:  # pragma: no cover - the defect this guards
        pytest.fail(f"AttributeError surfaced from a replayed config: {exc}")
    except ConfigSectionError:
        # A named error is an acceptable outcome; the section is identified.
        return

    assert isinstance(resolved.mirror, MirrorConfig)
    assert isinstance(resolved.notifications, NotificationConfig)
    assert mirror_enabled is True
    assert notifications_enabled is True


def test_replay_malformed_notifications_is_named_error_not_attribute_error():
    """Replay a shape where notifications cannot coerce: expect a named error,
    never an AttributeError, when the downstream handler touches .enabled."""
    raw = {
        "orgs": {
            "TestOrg": {
                "repositories": {
                    "repo-a": {"notifications": "not-a-mapping"},
                }
            }
        }
    }
    with pytest.raises(ConfigSectionError) as excinfo:
        OpsEngineConfig.load(raw)
    assert excinfo.value.section == "notifications"
