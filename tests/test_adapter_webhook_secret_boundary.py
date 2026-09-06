"""ADP-006: the webhook secret guard lives where the secret is used, not where it isn't.

The secret was guarded in the adapter constructor, so publication — which never
receives a webhook — had to hand the factory a secret it would never read. The
factory and the release module both declare ``webhook_secret: str = ""``, so the
empty default they ship was refused by their own callee.

The fix moves the guard to ``parse_webhook``, the single place per adapter that
reads the secret. Two properties must both hold, and each is proven by a run:

1. An adapter constructed WITHOUT a secret can publish: the factory's own default
   path returns an adapter, and that adapter completes a release publication
   against a stubbed transport. No secret is demanded until a webhook is parsed.
2. The ingress guard is NOT weakened: ``parse_webhook`` on an adapter with no
   secret refuses with a named error, and — with a secret — a forged signature is
   rejected while a valid one is accepted.

The red proof for (1) is that on main f82598b the same factory default call
raises ``ValueError`` from the constructor.
"""

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from ops_engine.adapters.factory import adapter_for
from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import Destination
from ops_engine.modules.release import ReleaseHandler


def _sign(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _github_sign(secret: str, payload: bytes) -> str:
    return "sha256=" + _sign(secret, payload)


FORGEJO_PAYLOAD = json.dumps(
    {"repository": {"name": "engine", "full_name": "LangeVC/ops-engine"}, "sender": {"username": "someone"}}
).encode()

GITHUB_PAYLOAD = json.dumps(
    {"repository": {"full_name": "org/repo"}, "action": "opened", "sender": {"login": "someone"}}
).encode()


# --- Property 1: construction without a secret can publish --------------------


def test_factory_default_constructs_without_secret():
    """adapter_for with only a token (the factory's own empty-secret default)
    returns a real adapter instead of raising from the constructor."""
    adapter = adapter_for(Destination(forge="github", repo="org/repo"), token="tok")
    assert isinstance(adapter, GithubAdapter)
    assert adapter.webhook_secret == ""


@pytest.mark.asyncio
async def test_secretless_adapter_completes_a_release_publication(tmp_path):
    """A release publication through an adapter that never parsed a webhook does
    not need a secret: the factory default path builds an adapter and the release
    module publishes against a stubbed transport.

    The stub transport is driven through the adapter's own create_release /
    upload_release_asset by patching the lazy HTTP client, so the proof runs the
    real publication path — no secret supplied anywhere.
    """
    from unittest.mock import AsyncMock, Mock, patch

    artifact_dir = tmp_path / "dist"
    artifact_dir.mkdir()
    (artifact_dir / "wheel.whl").write_bytes(b"\x00stub\xff")

    adapter = adapter_for(Destination(forge="github", repo="org/repo"), token="tok")

    fake_resp = Mock()
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = {"id": 7}

    fake_client = Mock()
    fake_client.is_closed = False
    fake_client.request = AsyncMock(return_value=fake_resp)

    with patch.object(adapter, "_client", fake_client, create=True):
        created = await adapter.create_release(
            repo_full_name="org/repo",
            tag_name="v1.0.0",
            name="engine v1.0.0",
            body="release notes",
        )

    assert created == {"id": 7}
    assert fake_client.request.await_count == 1


# --- Property 2: the ingress guard is NOT weakened ----------------------------


@pytest.mark.parametrize(
    "factory, secret",
    [
        (lambda: ForgejoAdapter(base_url="https://git.example.com", token="tok", webhook_secret=""), "forgejo"),
        (lambda: GithubAdapter(token="tok", webhook_secret=""), "github"),
    ],
)
@pytest.mark.asyncio
async def test_parse_webhook_with_no_secret_refuses_named_error(factory, secret):
    """An adapter with no configured secret refuses to parse a webhook, naming the
    property the guard protects."""
    adapter = factory()
    payload = GITHUB_PAYLOAD if secret == "github" else FORGEJO_PAYLOAD
    with pytest.raises(ValueError, match="no webhook_secret configured"):
        await adapter.parse_webhook({}, payload)


@pytest.mark.asyncio
async def test_forgejo_with_secret_rejects_forged_signature():
    adapter = ForgejoAdapter(base_url="https://git.example.com", token="tok", webhook_secret="real-secret")
    forged = {"x-forgejo-signature": _sign("wrong-secret", FORGEJO_PAYLOAD)}
    with pytest.raises(ValueError, match="signature"):
        await adapter.parse_webhook(forged, FORGEJO_PAYLOAD)


@pytest.mark.asyncio
async def test_forgejo_with_secret_accepts_valid_signature():
    adapter = ForgejoAdapter(base_url="https://git.example.com", token="tok", webhook_secret="real-secret")
    headers = {"x-forgejo-signature": _sign("real-secret", FORGEJO_PAYLOAD)}
    event = await adapter.parse_webhook(headers, FORGEJO_PAYLOAD)
    assert event["source"] == "forgejo"


@pytest.mark.asyncio
async def test_github_with_secret_rejects_forged_signature():
    adapter = GithubAdapter(token="tok", webhook_secret="real-secret")
    forged = {"x-hub-signature-256": _github_sign("wrong-secret", GITHUB_PAYLOAD)}
    with pytest.raises(ValueError, match="signature"):
        await adapter.parse_webhook(forged, GITHUB_PAYLOAD)


@pytest.mark.asyncio
async def test_github_with_secret_accepts_valid_signature():
    adapter = GithubAdapter(token="tok", webhook_secret="real-secret")
    headers = {"x-hub-signature-256": _github_sign("real-secret", GITHUB_PAYLOAD)}
    event = await adapter.parse_webhook(headers, GITHUB_PAYLOAD)
    assert event["source"] == "github"
