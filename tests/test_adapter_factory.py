"""ADP-002: the adapter factory turns a destination entry into the right adapter.

The factory lives in ``src/ops_engine/adapters/factory.py`` and is the missing
second half of the Layer-1 destinations seam: ``resolve_destinations`` produces
the destination list; ``adapter_for`` / ``adapters_for`` turn it into adapter
instances. The proofs here are threefold:

1. a destination entry's ``forge`` value maps to the concrete adapter class
   (``github`` -> ``GithubAdapter``, ``forgejo`` -> ``ForgejoAdapter``);
2. an unrecognised ``forge`` value raises ``UnknownForgeError`` naming the value
   and the supported set, and an empty destination list is the normal case that
   yields an empty adapter list, not a crash;
3. the factory makes no network call (proven under a socket guard, not by
   reading for a network library) and holds no organisation name (proven by
   reading the module source).
"""

import socket
from pathlib import Path

import pytest

from ops_engine.adapters.factory import (
    UnknownForgeError,
    adapter_for,
    adapters_for,
)
from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import Destination, OpsEngineConfig
from ops_engine.modules.mirror import resolve_destinations

FACTORY_SRC = (
    Path(__file__).resolve().parent.parent / "src" / "ops_engine" / "adapters" / "factory.py"
)

_ADAPTER_SECRET = "test-webhook-secret"


def _both_forge_config() -> OpsEngineConfig:
    """A config fixture carrying one repo with a GitHub and a Forgejo destination."""
    return OpsEngineConfig.load(
        {
            "orgs": {
                "exampleorg": {
                    "repositories": {
                        "engine": {
                            "destinations": [
                                {
                                    "forge": "github",
                                    "repo": "exampleorg/engine",
                                    "role": "mirror",
                                },
                                {
                                    "forge": "forgejo",
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


# --- Criterion 1: forge value -> matching adapter instance -------------------

def test_forge_github_yields_github_adapter():
    github = adapter_for(
        Destination(forge="github", repo="exampleorg/engine"),
        token="tok",
        webhook_secret=_ADAPTER_SECRET,
    )
    assert isinstance(github, GithubAdapter)


def test_forge_forgejo_yields_forgejo_adapter():
    forgejo = adapter_for(
        Destination(forge="forgejo", repo="exampleorg/engine"),
        token="tok",
        webhook_secret=_ADAPTER_SECRET,
        base_url="https://code.example.org",
    )
    assert isinstance(forgejo, ForgejoAdapter)


def test_resolve_destinations_into_adapters_for_both_forges():
    """A run over a config fixture carrying both forges yields both adapters."""
    config = _both_forge_config()
    destinations = resolve_destinations(config, "exampleorg/engine")

    assert {d.forge for d in destinations} == {"github", "forgejo"}

    adapters = adapters_for(
        destinations,
        token="tok",
        webhook_secret=_ADAPTER_SECRET,
        base_url="https://code.example.org",
    )
    assert isinstance(adapters[0], GithubAdapter)
    assert isinstance(adapters[1], ForgejoAdapter)


# --- Criterion 2: named refusal, no fallback; empty list is normal -----------

def test_unrecognised_forge_raises_named_error():
    with pytest.raises(UnknownForgeError) as exc:
        adapter_for(
            Destination(forge="gitlab", repo="exampleorg/engine"),
            token="tok",
            webhook_secret=_ADAPTER_SECRET,
        )
    message = str(exc.value)
    assert "gitlab" in message
    assert "github" in message and "forgejo" in message


def test_empty_destination_list_is_normal_not_a_crash():
    adapters = adapters_for(
        [],
        token="tok",
        webhook_secret=_ADAPTER_SECRET,
    )
    assert adapters == []


def test_empty_config_yields_empty_destination_list_and_empty_adapters():
    config = OpsEngineConfig.load(
        {"orgs": {"exampleorg": {"repositories": {"engine": {}}}}}
    )
    destinations = resolve_destinations(config, "exampleorg/engine")
    assert destinations == []
    assert adapters_for(destinations) == []


# --- Criterion 3: no network call, no organisation name ----------------------

def test_factory_makes_no_network_call(monkeypatch):
    """The factory constructs adapters without touching a socket.

    A guard that makes every socket construction fail is installed, then the
    factory is exercised. Because the adapters build their HTTP client lazily,
    constructing them must succeed — which proves no network call happens at
    construction time, without grepping the source for a network library.
    """

    def _blocked(*args, **kwargs):
        raise AssertionError("a socket was opened during adapter construction")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)

    config = _both_forge_config()
    destinations = resolve_destinations(config, "exampleorg/engine")
    adapters = adapters_for(
        destinations,
        token="tok",
        webhook_secret=_ADAPTER_SECRET,
        base_url="https://code.example.org",
    )
    assert isinstance(adapters[0], GithubAdapter)
    assert isinstance(adapters[1], ForgejoAdapter)


def test_factory_holds_no_organisation_name():
    """The module source holds no organisation name.

    The factory is Layer 1: it knows forge values, not orgs. Reading the source
    and asserting none of the ecosystem's organisation identifiers appears in it
    is the guard that the "no organisation" constraint is met by the code, not
    just promised in prose.
    """
    source = FACTORY_SRC.read_text(encoding="utf-8").lower()
    org_tokens = (
        "langevc",
        "capacium",
        "elementeer",
        "fusionaize",
        "skillweave",
        "typelicious",
        "veeona",
    )
    for token in org_tokens:
        assert token not in source, f"factory source carries organisation name {token!r}"
