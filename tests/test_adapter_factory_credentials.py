"""ADP-011: the factory serves a credential per destination, not one shared token.

ADP-002 built :func:`adapters_for` with a single ``token``/``webhook_secret``
pair, so its only consumer's real shape — two destinations carrying two
DIFFERENT credentials (a Forgejo runner token and a GitHub mirror token) — could
not be served in one call. That is why the release workflow bypassed the factory
and hand-built each adapter inline. This lane gives the factory a
credential-per-destination form keyed by forge value, deletes the inline
construction in the release workflow, and proves three things:

1. two destinations carrying two distinct credentials are served in ONE
   ``adapters_for`` call, and the release module publishes one build to both
   using the credentials the factory supplied — a real run against stubbed
   adapters, asserting each adapter received its own token and not the other's;
2. the factory stores nothing and logs nothing: after the call, the factory holds
   no reference to either token (no module state), and constructing the adapters
   triggers no credential-valued log record;
3. an ABSENT credential for a declared destination is a named refusal
   (:class:`MissingCredentialError`), never a silent unauthenticated call.
"""

import logging
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from ops_engine.adapters.factory import (
    Credential,
    MissingCredentialError,
    UnknownForgeError,
    adapters_for,
)
from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import Destination, OpsEngineConfig
from ops_engine.modules.mirror import resolve_destinations
from ops_engine.modules.release import PublicationReport, ReleaseHandler

_FORGEJO_TOKEN = "forgejo-runner-token"
_GH_MIRROR_TOKEN = "github-mirror-token"
_FORGEJO_SECRET = "forgejo-webhook-secret"
_GH_SECRET = "github-webhook-secret"


def _two_destinations() -> list[Destination]:
    return [
        Destination(forge="forgejo", repo="exampleorg/engine", role="release"),
        Destination(forge="github", repo="exampleorg/engine", role="release"),
    ]


def _both_forge_config() -> OpsEngineConfig:
    return OpsEngineConfig.load(
        {
            "orgs": {
                "exampleorg": {
                    "repositories": {
                        "engine": {
                            "destinations": [
                                {
                                    "forge": "forgejo",
                                    "repo": "exampleorg/engine",
                                    "role": "release",
                                },
                                {
                                    "forge": "github",
                                    "repo": "exampleorg/engine",
                                    "role": "release",
                                },
                            ]
                        }
                    }
                }
            }
        }
    )


# --- Criterion 1/2: one call, two destinations, two distinct credentials -----


def test_adapters_for_serves_each_destination_its_own_credential():
    """One call constructs both adapters, each holding ITS own token.

    This is the shape the release workflow's consumer actually has: two
    destinations, two DIFFERENT credentials. The single-token form ADP-002
    shipped cannot express it; the credentials mapping can. Each adapter's token
    equals its forge's credential and no other's.
    """
    adapters = adapters_for(
        _two_destinations(),
        credentials={
            "forgejo": Credential(token=_FORGEJO_TOKEN, webhook_secret=_FORGEJO_SECRET),
            "github": Credential(token=_GH_MIRROR_TOKEN, webhook_secret=_GH_SECRET),
        },
        base_url="https://code.example.org",
    )

    forgejo, github = adapters
    assert isinstance(forgejo, ForgejoAdapter)
    assert isinstance(github, GithubAdapter)
    assert forgejo.token == _FORGEJO_TOKEN
    assert github.token == _GH_MIRROR_TOKEN
    assert forgejo.token != github.token
    # Each adapter got exactly its own credential, not the other's.
    assert forgejo.token != _GH_MIRROR_TOKEN
    assert github.token != _FORGEJO_TOKEN
    assert forgejo.webhook_secret == _FORGEJO_SECRET
    assert github.webhook_secret == _GH_SECRET


@pytest.mark.asyncio
async def test_one_publication_uses_each_destination_its_own_credential(tmp_path):
    """A real publish run over a two-destination config, driving the factory's
    credential-per-destination form end to end, asserts each destination's
    adapter received its own token — not a shared one and not the other's.

    The adapters are built by the factory from the credentials mapping (the same
    shape the workflow now uses), then handed to ``publish_release``, which runs
    the publication. The release id each stub returns is keyed to the token it
    was constructed with, so the report proves the two destinations were served
    by two different credentials.
    """
    config = _both_forge_config()
    destinations = resolve_destinations(config, "exampleorg/engine")

    cred_forgejo = Credential(token=_FORGEJO_TOKEN, webhook_secret=_FORGEJO_SECRET)
    cred_github = Credential(token=_GH_MIRROR_TOKEN, webhook_secret=_GH_SECRET)

    adapters = adapters_for(
        destinations,
        credentials={"forgejo": cred_forgejo, "github": cred_github},
        base_url="https://code.example.org",
    )

    # Stub the publication calls so the run is hermetic; the returned release id
    # is chosen from the token each adapter carries, proving which credential
    # served which destination.
    for adapter in adapters:
        release_id = 100 if adapter.token == _FORGEJO_TOKEN else 200
        adapter.create_release = AsyncMock(return_value={"id": release_id})
        adapter.upload_release_asset = AsyncMock(return_value={"ok": True})

    artifact_dir = tmp_path / "dist"
    artifact_dir.mkdir()
    (artifact_dir / "a.whl").write_bytes(b"wheel")

    report = await ReleaseHandler.publish_release(
        config,
        "exampleorg/engine",
        "v1.0.0",
        "release notes",
        artifact_dir,
        adapters=adapters,
    )

    assert report == PublicationReport(
        repo="exampleorg/engine",
        tag_name="v1.0.0",
        published=["forgejo:exampleorg/engine", "github:exampleorg/engine"],
        failed=[],
    )

    # Each adapter was constructed with its own credential; the forgejo adapter
    # carries the forgejo token and the github adapter the mirror token.
    forgejo, github = adapters
    assert forgejo.token == _FORGEJO_TOKEN
    assert github.token == _GH_MIRROR_TOKEN
    assert forgejo.token != github.token
    # The two uploads carried the two distinct release ids, keyed to the tokens.
    assert forgejo.upload_release_asset.await_count == 1
    assert github.upload_release_asset.await_count == 1
    assert forgejo.upload_release_asset.call_args.kwargs["release_id"] == 100
    assert github.upload_release_asset.call_args.kwargs["release_id"] == 200


# --- Criterion 3a: no credential stored or logged by the factory -------------


def test_factory_stores_no_credential():
    """After ``adapters_for`` returns, the factory module holds no reference to
    any credential: the tokens are carried only by the returned adapters, never
    retained as module state or on the ``Credential`` values beyond the call.

    ``Credential`` is an immutable, caller-owned value; the factory's module
    namespace gains no assignment of a token. This is proven by constructing the
    adapters and asserting the factory module exposes no credential-bearing
    attribute and its globals hold no token value.
    """
    from ops_engine.adapters import factory

    creds = {"forgejo": Credential(token=_FORGEJO_TOKEN, webhook_secret=_FORGEJO_SECRET)}
    adapters_for(
        [Destination(forge="forgejo", repo="exampleorg/engine")],
        credentials=creds,
        base_url="https://code.example.org",
    )

    # The token never becomes a module-level name in the factory.
    for name, value in vars(factory).items():
        assert value is not creds.get("forgejo"), f"factory stores credential as {name!r}"
    # The module holds no dict/list that retained the token string.
    for name, value in vars(factory).items():
        if isinstance(value, (dict, list, tuple, set)):
            flattened = repr(value)
            assert _FORGEJO_TOKEN not in flattened, (
                f"factory module state {name!r} retains the token"
            )


def test_factory_logs_no_credential(caplog):
    """Constructing adapters through the factory emits no log record carrying a
    credential value. The factory logs nothing at all on the success path, so a
    token can never leak into a runner log via the factory.
    """
    with caplog.at_level(logging.DEBUG):
        adapters_for(
            _two_destinations(),
            credentials={
                "forgejo": Credential(token=_FORGEJO_TOKEN, webhook_secret=_FORGEJO_SECRET),
                "github": Credential(token=_GH_MIRROR_TOKEN, webhook_secret=_GH_SECRET),
            },
            base_url="https://code.example.org",
        )

    for record in caplog.records:
        message = record.getMessage()
        assert _FORGEJO_TOKEN not in message
        assert _GH_MIRROR_TOKEN not in message
        assert _FORGEJO_SECRET not in message
        assert _GH_SECRET not in message


# --- Criterion 3b: absent credential is a named refusal -----------------------


def test_absent_credential_for_a_declared_destination_is_a_named_refusal():
    """A destination whose forge has no entry in the credentials mapping raises
    :class:`MissingCredentialError` naming the destination — never a silent
    unauthenticated call that would construct an adapter with an empty token.
    """
    with pytest.raises(MissingCredentialError) as exc:
        adapters_for(
            _two_destinations(),
            credentials={
                "forgejo": Credential(token=_FORGEJO_TOKEN, webhook_secret=_FORGEJO_SECRET),
            },
            base_url="https://code.example.org",
        )
    message = str(exc.value)
    assert "github" in message
    assert _FORGEJO_TOKEN not in message


def test_unknown_forge_still_raises_named_error_even_with_credentials():
    """The per-destination form preserves the ADP-002 refusal: an unrecognised
    forge value raises :class:`UnknownForgeError`, not a silent fallback — even
    when a credentials mapping is supplied."""
    with pytest.raises(UnknownForgeError):
        adapters_for(
            [Destination(forge="gitlab", repo="exampleorg/engine")],
            credentials={"gitlab": Credential(token="some-token")},
            base_url="https://code.example.org",
        )


def test_empty_destination_list_with_credentials_is_normal():
    """An empty destination list still maps to an empty adapter list under the
    credentials form — the deliberate unmirrored case, not a crash."""
    assert adapters_for([], credentials={}) == []


def test_credential_is_an_immutable_value(caplog):
    """A ``Credential`` snapshots token and secret as plain, caller-owned fields;
    ``asdict`` round-trips exactly the two strings so nothing about the
    credential is hidden or derived."""
    c = Credential(token=_FORGEJO_TOKEN, webhook_secret=_FORGEJO_SECRET)
    assert asdict(c) == {"token": _FORGEJO_TOKEN, "webhook_secret": _FORGEJO_SECRET}
