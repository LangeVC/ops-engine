"""FFR-200-1 dispatch 3: the webhook ingress derives the canonical org key.

The Forgejo webhook ingress must derive the canonical org key from
``repository.owner.lower_name`` (via :func:`canonical_org_key`) and pass that
canonical key onward. No caller downstream of the ingress may receive a
display-case org name. This is the red proof that the ingress, and not just
``get_repo_config``, canonicalizes the key.
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
    """A Forgejo webhook body with a display-case owner and a lower_name."""
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
async def test_ingress_derives_canonical_org_key_from_lower_name(adapter):
    """A payload carrying ``fusionAIze/faigrid`` (display case) reaches a
    handler with the canonical key ``fusionaize``."""
    owner = {"id": 1, "login": "fusionAIze", "full_name": "fusionAIze", "username": "fusionAIze", "lower_name": "fusionaize"}
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
async def test_ingress_refuses_without_lower_name(adapter):
    """Without owner.lower_name the ingress must not fall back to full_name/login."""
    owner = {"id": 1, "login": "fusionAIze", "full_name": "fusionAIze", "username": "fusionAIze"}
    payload = _forgejo_payload(owner)

    with pytest.raises(ValueError):
        await adapter.parse_webhook(_signed_headers(payload), payload)
