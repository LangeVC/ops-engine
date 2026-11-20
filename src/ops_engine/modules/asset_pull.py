"""REA-017: read a GitHub-built release asset with the engine's own credential.

D4 keeps a GitHub-built binary on GitHub unless the repository declares
``release.forgejo_assets: pull``. When it does, the engine downloads the asset
from the GitHub release, checks its SHA-256 against the digest the GitHub API
reports, attaches it to the Forgejo release and re-downloads it from Forgejo
to check the bytes again — Forgejo reports no digest of its own, only a size.

Both collaborators are read-only and injectable, exactly like the REA-016
``HttpxChannelReader``: the shipped implementations take an optional
``httpx.AsyncClient`` so a test drives them through a mock transport, and
neither writes on the source forge. The free-space reading comes from a
declared HTTP source (never ssh) and answers ``None`` when no source is
declared, so an undeterminable reading never fails a release.
"""

from __future__ import annotations

from typing import Optional

import httpx


class HttpxAssetDownloader:
    """Download one release asset by its API ``url``.

    The request carries the source adapter's own credential, so the engine
    reads GitHub with its GitHub token and nothing else. ``GET`` only: the
    source forge is never written. Redirects are followed (GitHub serves the
    download itself through one).
    """

    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        self._client = client

    async def download(self, adapter, repo: str, asset: dict) -> bytes:
        url = asset["url"]
        headers = {
            "Authorization": f"token {getattr(adapter, 'token', '')}",
            "Accept": "application/octet-stream",
        }
        if self._client is not None:
            response = await self._client.get(url, headers=headers)
        else:
            async with httpx.AsyncClient(
                timeout=120.0, follow_redirects=True
            ) as client:
                response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.content


class FreeSpaceUnavailable(Exception):
    """No reading could be made; the message names the reason.

    The caller turns this into the report's ``unavailable: <reason>`` string.
    It is evidence, not a gate: a release never fails because a host reading
    could not be taken.
    """


class HttpxFreeSpaceReader:
    """Read a forge host's free space from a declared HTTP source.

    The source is a URL the operator declares (an exporter or the host's own
    endpoint), never ssh and never a credential: the read is unauthenticated so
    the engine adds no secret to a host endpoint. The response is plain-text
    integer bytes or JSON carrying ``free_bytes``, ``free`` or
    ``available_bytes``. The read has a timeout.

    An undeclared source and a failed read raise :class:`FreeSpaceUnavailable`
    whose message names the reason, so the report can say why the reading is
    missing instead of leaving a silent hole.
    """

    def __init__(
        self,
        client: Optional[httpx.AsyncClient] = None,
        source_url: str = "",
    ) -> None:
        self._client = client
        self._source_url = source_url

    async def free_bytes(self, destination) -> int:
        if not self._source_url:
            raise FreeSpaceUnavailable("no source declared")
        try:
            if self._client is not None:
                response = await self._client.get(self._source_url)
            else:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(self._source_url)
        except httpx.HTTPError as exc:
            raise FreeSpaceUnavailable(f"read failed: {exc}") from exc
        if response.status_code != 200:
            raise FreeSpaceUnavailable(f"read failed: HTTP {response.status_code}")
        return _free_bytes_from(response)


def _free_bytes_from(response: httpx.Response) -> int:
    """The byte count in a free-space answer: plain-text or JSON."""
    body = response.text.strip()
    # A declared endpoint may answer the bare integer; an exporter usually
    # answers JSON. Both are read here, and anything else is a named miss.
    try:
        return int(body)
    except ValueError:
        pass
    try:
        payload = response.json()
    except ValueError as exc:
        raise FreeSpaceUnavailable("unreadable answer: not an integer or JSON") from exc
    for key in ("free_bytes", "free", "available_bytes"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, int):
            return value
    raise FreeSpaceUnavailable("unreadable answer: no free-byte field")
