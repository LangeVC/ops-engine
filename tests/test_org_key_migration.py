"""FFR-200-2: migrate display-case org keys to canonical lowercase keys.

Two acceptance criteria:
  1. Existing display-case keys are migrated to canonical keys with the old
     value preserved as github.login.
  2. Two keys that collide after lowercasing fail the load with a named error.
"""

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATE_SCRIPT = REPO_ROOT / "scripts" / "migrate-org-keys.py"


def _load_migrate_module():
    spec = importlib.util.spec_from_file_location("migrate_org_keys", MIGRATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migrate = _load_migrate_module()
migrate_orgs = migrate.migrate_orgs
migrate_config = migrate.migrate_config
OrgKeyCollisionError = migrate.OrgKeyCollisionError


def test_display_case_key_migrates_to_lowercase_and_preserves_login():
    orgs = {
        "fusionAIze": {
            "release": {"enabled": True},
        },
    }
    migrated = migrate_orgs(orgs)
    assert list(migrated) == ["fusionaize"]
    assert migrated["fusionaize"]["github"]["login"] == "fusionAIze"
    assert migrated["fusionaize"]["release"]["enabled"] is True


def test_langevc_display_key_preserves_handle_as_login():
    orgs = {
        "LangeVC": {"repositories": {"faiops": {}}},
    }
    migrated = migrate_orgs(orgs)
    assert list(migrated) == ["langevc"]
    assert migrated["langevc"]["github"]["login"] == "LangeVC"


def test_already_lowercase_key_still_records_login():
    orgs = {
        "elementeer": {"release": {"enabled": True}},
    }
    migrated = migrate_orgs(orgs)
    assert list(migrated) == ["elementeer"]
    assert migrated["elementeer"]["github"]["login"] == "elementeer"


def test_existing_github_login_is_not_clobbered():
    orgs = {
        "fusionAIze": {"github": {"login": "some-handle"}},
    }
    migrated = migrate_orgs(orgs)
    assert migrated["fusionaize"]["github"]["login"] == "some-handle"


def test_input_mapping_is_not_mutated():
    orgs = {"fusionAIze": {"release": {"enabled": True}}}
    migrate_orgs(orgs)
    assert list(orgs) == ["fusionAIze"]
    assert "github" not in orgs["fusionAIze"]


def test_collision_after_lowercasing_raises_named_error():
    orgs = {
        "fusionAIze": {"release": {"enabled": True}},
        "FusionAIze": {"release": {"enabled": False}},
    }
    with pytest.raises(OrgKeyCollisionError) as excinfo:
        migrate_orgs(orgs)
    assert excinfo.value.canonical == "fusionaize"
    assert set(excinfo.value.keys) == {"fusionAIze", "FusionAIze"}


def test_collision_between_display_and_lowercase_key_raises():
    orgs = {
        "fusionaize": {"release": {"enabled": True}},
        "fusionAIze": {"release": {"enabled": False}},
    }
    with pytest.raises(OrgKeyCollisionError):
        migrate_orgs(orgs)


def test_non_mapping_org_value_is_rekeyed_without_inspection():
    orgs = {"fusionAIze": "not-a-mapping"}
    migrated = migrate_orgs(orgs)
    assert list(migrated) == ["fusionaize"]
    assert migrated["fusionaize"] == "not-a-mapping"


def test_migrate_config_without_orgs_is_a_read_failure():
    """LNF-100-1: a config with no org keys is a read failure, not '0 orgs'.

    Previously ``migrate_config({})`` shipped ``{"orgs": {}}`` — reporting
    success on a file from which no org keys were read. Criterion 2 of LNF-100-1
    supersedes that: zero org keys names the file and the expected layout.
    """
    with pytest.raises(migrate.OrgLayoutError):
        migrate_config({})


def test_migrate_config_without_orgs_still_a_read_failure():
    """A config with only metadata (no org keys) is still a read failure."""
    with pytest.raises(migrate.OrgLayoutError):
        migrate_config({"repositories": {"a": 1}})


def test_migrate_config_never_emits_a_hard_coded_org_name():
    migrated = migrate_config(
        {"langevc": {"repositories": {"a": 1}}, "repositories": {"keep": {}}}
    )
    assert "langevc" in migrated["orgs"]
    assert "Capacium" not in migrated
    assert "fusionAIze" not in migrated


def test_migrated_dump_of_empty_template_is_a_read_failure():
    """LNF-100-1: a template with no org keys must fail, not emit empty orgs."""
    with pytest.raises(migrate.OrgLayoutError):
        migrate_config({})
