"""ADP-002: Adapter factory — a destination entry becomes the matching adapter.

The factory is the second half of the Layer-1 destinations seam DST-003 opened:
``resolve_destinations`` turns a config object into a ``Destination`` list; this
module turns one ``Destination`` into a constructed ``ForgeAdapter``. It holds a
forge-name-to-adapter mapping and nothing else. No organisation name is held and
no network call is made: credentials and, for Forgejo, the forge ``base_url``
arrive at the call site. An unrecognised forge value is a named refusal, never a
silent fallback to GitHub.
"""

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
) -> list[ForgeAdapter]:
    """Map a destination list to the matching adapter list.

    An empty destination list is the normal, deliberate "unmirrored" case and
    maps to an empty adapter list — never a crash. Each entry is mapped through
    :func:`adapter_for`, so an entry carrying an unrecognised forge value raises
    :class:`UnknownForgeError` exactly as the single-entry form does.
    """
    return [
        adapter_for(
            destination,
            token=token,
            webhook_secret=webhook_secret,
            base_url=base_url,
        )
        for destination in destinations
    ]
