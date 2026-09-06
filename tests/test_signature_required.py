"""LNF-050-1 (LVC-235) — the webhook endpoints refuse what they cannot verify.

Both ops-engine adapters (Forgejo and GitHub) used to accept a payload whenever
a signature header OR the configured secret was missing::

    if not sig_header or not self.webhook_secret:
        return          # accepts silently

Two ways past the gate, both fixed in both adapters:

1. A request without a signature header is refused — absence of proof is not
   proof, even when the secret is correctly configured.
2. A service with no configured secret refuses to verify at the ingress rather
   than accepting everything (ADP-006: construction still succeeds so that
   publication, which never parses a webhook, needs no secret).

And when the secret IS configured and a signature IS present, the correct
signature must still be accepted and a wrong one refused.

The green proofs for the network binding live here too: the layover template
must not publish a host port (so the ingress is reachable only from the Docker
network, not 0.0.0.0) and the container must not bind the wildcard address.
"""

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter


SECRET = "a-very-secret-token"

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "examples" / "layover-template"


def _payload() -> bytes:
    return json.dumps({
        "repository": {
            "owner": {"lower_name": "fusionaize"},
            "name": "faigrid",
            "full_name": "fusionaize/faigrid",
        },
        "action": "opened",
        "sender": {"username": "someone"},
    }).encode()


def _github_payload() -> bytes:
    return json.dumps({
        "repository": {"full_name": "org/repo"},
        "action": "opened",
        "sender": {"login": "someone"},
    }).encode()


def _sign(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _github_sign(secret: str, payload: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _forgejo_adapter(secret: str) -> ForgejoAdapter:
    return ForgejoAdapter(
        base_url="https://git.example.com", token="test-token", webhook_secret=secret,
    )


def _github_adapter(secret: str) -> GithubAdapter:
    return GithubAdapter(token="test-token", webhook_secret=secret)


# --- Forgejo: refusal proofs -------------------------------------------------


@pytest.mark.asyncio
async def test_unsigned_payload_is_refused_when_secret_set():
    """A request carrying no signature header must be refused, not accepted."""
    adapter = _forgejo_adapter(SECRET)
    payload = _payload()

    with pytest.raises(ValueError, match="signature"):
        await adapter.parse_webhook({}, payload)


@pytest.mark.asyncio
async def test_correctly_signed_payload_is_accepted():
    """A correctly signed payload with the configured secret is accepted."""
    adapter = _forgejo_adapter(SECRET)
    payload = _payload()
    headers = {"x-forgejo-signature": _sign(SECRET, payload)}

    event = await adapter.parse_webhook(headers, payload)

    assert event["source"] == "forgejo"
    assert event["org"] == "fusionaize"


@pytest.mark.asyncio
async def test_wrongly_signed_payload_is_refused():
    """A payload signed with a different secret must be refused."""
    adapter = _forgejo_adapter(SECRET)
    payload = _payload()
    headers = {"x-forgejo-signature": _sign("wrong-secret", payload)}

    with pytest.raises(ValueError, match="signature"):
        await adapter.parse_webhook(headers, payload)


@pytest.mark.asyncio
async def test_no_configured_secret_refuses_at_ingress_not_at_start():
    """A service with no configured secret may start (to publish, which never
    parses a webhook), but refuses to parse a webhook rather than accepting
    everything (ADP-006 moved the guard from the constructor to the ingress)."""
    adapter = _forgejo_adapter("")
    with pytest.raises(ValueError, match="secret"):
        await adapter.parse_webhook({"x-forgejo-signature": "deadbeef"}, _payload())


# --- GitHub: the second adapter, the same gate ------------------------------


@pytest.mark.asyncio
async def test_github_unsigned_payload_is_refused_when_secret_set():
    """The GitHub ingress refuses a payload without a signature header."""
    adapter = _github_adapter(SECRET)
    payload = _github_payload()

    with pytest.raises(ValueError, match="signature"):
        await adapter.parse_webhook({}, payload)


@pytest.mark.asyncio
async def test_github_correctly_signed_payload_is_accepted():
    """A correctly signed GitHub payload is accepted."""
    adapter = _github_adapter(SECRET)
    payload = _github_payload()
    headers = {"x-hub-signature-256": _github_sign(SECRET, payload)}

    event = await adapter.parse_webhook(headers, payload)

    assert event["source"] == "github"
    assert event["repo"] == "org/repo"


@pytest.mark.asyncio
async def test_github_wrongly_signed_payload_is_refused():
    """A GitHub payload signed with a different secret is refused."""
    adapter = _github_adapter(SECRET)
    payload = _github_payload()
    headers = {"x-hub-signature-256": _github_sign("wrong-secret", payload)}

    with pytest.raises(ValueError, match="signature"):
        await adapter.parse_webhook(headers, payload)


@pytest.mark.asyncio
async def test_github_no_configured_secret_refuses_at_ingress_not_at_start():
    """A GitHub service with no configured secret may start but refuses to parse
    a webhook (ADP-006 moved the guard to the ingress)."""
    adapter = _github_adapter("")
    with pytest.raises(ValueError, match="secret"):
        await adapter.parse_webhook({"x-hub-signature-256": "sha256=deadbeef"}, _github_payload())


# --- Network binding: reachable only from the Docker network ----------------


def test_template_does_not_publish_a_host_port():
    """The layover template must not publish a host port: that is what puts the
    ingress on 0.0.0.0. 'expose' documents the port on the Docker network
    without binding the host."""
    compose = (TEMPLATE_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "ports:" not in compose, (
        "the template publishes a host port; the ingress must be reachable "
        "only from the Docker network, not 0.0.0.0"
    )


def test_template_binds_container_internal_address():
    """The container must not bind the wildcard address, even though no host
    port is published. 0.0.0.0 inside plus a published port is the leak."""
    dockerfile = (TEMPLATE_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--host 0.0.0.0" not in dockerfile, (
        "the container binds 0.0.0.0; the ingress must not be reachable from "
        "the host"
    )
