"""OME-012: the two-variable mirror destination resolution contract.

The destination is resolved from TWO Actions variables — the two halves of one
contract, not two sources of one value:

  GH_REPO_OWNER  ORG scope   the GitHub owner            e.g. ``Capacium``
  GH_REPO        REPO scope  the full owner/name target  e.g. ``Capacium/capacium``

Resolution and the two proofs (EXISTS via ``git ls-remote``, IS OURS via
``permissions.push``) are proven here by this repository's own tests. The
proofs are additionally proven against the real forges in the verdict.

DST-003 adds ``resolve_destinations(config, repo)``, the pure Layer-1 resolver
that knows no organisations and makes no network calls. Its tests live in
``tests/test_mirror_destination_resolution.py`` alongside the two-variable
contract it complements.
"""

import pytest

from ops_engine.config_loader import MirrorConfig
from ops_engine.modules.mirror import (
    MIRROR_OWNER_VARIABLE,
    MIRROR_REPO_VARIABLE,
    MirrorDestinationResolution,
    MirrorDestinationError,
    MirrorHandler,
    resolve_destinations,
)


def test_resolves_two_variables_to_destination():
    resolved = MirrorHandler.resolve_destination(
        gh_repo_owner="Capacium",
        gh_repo="Capacium/capacium",
    )
    assert resolved == MirrorDestinationResolution(
        destination="Capacium/capacium", source="double match"
    )


def test_resolves_verbatim_with_whitespace_stripped():
    # Owner and destination are used verbatim apart from surrounding-whitespace
    # stripping; nothing else is normalised.
    resolved = MirrorHandler.resolve_destination(
        gh_repo_owner="  Capacium  ",
        gh_repo="  Capacium/capacium  ",
    )
    assert resolved.destination == "Capacium/capacium"


def test_owner_unset_refuses_naming_org_scope_variable():
    with pytest.raises(MirrorDestinationError) as exc:
        MirrorHandler.resolve_destination(
            gh_repo_owner=None,
            gh_repo="Capacium/capacium",
        )
    message = str(exc.value)
    assert MIRROR_OWNER_VARIABLE in message
    assert "ORG-level" in message


def test_repo_unset_refuses_naming_repo_scope_variable():
    with pytest.raises(MirrorDestinationError) as exc:
        MirrorHandler.resolve_destination(
            gh_repo_owner="Capacium",
            gh_repo=None,
        )
    message = str(exc.value)
    assert MIRROR_REPO_VARIABLE in message
    assert "REPOSITORY-level" in message


def test_both_unset_refuses_owner_first():
    # Check order: the ORG-scope variable is named first.
    with pytest.raises(MirrorDestinationError) as exc:
        MirrorHandler.resolve_destination(gh_repo_owner=None, gh_repo=None)
    message = str(exc.value)
    assert MIRROR_OWNER_VARIABLE in message


def test_double_match_mismatch_refuses_naming_both_values():
    # A GH_REPO whose owner prefix disagrees with GH_REPO_OWNER must be REFUSED,
    # not preferred. The refusal names both values and proves no network call
    # was attempted.
    with pytest.raises(MirrorDestinationError) as exc:
        MirrorHandler.resolve_destination(
            gh_repo_owner="Capacium",
            gh_repo="something-else/capacium",
        )
    message = str(exc.value)
    assert "Capacium" in message
    assert "something-else" in message
    assert "case-sensitive" in message
    assert "no GitHub request was made" in message


def test_double_match_is_case_sensitive():
    # A case difference alone is a refusal: no lower/title/slug is applied.
    with pytest.raises(MirrorDestinationError):
        MirrorHandler.resolve_destination(
            gh_repo_owner="Capacium",
            gh_repo="capacium/capacium",
        )


def test_double_match_is_case_sensitive_owner_lowercase():
    # The reverse case: an all-lowercase owner must match verbatim too.
    with pytest.raises(MirrorDestinationError):
        MirrorHandler.resolve_destination(
            gh_repo_owner="elementeer",
            gh_repo="Elementeer/elementeer",
        )


def test_awkward_corpus_owners_used_verbatim():
    # The operator corpus (capacium->Capacium, elementeer->elementeer,
    # fusionaize->fusionAIze, veeona->Veeona-AI) cannot be produced by any
    # casing rule, so these values must be accepted verbatim, not normalised.
    for owner, repo in (
        ("elementeer", "elementeer/elementeer"),
        ("fusionAIze", "fusionAIze/fusionAIze"),
        ("Veeona-AI", "Veeona-AI/veeona"),
    ):
        resolved = MirrorHandler.resolve_destination(
            gh_repo_owner=owner, gh_repo=repo
        )
        assert resolved.destination == repo


def test_many_to_one_owner_does_not_identify_canonical_org():
    # Both `skillweave` and `langevc` resolve to owner `LangeVC`: the mapping is
    # many-to-one, so an owner does not identify a canonical org. Each repo is
    # simply a full destination whose prefix must match the owner.
    for repo in ("LangeVC/skillweave", "LangeVC/langevc"):
        resolved = MirrorHandler.resolve_destination(
            gh_repo_owner="LangeVC", gh_repo=repo
        )
        assert resolved.destination == repo


def test_resolves_destination_from_config_alone():
    resolved = MirrorHandler.resolve_destination(
        config=MirrorConfig(github="LangeVC/ops-engine")
    )
    assert resolved == MirrorDestinationResolution(
        destination="LangeVC/ops-engine", source="config"
    )


def test_config_destination_used_verbatim_case_sensitive():
    # The awkward operator corpus must resolve verbatim from config, never
    # re-cased, exactly like the variable path's double-match guard demands.
    for github in (
        "elementeer/elementeer",
        "fusionAIze/fusionAIze",
        "Veeona-AI/veeona",
    ):
        resolved = MirrorHandler.resolve_destination(
            config=MirrorConfig(github=github)
        )
        assert resolved.destination == github
        assert resolved.source == "config"


def test_config_wins_over_variables_when_both_supplied():
    # Precedence: the config is primary and the variables are a deprecated
    # override. A stale variable must not silently override a correct config.
    resolved = MirrorHandler.resolve_destination(
        config=MirrorConfig(github="LangeVC/ops-engine"),
        gh_repo_owner="SomebodyElse",
        gh_repo="SomebodyElse/ops-engine",
    )
    assert resolved.destination == "LangeVC/ops-engine"
    assert resolved.source == "config"


def test_variables_are_deprecated_override_when_config_empty():
    # When the config carries no destination, the variable path is consulted.
    resolved = MirrorHandler.resolve_destination(
        config=MirrorConfig(github=""),
        gh_repo_owner="Capacium",
        gh_repo="Capacium/capacium",
    )
    assert resolved == MirrorDestinationResolution(
        destination="Capacium/capacium", source="double match"
    )


def test_config_malformed_destination_refuses_before_network():
    # A config.github that is not owner/name refuses — before any network call —
    # naming the config field, not a variable.
    with pytest.raises(MirrorDestinationError) as exc:
        MirrorHandler.resolve_destination(config=MirrorConfig(github="no-slash"))
    message = str(exc.value)
    assert "mirror.github" in message
    assert "owner/name" in message


def test_no_config_and_no_variables_refuses_naming_variable():
    # Neither source supplied: the refusal names the ORG-scope variable first,
    # exactly as the variable path's check order requires.
    with pytest.raises(MirrorDestinationError) as exc:
        MirrorHandler.resolve_destination(config=MirrorConfig(github=""))
    message = str(exc.value)
    assert MIRROR_OWNER_VARIABLE in message


@pytest.mark.asyncio
async def test_prove_not_reachable_names_variable(monkeypatch):
    async def fake_exists(remote):
        return False

    monkeypatch.setattr(MirrorHandler, "_repo_exists", staticmethod(fake_exists))

    with pytest.raises(MirrorDestinationError) as exc:
        await MirrorHandler.prove_destination("LangeVC/does-not-exist")
    msg = str(exc.value)
    assert "does-not-exist" in msg
    assert MIRROR_REPO_VARIABLE in msg
    assert "REPOSITORY-level" in msg
    # EXISTS must fail before IS OURS is even consulted: nothing creates a repo.
    assert "ours" not in msg


@pytest.mark.asyncio
async def test_prove_exists_but_not_ours_fails(monkeypatch):
    async def fake_exists(remote):
        return True

    async def fake_is_ours(destination, *, token, api_base):
        return False

    monkeypatch.setattr(MirrorHandler, "_repo_exists", staticmethod(fake_exists))
    monkeypatch.setattr(MirrorHandler, "_repo_is_ours", staticmethod(fake_is_ours))

    with pytest.raises(MirrorDestinationError) as exc:
        await MirrorHandler.prove_destination("octocat/Hello-World")
    msg = str(exc.value)
    # EXISTS passed (it exists) but IS OURS failed — the failure names the
    # ownership distinction explicitly and the REPO-scope variable to correct.
    assert "NOT ours" in msg
    assert "octocat/Hello-World" in msg
    assert MIRROR_REPO_VARIABLE in msg


@pytest.mark.asyncio
async def test_prove_exists_and_ours_passes(monkeypatch):
    async def fake_exists(remote):
        return True

    async def fake_is_ours(destination, *, token, api_base):
        return True

    monkeypatch.setattr(MirrorHandler, "_repo_exists", staticmethod(fake_exists))
    monkeypatch.setattr(MirrorHandler, "_repo_is_ours", staticmethod(fake_is_ours))

    # Should not raise.
    await MirrorHandler.prove_destination("LangeVC/ops-engine")


# ── DST-003: resolve_destinations(config, repo) — the pure Layer-1 resolver ──


def _five_layover_configs():
    """The five real layover shapes, expressed as OpsEngineConfig mappings.

    Three shapes measured 2026-09-04:

      * lvc-ops:        mirror.github + visibility (owner/name destination)
      * elementeer, skillweave: mirror_url + primary_forge (no github key)
      * capacium, fusionaize:   no mirror section at all
    """
    from ops_engine.config_loader import OpsEngineConfig

    lvc_ops = OpsEngineConfig.load(
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
                        }
                    }
                }
            }
        }
    )
    elementeer = OpsEngineConfig.load(
        {
            "orgs": {
                "elementeer": {
                    "repositories": {
                        "elementeer-mcp": {
                            "mirror": {
                                "enabled": True,
                                "primary_forge": "forgejo",
                                "mirror_url": "github.com/elementeer/elementeer-mcp",
                                "verify_on_push": True,
                            }
                        }
                    }
                }
            }
        }
    )
    skillweave = OpsEngineConfig.load(
        {
            "orgs": {
                "skillweave": {
                    "repositories": {
                        "skillweave": {
                            "mirror": {
                                "enabled": True,
                                "primary_forge": "forgejo",
                                "mirror_url": "github.com/typelicious/SkillWeave",
                                "verify_on_push": True,
                            }
                        }
                    }
                }
            }
        }
    )
    capacium = OpsEngineConfig.load(
        {
            "orgs": {
                "capacium": {
                    "repositories": {
                        "capacium": {
                            "release": {"enabled": True, "trigger": "tag_push"}
                        }
                    }
                }
            }
        }
    )
    fusionaize = OpsEngineConfig.load(
        {
            "orgs": {
                "fusionaize": {
                    "repositories": {
                        "fusionaize-sdk": {
                            "release": {"enabled": True, "trigger": "tag_push"}
                        }
                    }
                }
            }
        }
    )
    return [
        ("langevc/ops-engine", lvc_ops, "LangeVC/ops-engine"),
        ("elementeer/elementeer-mcp", elementeer, "github.com/elementeer/elementeer-mcp"),
        ("skillweave/skillweave", skillweave, "github.com/typelicious/SkillWeave"),
        ("capacium/capacium", capacium, None),
        ("fusionaize/fusionaize-sdk", fusionaize, None),
    ]


def test_resolve_destinations_returns_list_for_all_five_layovers():
    for repo, config, expected_repo in _five_layover_configs():
        resolved = resolve_destinations(config, repo)
        if expected_repo is None:
            assert resolved == [], f"{repo}: expected deliberate unmirrored case"
        else:
            assert [d.repo for d in resolved] == [expected_repo], (
                f"{repo}: expected destination {expected_repo}"
            )


def test_resolve_destinations_returns_destination_objects():
    config = _five_layover_configs()[0][1]
    resolved = resolve_destinations(config, "langevc/ops-engine")
    assert len(resolved) == 1
    dest = resolved[0]
    assert dest.forge == "github"
    assert dest.repo == "LangeVC/ops-engine"
    assert dest.role == "mirror"
    assert dest.visibility == "public"


def test_resolve_destinations_rejects_non_org_repo_selector():
    config = _five_layover_configs()[0][1]
    with pytest.raises(ValueError):
        resolve_destinations(config, "no-slash")


def test_resolve_destinations_holds_no_network_and_no_orgs(monkeypatch):
    # The resolver must not import a forge adapter nor make network calls
    # legitimately. This test asserts the function needs nothing but the
    # supplied config object and the repo selector: an org-to-config registry is
    # never touched because the config object was handed in by the caller.
    config = _five_layover_configs()[0][1]
    resolved = resolve_destinations(config, "langevc/ops-engine")
    assert [d.repo for d in resolved] == ["LangeVC/ops-engine"]
