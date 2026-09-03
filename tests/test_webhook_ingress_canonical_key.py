"""Canonical org key ingress: the ingress derives the canonical key.

The Forgejo webhook ingress derives the canonical org key from
``repository.full_name`` (the ``org/repo`` form Forgejo actually sends), with
``owner.username`` as the fallback (see :func:`canonical_org_key`, LVC-238). No
caller downstream of the ingress may receive a display-case org name. This is
the proof that the ingress — not just ``get_repo_config`` — canonicalizes the
key, and that it refuses (rather than keying on a display-only field) when no
org-sourced field is present.
"""

import hashlib
import hmac
import json

import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter


SECRET = "test-secret"


def _sign(payload: bytes) -> str:
    return hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()


def _forgejo_payload(owner: dict, repo_name: str = "faigrid") -> bytes:
    """A Forgejo webhook body with a display-case owner.

    ``repository.full_name`` is built as ``"<owner full_name>/<repo_name>"`` —
    the display-case org handle followed by the repo name, the ``org/repo``
    shape :func:`canonical_org_key` reads as its org source.
    """
    repository = {"owner": owner, "name": repo_name, "full_name": f"{owner['full_name']}/{repo_name}"}
    return json.dumps({"repository": repository, "action": "opened", "sender": {"username": "someone"}}).encode()


@pytest.fixture
def adapter():
    return ForgejoAdapter(
        base_url="https://git.example.com", token="test-token", webhook_secret=SECRET,
    )


def _signed_headers(payload: bytes) -> dict[str, str]:
    return {"x-forgejo-signature": _sign(payload)}


@pytest.mark.asyncio
async def test_ingress_derives_canonical_org_key_from_full_name(adapter):
    """A payload carrying ``fusionAIze/faigrid`` (display case) reaches a
    handler with the canonical key ``fusionaize``."""
    owner = {"id": 1, "login": "fusionAIze", "full_name": "fusionAIze", "username": "fusionAIze"}
    payload = _forgejo_payload(owner)

    event = await adapter.parse_webhook(_signed_headers(payload), payload)

    assert event["org"] == "fusionaize"


@pytest.mark.asyncio
async def test_ingress_repo_is_canonical_full_name(adapter):
    """The repo passed onward concatenates the canonical org key, never display case."""
    owner = {
        "id": 1,
        "login": "fusionAIze",
        "full_name": "fusionAIze",
        "username": "fusionAIze",
        "lower_name": "fusionaize",
    }
    payload = _forgejo_payload(owner)

    event = await adapter.parse_webhook(_signed_headers(payload), payload)

    assert event["repo"] == "fusionaize/faigrid"


@pytest.mark.asyncio
async def test_ingress_refuses_without_org_sourced_field(adapter):
    """Without ``repository.full_name`` (org/repo form) or ``owner.username`` the
    ingress refuses with a named error — it never keys on the display-only
    ``owner.login`` / ``owner.full_name``."""
    repository = {
        "owner": {"id": 1, "login": "fusionAIze", "full_name": "fusionAIze"},
        "name": "faigrid",
        "full_name": "A Single Display Name",  # no '/', so no org portion
    }
    payload = json.dumps(
        {"repository": repository, "action": "opened", "sender": {"username": "someone"}}
    ).encode()

    with pytest.raises(ValueError, match="cannot derive canonical org key"):
        await adapter.parse_webhook(_signed_headers(payload), payload)
