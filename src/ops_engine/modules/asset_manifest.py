"""REA-016: inspect the release manifest's pieces against their deadline.

The manifest is the additive, optional extension of the REA-010
``release.assets`` declaration (D4/D5): every asset and every channel names its
producer (``forgejo`` or ``github``) and the destinations that must hold it.

A github-produced piece is not an error while it is still on its way: it is
pending until the manifest's deadline (60 minutes unless the repository
overrides it), and only then is the run red. A forgejo-produced piece is
required at once — the Forgejo build either carries it or it does not.

The channels (PyPI, ghcr, Homebrew tap, winget) are read read-only, the way
capacium's ``verify-channels`` step reads them: the reader answers presence
and nothing else, and an unprobeable channel (winget) is reported as manual,
never as a failure.

This module carries no publishing code. It judges what the release path
already wrote and read back.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from ops_engine.config_loader import AssetManifest

# The release object's own creation time is the clock the pending deadline runs
# from. Destinations render it as ISO-8601, GitHub with a trailing ``Z``.
def parse_timestamp(value: object) -> Optional[datetime]:
    """Parse a destination's ISO-8601 timestamp, or return None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def version_of(tag_name: str) -> str:
    """The version form of a tag: ``v3.5.1`` -> ``3.5.1``."""
    return tag_name[1:] if tag_name.startswith("v") else tag_name


def render_name(name: str, tag_name: str) -> str:
    """Render the ``<version>``/``<tag>`` placeholders of a declared name."""
    return name.replace("<version>", version_of(tag_name)).replace("<tag>", tag_name)


def deadline_passed(
    since: datetime, deadline_minutes: int, now: Optional[datetime] = None
) -> bool:
    """True when ``since`` is at least ``deadline_minutes`` before ``now``."""
    now = now or datetime.now(timezone.utc)
    return now - since >= timedelta(minutes=deadline_minutes)


def piece_text(name: str, producer: str, since: datetime) -> str:
    """The operator-facing form of one asset piece waiting on its producer."""
    return f"{name} producer={producer} since={since.isoformat()}"


@dataclass(frozen=True)
class Piece:
    """One manifest piece whose producer has not fulfilled it yet.

    ``channel`` is set for a channel piece (PyPI, ghcr, Homebrew tap, winget)
    and ``red`` says whether the deadline has passed. A failed channel piece
    names the channel explicitly, so the message says both what the piece is
    (``pypi:ops-engine``) and which channel form it belongs to.
    """

    name: str
    producer: str
    since: datetime
    channel: Optional[str] = None
    red: bool = False

    def as_text(self) -> str:
        subject = f"{self.channel}:{self.name}" if self.channel else self.name
        text = f"{subject} producer={self.producer} since={self.since.isoformat()}"
        if self.channel and self.red:
            text += f" channel:{self.channel}"
        return text


@dataclass
class ChannelVerdict:
    """The read-only result of judging every declared channel.

    ``satisfied`` and ``manual`` carry the ``<channel>:<name>`` label;
    ``pending`` and ``failed`` carry the :class:`Piece` so the caller can name
    the producer and since when the piece is due.
    """

    satisfied: list[str] = field(default_factory=list)
    pending: list["Piece"] = field(default_factory=list)
    failed: list["Piece"] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)


async def evaluate_channels(
    manifest: AssetManifest,
    tag_name: str,
    *,
    reader: object = None,
    since: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> ChannelVerdict:
    """Judge the manifest's channels read-only against the deadline.

    A channel the reader proves present is satisfied; one it proves absent is
    pending within the deadline and failed after it; one it cannot probe
    (winget, or a read error) is manual — unknown is never a failure.
    """
    reader = reader if reader is not None else HttpxChannelReader()
    version = version_of(tag_name)
    now = now or datetime.now(timezone.utc)
    moment = since if since is not None else now
    verdict = ChannelVerdict()
    for entry in manifest.channels:
        present = await reader.present_at(entry.channel, entry.name, version)
        label = f"{entry.channel}:{entry.name}"
        if present is None:
            verdict.manual.append(label)
            continue
        if present:
            verdict.satisfied.append(label)
            continue
        red = deadline_passed(moment, manifest.deadline_minutes, now)
        piece = Piece(
            name=entry.name,
            producer=entry.producer,
            since=moment,
            channel=entry.channel,
            red=red,
        )
        (verdict.failed if red else verdict.pending).append(piece)
    return verdict


class HttpxChannelReader:
    """Read-only presence probe for the manifest's channels.

    Only GET is ever issued: PyPI answers through its JSON API, ghcr through an
    anonymous pull token followed by a manifest HEAD/GET, and a Homebrew tap is
    probed at its repository. winget has no read-only probe and answers None
    (manual). Any read error is None as well — an unknown answer must not fail
    a release.
    """

    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        self._client = client

    async def present_at(
        self, channel: str, name: str, version: str
    ) -> Optional[bool]:
        if channel == "winget":
            return None
        if self._client is not None:
            return await self._probe(self._client, channel, name, version)
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await self._probe(client, channel, name, version)

    async def _probe(
        self,
        client: httpx.AsyncClient,
        channel: str,
        name: str,
        version: str,
    ) -> Optional[bool]:
        try:
            if channel == "pypi":
                response = await client.get(f"https://pypi.org/pypi/{name}/json")
                if response.status_code == 404:
                    return False
                if response.status_code != 200:
                    return None
                try:
                    return (
                        response.json().get("info", {}).get("version") == version
                    )
                except ValueError:
                    return None
            if channel == "ghcr":
                token_response = await client.get(
                    "https://ghcr.io/token",
                    params={
                        "service": "ghcr.io",
                        "scope": f"repository:{name}:pull",
                    },
                )
                if token_response.status_code != 200:
                    return None
                try:
                    token = token_response.json().get("token", "")
                except ValueError:
                    return None
                response = await client.get(
                    f"https://ghcr.io/v2/{name}/manifests/{version}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": (
                            "application/vnd.oci.image.manifest.v1+json, "
                            "application/vnd.docker.distribution.manifest.v2+json"
                        ),
                    },
                )
                if response.status_code == 404:
                    return False
                if response.status_code != 200:
                    return None
                return True
            if channel == "homebrew":
                response = await client.get(
                    f"https://raw.githubusercontent.com/{name}/HEAD/"
                )
                if response.status_code == 404:
                    return False
                if response.status_code != 200:
                    return None
                return True
        except httpx.HTTPError:
            return None
        return None
