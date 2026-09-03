"""OME-002: the mirror destination resolution contract.

Pure precedence resolution is proven here by this repository's own tests. The
two proofs (EXISTS via ``git ls-remote``, IS OURS via ``permissions.push``)
have unit tests here and are additionally proven against the real forges in the
verdict (see the acceptance-criteria evidence), where a repo we do not own is
shown to exist but fail the IS OURS proof.
"""

import pytest

from ops_engine.modules.mirror import (
    MirrorDestinationResolution,
    MirrorDestinationError,
    MirrorHandler,
)


def test_repo_override_wins_over_org_declaration():
    resolved = MirrorHandler.resolve_destination(
        repo_override="LangeVC/skillweave",
        org_github_login="LangeVC",
        repo_name="repo-a",
    )
    assert resolved == MirrorDestinationResolution(
        destination="LangeVC/skillweave", source="repo override"
    )


def test_org_declaration_composes_destination():
    resolved = MirrorHandler.resolve_destination(
        org_github_login="LangeVC",
        repo_name="skillweave",
    )
    assert resolved.destination == "LangeVC/skillweave"
    assert resolved.source == "org declaration"


def test_no_destination_fails_naming_variable_and_value():
    with pytest.raises(MirrorDestinationError) as exc:
        MirrorHandler.resolve_destination(
            org_github_login=None,
            repo_name=None,
        )
    message = str(exc.value)
    assert "GH_REPOSITORY" in message
    assert "<github-org>/<repo>" in message


def test_org_only_missing_repo_name_does_not_silently_compose():
    # org login present but no repo name and no override -> cannot compose a
    # destination, must fail (never a half-formed plausible-but-wrong one).
    with pytest.raises(MirrorDestinationError):
        MirrorHandler.resolve_destination(org_github_login="LangeVC", repo_name=None)


def test_fallback_is_gated_not_trusted():
    # A bare fallback is accepted only as a gated candidate, explicitly marked
    # so it can never be used without first passing prove_destination.
    resolved = MirrorHandler.resolve_destination(
        fallback="skillweave/skillweave",
    )
    assert resolved.destination == "skillweave/skillweave"
    assert resolved.source == "gated fallback"


def test_precedence_repo_beats_fallback_and_org():
    resolved = MirrorHandler.resolve_destination(
        repo_override="LangeVC/x",
        org_github_login="LangeVC",
        repo_name="y",
        fallback="some/fallback",
    )
    assert resolved.destination == "LangeVC/x"
    assert resolved.source == "repo override"


@pytest.mark.asyncio
async def test_prove_not_reachable_names_variable(monkeypatch):
    async def fake_exists(remote):
        return False

    monkeypatch.setattr(MirrorHandler, "_repo_exists", staticmethod(fake_exists))

    with pytest.raises(MirrorDestinationError) as exc:
        await MirrorHandler.prove_destination("LangeVC/does-not-exist")
    msg = str(exc.value)
    assert "does-not-exist" in msg
    assert "GH_REPOSITORY" in msg
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
    # ownership distinction explicitly.
    assert "NOT ours" in msg
    assert "octocat/Hello-World" in msg


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

