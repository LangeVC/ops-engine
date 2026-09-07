"""ADP-002 / ADP-011: Adapter factory — a destination entry becomes the matching adapter.

The factory is the second half of the Layer-1 destinations seam DST-003 opened:
``resolve_destinations`` turns a config object into a ``Destination`` list; this
module turns one ``Destination`` into a constructed ``ForgeAdapter``. It holds a
forge-name-to-adapter mapping and nothing else. No organisation name is held and
no network call is made: credentials and, for Forgejo, the forge ``base_url``
arrive at the call site. An unrecognised forge value is a named refusal, never a
silent fallback to GitHub.

ADP-011 adds the credential-per-destination form: ``adapters_for`` can serve a
DIFFERENT credential to each destination, keyed by forge value, so a consumer
with two destinations carrying two distinct credentials (e.g. a Forgejo token
and a GitHub mirror token) no longer has to bypass the factory and hand-build
adapters. A credential is not a destination: the token is caller input, the
factory learns only that a destination's forge HAS a credential in the mapping,
and it stores nothing and logs nothing after the call.
"""

from dataclasses import dataclass

from ops_engine.adapters.base import ForgeAdapter
from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import Destination

_SUPPORTED_FORGES = ("github", "forgejo")


class UnknownForgeError(ValueError):
    """A destination named a forge no adapter in this package can serve.

    The message names the offending value and the supported set, so an operator
    can correct the config rather than silently receiving a GitHub adapter for a
    forge that is not GitHub. It is a refusal, never a fallback.
    """


class MissingCredentialError(ValueError):
    """A declared destination's forge has no credential in the supplied mapping.

    Raised by :func:`adapters_for` (credential-per-destination form) when a
    destination's ``forge`` value is absent from the ``credentials`` mapping. It
    is a refusal, never a silent unauthenticated call: a destination that would
    otherwise construct an adapter with an empty token must instead be named so
    the operator corrects the credential map rather than publishing unauthenticated.
    """


@dataclass(frozen=True)
class Credential:
    """A credential for one forge: a token and an optional webhook secret.

    The token authenticates a destination's publication; the webhook secret
    satisfies the ingress contract even though publication never parses a
    webhook (ADP-006). Both are caller input. The factory forwards them to the
    matching adapter and retains nothing: a :class:`Credential` is a value passed
    into a call, not state the factory stores or logs.
    """

    token: str = ""
    webhook_secret: str = ""


def adapter_for(
    destination: Destination,
    *,
    token: str = "",
    webhook_secret: str = "",
    base_url: str = "",
) -> ForgeAdapter:
    """Construct the adapter a destination's ``forge`` value names.

    The forge is a VALUE, not a key name (DST-001): the mapping below is the one
    place the supported forge set is declared, and it holds no organisation
    knowledge. ``token`` and ``webhook_secret`` are credentials supplied by the
    caller; ``base_url`` is the Forgejo instance address and is likewise caller
    input (and unused for GitHub, whose host is the adapter's own constant).
    Nothing here fetches, discovers, or hardcodes any host or organisation.

    Raises:
        UnknownForgeError: ``destination.forge`` is not one of the supported
            values. The message names the value and the supported set.
    """
    forge = destination.forge.strip().lower()
    if forge == "github":
        return GithubAdapter(token=token, webhook_secret=webhook_secret)
    if forge == "forgejo":
        return ForgejoAdapter(
            base_url=base_url, token=token, webhook_secret=webhook_secret
        )
    raise UnknownForgeError(
        f"unrecognised forge {destination.forge!r}; supported forges are: "
        f"{', '.join(_SUPPORTED_FORGES)}"
    )


def adapters_for(
    destinations: list[Destination],
    *,
    token: str = "",
    webhook_secret: str = "",
    base_url: str = "",
    credentials: dict[str, Credential] | None = None,
) -> list[ForgeAdapter]:
    """Map a destination list to the matching adapter list.

    Two credential shapes are accepted:

    * **Per-destination** (``credentials``): a mapping from forge value
      (``"github"`` / ``"forgejo"``) to a :class:`Credential`. Each destination
      is served only its own forge's credential, so two destinations carrying
      two distinct tokens are constructed in one call. A destination whose forge
      is absent from the mapping raises :class:`MissingCredentialError` — a
      named refusal, never a silent unauthenticated call.
    * **Shared** (the legacy ``token``/``webhook_secret`` keywords, used when
      ``credentials`` is omitted): every destination receives the same
      credential.

    An empty destination list is the normal, deliberate "unmirrored" case and
    maps to an empty adapter list — never a crash. Each entry is mapped through
    :func:`adapter_for`, so an entry carrying an unrecognised forge value raises
    :class:`UnknownForgeError` exactly as the single-entry form does, whichever
    credential shape supplies it.
    """
    if credentials is None:
        return [
            adapter_for(
                destination,
                token=token,
                webhook_secret=webhook_secret,
                base_url=base_url,
            )
            for destination in destinations
        ]

    adapters: list[ForgeAdapter] = []
    for destination in destinations:
        forge = destination.forge.strip().lower()
        credential = credentials.get(forge)
        if credential is None:
            raise MissingCredentialError(
                f"destination {destination.forge!r} has no credential; "
                f"supplied credentials cover: {', '.join(sorted(credentials)) or '(none)'}"
            )
        adapters.append(
            adapter_for(
                destination,
                token=credential.token,
                webhook_secret=credential.webhook_secret,
                base_url=base_url,
            )
        )
    return adapters
