"""FFR-200-1 dispatch 3 + LVC-238: the webhook ingress derives the canonical org key.

The Forgejo webhook ingress must derive the canonical org key from the payload
(via :func:`canonical_org_key`) and pass that canonical key onward. No caller
downstream of the ingress may receive a display-case org name.

LVC-238: the key is derived from ``repository.full_name`` (the ``org/repo``
handle Forgejo actually sends) and falls back to ``owner.username``. The old
``owner.lower_name`` derivation was built against Forgejo's database schema,
not against a webhook payload, and raised on every real event.
"""

import json

import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter


def _forgejo_payload(owner: dict, repo_name: str = "faigrid", org_handle: str = "fusionaize") -> bytes:
    """A Forgejo webhook body with a display-case owner; the key comes from the
    ``repository.full_name`` handle, not from ``owner.lower_name`` (LVC-238)."""
    repository = {"owner": owner, "name": repo_name, "full_name": f"{org_handle}/{repo_name}"}
    return json.dumps({"repository": repository, "action": "opened", "sender": {"username": "someone"}}).encode()


@pytest.fixture
def adapter():
    return ForgejoAdapter(
        base_url="https://git.example.com", token="test-token", webhook_secret="",
    )


@pytest.mark.asyncio
async def test_ingress_derives_canonical_org_key_without_lower_name(adapter):
    """A payload carrying no ``owner.lower_name`` (the API never sends it) still
    reaches a handler with the canonical key ``fusionaize`` (LVC-238)."""
    owner = {"id": 1, "login": "fusionAIze", "full_name": "fusionAIze", "username": "fusionaize"}
    payload = _forgejo_payload(owner, org_handle="fusionaize")

    event = await adapter.parse_webhook({}, payload)

    assert event["org"] == "fusionaize"


@pytest.mark.asyncio
async def test_ingress_repo_is_canonical_full_name(adapter):
    """The repo passed onward concatenates the canonical org key, never display case."""
    owner = {
        "id": 1,
        "login": "fusionAIze",
        "full_name": "fusionAIze",
        "username": "fusionaize",
    }
    payload = _forgejo_payload(owner, org_handle="fusionaize")

    event = await adapter.parse_webhook({}, payload)

    assert event["repo"] == "fusionaize/faigrid"


@pytest.mark.asyncio
async def test_ingress_refuses_without_any_usable_org_field(adapter):
    """With no usable org field (no 'org/repo' full_name, no owner.username) the
    ingress fails with a named error rather than keying on a display attribute."""
    owner = {"id": 1, "login": "fusionAIze", "full_name": "fusionAIze"}
    repository = {"owner": owner, "name": "faigrid", "full_name": "fusionAIze"}  # no '/'
    payload = json.dumps({"repository": repository, "action": "opened"}).encode()

    with pytest.raises(ValueError, match="cannot derive canonical org key"):
        await adapter.parse_webhook({}, payload)
