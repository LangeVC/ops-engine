"""FFR-200-1 + LVC-238: the canonical org key derives from the webhook payload.

Every internal org lookup keys on a canonical org key (always lowercase). The
webhook ingress derives it from ``repository.full_name`` (the ``org/repo`` form
Forgejo actually sends, already lowercase) and falls back to
``owner.username``. ``owner.login``, ``owner.full_name`` and free-form display
names are display or account identifiers and are never used as a key.

LVC-238 correction: ``owner.lower_name`` is a column of Forgejo's database
schema (``user.lower_name``), not a field of the webhook payload. Deriving the
key from it made the ingress raise on every real payload.
"""

import pytest

from ops_engine.config_loader import (
    ForgejoIdentity,
    GithubIdentity,
    OpsEngineConfig,
    OrgConfig,
    RepoConfig,
    ReleaseConfig,
    canonical_org_key,
)


def _forgejo_repository(*, org_handle="fusionaize", owner_full_name="fusionAIze", login="fusionAIze"):
    """A repository mapping shaped like a real Forgejo webhook payload.

    ``repository.full_name`` is ``"org_handle/repo"`` — the org portion is the
    always-lowercase handle. The owner carries display attributes (``login``,
    ``full_name``) that are never the key.
    """
    owner = {"id": 1, "login": login, "full_name": owner_full_name, "username": org_handle}
    return {
        "owner": owner,
        "name": "faigrid",
        "full_name": f"{org_handle}/faigrid",
    }


def test_canonical_org_key_derives_from_repo_full_name():
    repo = _forgejo_repository(org_handle="fusionaize", owner_full_name="fusionAIze", login="fusionAIze")
    assert canonical_org_key(repo) == "fusionaize"


def test_canonical_org_key_ignores_owner_display_name_and_login():
    """Owner full_name (a free-form display name) and login (display case) are
    never the key: the key comes from the repository full_name handle."""
    repo = _forgejo_repository(org_handle="langevc", owner_full_name="Lange Ventures & Consulting", login="LangeVC")
    assert canonical_org_key(repo) == "langevc"


def test_canonical_org_key_derives_without_any_lower_name():
    """The API sends no ``owner.lower_name`` at all (LVC-238): the function must
    derive the key from the payload fields, not refuse on the absent schema column."""
    repo = _forgejo_repository(org_handle="fusionaize", owner_full_name="fusionAIze", login="fusionAIze")
    assert "lower_name" not in repo["owner"]
    assert canonical_org_key(repo) == "fusionaize"


def test_canonical_org_key_refuses_without_owner():
    with pytest.raises(ValueError):
        canonical_org_key({})


def test_get_repo_config_resolves_by_canonical_lower_name():
    config = OpsEngineConfig(
        orgs={
            "fusionaize": OrgConfig(
                release=ReleaseConfig(enabled=True, trigger="tag_push"),
                repositories={"faigrid": RepoConfig()},
            )
        }
    )
    resolved = config.get_repo_config("fusionaize", "faigrid")
    assert resolved.release.enabled is True


def test_get_repo_config_resolves_display_case_to_canonical_key():
    """The case-mismatch defect (LVC-229): a display-cased lookup still resolves
    against the config stored under the lowercase lower_name."""
    config = OpsEngineConfig(
        orgs={
            "fusionaize": OrgConfig(
                repositories={"faigrid": RepoConfig(release=ReleaseConfig(enabled=True))}
            )
        }
    )
    resolved = config.get_repo_config("fusionAIze", "faigrid")
    assert resolved.release.enabled is True


def test_get_repo_config_repo_name_is_not_case_folded():
    """Only the org key is canonical; the repository name stays exact."""
    config = OpsEngineConfig(
        orgs={
            "fusionaize": OrgConfig(
                repositories={"faigrid": RepoConfig(release=ReleaseConfig(enabled=True))}
            )
        }
    )
    resolved = config.get_repo_config("fusionaize", "Faigrid")
    assert resolved.release is None


def test_display_name_and_github_login_are_attributes_not_keys():
    """forgejo.display_name and github.login are stored attributes under the
    canonical lower_name key; they are never the lookup key themselves."""
    config = OpsEngineConfig(
        orgs={
            "fusionaize": OrgConfig(
                forgejo=ForgejoIdentity(display_name="fusionAIze"),
                github=GithubIdentity(login="fusionAIze"),
                repositories={"faigrid": RepoConfig()},
            )
        }
    )
    org = config.orgs["fusionaize"]
    assert org.forgejo.display_name == "fusionAIze"
    assert org.github.login == "fusionAIze"


def test_display_name_and_github_login_load_from_raw_mapping():
    """A raw config.yml carries forgejo.display_name and github.login as data."""
    raw = {
        "orgs": {
            "fusionaize": {
                "forgejo": {"display_name": "fusionAIze"},
                "github": {"login": "fusionAIze"},
                "repositories": {"faigrid": {"release": {"enabled": True}}},
            }
        }
    }
    config = OpsEngineConfig.load(raw)
    org = config.orgs["fusionaize"]
    assert org.forgejo.display_name == "fusionAIze"
    assert org.github.login == "fusionAIze"
    assert config.get_repo_config("fusionaize", "faigrid").release.enabled is True


def test_identity_attributes_do_not_affect_lookup_key():
    """A display-cased github.login or forgejo.display_name is never a key:
    every lookup still resolves through the canonical lower_name only."""
    config = OpsEngineConfig(
        orgs={
            "fusionaize": OrgConfig(
                forgejo=ForgejoIdentity(display_name="Lange Ventures & Consulting"),
                github=GithubIdentity(login="LangeVC"),
                repositories={"faigrid": RepoConfig(release=ReleaseConfig(enabled=True))},
            )
        }
    )
    assert "fusionAIze" not in config.orgs
    assert config.get_repo_config("fusionaize", "faigrid").release.enabled is True
