"""FFR-200-1: the canonical org key is the Forgejo lower_name.

Every internal org lookup keys on the Forgejo ``lower_name`` (always lowercase).
The webhook ingress derives that key from ``owner.lower_name`` only — never from
``full_name``, ``login``, or ``username``, which carry display case or a free-form
display name and would key on the wrong string (see LVC-229).
"""

import pytest

from ops_engine.config_loader import (
    OpsEngineConfig,
    OrgConfig,
    RepoConfig,
    ReleaseConfig,
    canonical_org_key,
)


def _forgejo_repository(*, lower_name=None, owner_full_name="Display Case", login="DisplayCase", username="display-case"):
    owner = {"id": 1, "login": login, "full_name": owner_full_name, "username": username}
    if lower_name is not None:
        owner["lower_name"] = lower_name
    return {
        "owner": owner,
        "name": "faigrid",
        "full_name": f"{owner['full_name']}/faigrid",
    }


def test_canonical_org_key_is_lower_name():
    repo = _forgejo_repository(lower_name="fusionaize", owner_full_name="fusionAIze", login="fusionAIze")
    assert canonical_org_key(repo) == "fusionaize"


def test_canonical_org_key_ignores_display_full_name_and_login():
    """lower_name wins even when full_name is a free-form display name and login is cased."""
    repo = _forgejo_repository(
        lower_name="langevc",
        owner_full_name="Lange Ventures & Consulting",
        login="LangeVC",
    )
    assert canonical_org_key(repo) == "langevc"


def test_canonical_org_key_refuses_without_lower_name():
    """>The ingress must never fall back to full_name/login/username when lower_name is absent."""
    repo = _forgejo_repository(lower_name=None, owner_full_name="fusionAIze", login="fusionAIze")
    with pytest.raises(ValueError):
        canonical_org_key(repo)


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
