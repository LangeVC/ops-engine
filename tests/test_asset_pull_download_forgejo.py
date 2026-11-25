"""OE-209: the shipped downloader reads a Forgejo asset by browser_download_url.

The post-upload read-back of the pull (``_pull_github_assets`` in
``release.py``) downloads the Forgejo asset it just attached through the
shipped ``HttpxAssetDownloader``. That downloader read ``asset['url']`` only,
and a Forgejo asset carries ``browser_download_url`` and no ``url``, so the
read-back raised ``KeyError: 'url'`` and the destination ended failed although
the file was there (measured by the OE-204 live probe: ``upload failed ('url')``).

The red proof below drives the shipped downloader — not a fake — over an httpx
mock transport against a Forgejo-shaped asset and a GitHub-shaped asset.
Against the base commit the Forgejo case raises ``KeyError: 'url'``; with the
fix both return the bytes, each with its adapter's own credential, and a test
of ``_pull_github_assets`` with the shipped downloader and both fake forges
ends green with the piece verified after upload.
"""

import hashlib
import json

import httpx
import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import OpsEngineConfig
from ops_engine.modules.asset_pull import HttpxAssetDownloader
from ops_engine.modules.release import ReleaseHandler

REPO = "capacium/capacium"
TAG = "v3.7.4"
VERSION = "3.7.4"
TEMPLATE = "Ops Engine {tag_name}"
RELEASE_NAME = "Ops Engine v3.7.4"
NOTES = "- A feature.\n\n- A fix.\n"
BODY = f"## Ops Engine {TAG}\n\n{NOTES}\n"
RELEASE_ID = 777

# The build's own files: the Forgejo release carries these either way.
FILES = [
    "capacium-3.7.4-py3-none-any.whl",
    "capacium-3.7.4.tar.gz",
    "SHA256SUMS",
]
CONTENT = {name: f"forgejo built bytes of {name}\n".encode() for name in FILES}

GITHUB_BINARY = "capacium-3.7.4-linux-amd64.tar.gz"
GITHUB_BYTES = b"github built bytes\n"

MANIFEST = {
    "assets": [
        {
            "type": "github_release_tarball",
            "name": "capacium-<version>-linux-amd64.tar.gz",
            "producer": "github",
            "destinations": [f"forgejo:{REPO}", f"github:{REPO}"],
        }
    ]
}

FORGEJO_BROWSER_URL = (
    f"https://forge.invalid/{REPO}/releases/download/{TAG}/{GITHUB_BINARY}"
)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# --- Criterion 1: the shipped downloader, not a fake ----------------------


@pytest.mark.asyncio
async def test_the_shipped_downloader_reads_a_forgejo_asset_by_browser_url():
    """A Forgejo asset carries ``browser_download_url`` and no ``url``.

    The shipped downloader follows the download URL and returns the bytes with
    the Forgejo adapter's own credential. Against the base commit this raises
    ``KeyError: 'url'``.
    """
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(200, content=GITHUB_BYTES)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ForgejoAdapter(
        base_url="https://forge.invalid", token="forgejo-token", webhook_secret="s"
    )
    downloader = HttpxAssetDownloader(client=client)

    asset = {
        "id": 901,
        "name": GITHUB_BINARY,
        "browser_download_url": FORGEJO_BROWSER_URL,
    }
    assert await downloader.download(adapter, REPO, asset) == GITHUB_BYTES
    assert seen == [(FORGEJO_BROWSER_URL, "token forgejo-token")]


@pytest.mark.asyncio
async def test_the_shipped_downloader_resolves_a_relative_browser_url():
    """A relative ``browser_download_url`` resolves against the adapter host.

    The adapter's ``base_url`` is the forge host (no API prefix); the download
    is a browser download, so the relative path joins that host, and the
    credential still travels with it.
    """
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(200, content=GITHUB_BYTES)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ForgejoAdapter(
        base_url="https://forge.invalid", token="forgejo-token", webhook_secret="s"
    )
    downloader = HttpxAssetDownloader(client=client)

    asset = {
        "id": 901,
        "name": GITHUB_BINARY,
        "browser_download_url": (
            f"/{REPO}/releases/download/{TAG}/{GITHUB_BINARY}"
        ),
    }
    assert await downloader.download(adapter, REPO, asset) == GITHUB_BYTES
    assert seen == [(FORGEJO_BROWSER_URL, "token forgejo-token")]


@pytest.mark.asyncio
async def test_the_shipped_downloader_keeps_the_github_api_url_path():
    """A GitHub asset keeps its API ``url`` with the octet-stream header.

    The download carries the GitHub adapter's own credential and the
    ``Accept: application/octet-stream`` header the API asset endpoint wants.
    """
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.method,
                request.url.path,
                request.headers.get("authorization"),
                request.headers.get("accept"),
            )
        )
        return httpx.Response(200, content=GITHUB_BYTES)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GithubAdapter(token="github-token", webhook_secret="s")
    downloader = HttpxAssetDownloader(client=client)

    asset = {
        "id": 901,
        "name": GITHUB_BINARY,
        "url": f"https://api.github.invalid/repos/{REPO}/releases/assets/901",
    }
    assert await downloader.download(adapter, REPO, asset) == GITHUB_BYTES
    assert seen == [
        (
            "GET",
            f"/repos/{REPO}/releases/assets/901",
            "token github-token",
            "application/octet-stream",
        )
    ]


# --- Criterion 2: redirect credential guard, missing-URL named failure -----


@pytest.mark.asyncio
async def test_a_redirect_to_another_host_does_not_carry_the_credential():
    """A redirect to a different host drops the adapter's credential.

    The shipped downloader follows the redirect (the engine reads GitHub
    through one), but the credential never travels to the other host.
    """
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "first.invalid":
            return httpx.Response(
                302, headers={"location": "https://second.invalid/download"}
            )
        return httpx.Response(200, content=GITHUB_BYTES)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GithubAdapter(token="github-token", webhook_secret="s")
    downloader = HttpxAssetDownloader(client=client)

    asset = {
        "id": 901,
        "name": GITHUB_BINARY,
        "url": "https://first.invalid/repos/releases/assets/901",
    }
    assert await downloader.download(adapter, REPO, asset) == GITHUB_BYTES
    assert seen == [
        ("first.invalid", "token github-token"),
        ("second.invalid", None),
    ]


@pytest.mark.asyncio
async def test_a_missing_url_and_browser_url_fails_naming_the_asset():
    """An asset with neither URL fails with a message naming the asset."""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x"))
    )
    adapter = ForgejoAdapter(
        base_url="https://forge.invalid", token="t", webhook_secret="s"
    )
    downloader = HttpxAssetDownloader(client=client)

    asset = {"id": 901, "name": GITHUB_BINARY}
    with pytest.raises(ValueError) as exc:
        await downloader.download(adapter, REPO, asset)
    assert GITHUB_BINARY in str(exc.value)


# --- Criterion 1: the pull, with the shipped downloader and both forges ----


class FakeDestination:
    """One forge's release endpoint, addressed by the path it was asked for.

    The GitHub side holds the piece with its API ``url`` and a digest; the
    Forgejo side reports its assets with ``browser_download_url`` and no
    ``url``, the way a real Forgejo asset does.
    """

    def __init__(self, *, forge: str, repo: str, preloaded=None, create_status=409):
        self.forge = forge
        self.repo = repo
        self.create_status = create_status
        self.asset_bytes: dict[str, bytes] = {}
        # GitHub's download URL ends in the asset id, not the name; the shipped
        # downloader reads that URL, so the transport must answer by id too.
        self.asset_id_bytes: dict[str, bytes] = {}
        self.next_id = 900
        self.calls: list[tuple[str, str]] = []
        self.requested_repos: list[str] = []
        self.uploaded: list[str] = []
        self.release = None
        if preloaded is not None:
            assets = []
            for name, data in preloaded.items():
                self.asset_bytes[name] = data
                assets.append(self._asset(name, data))
            self.release = {
                "id": RELEASE_ID,
                "tag_name": TAG,
                "name": RELEASE_NAME,
                "body": BODY,
                "draft": False,
                "prerelease": False,
                "created_at": None,
                "assets": assets,
            }

    def _asset(self, name: str, data: bytes) -> dict:
        self.next_id += 1
        self.asset_id_bytes[str(self.next_id)] = data
        if self.forge == "github":
            asset = {
                "id": self.next_id,
                "name": name,
                "size": len(data),
                "url": (
                    f"https://api.github.invalid/repos/{self.repo}"
                    f"/releases/assets/{self.next_id}"
                ),
                "digest": _sha256(data),
            }
        else:
            asset = {
                "id": self.next_id,
                "name": name,
                "size": len(data),
                "browser_download_url": (
                    f"https://forge.invalid/{self.repo}/releases/download/"
                    f"{TAG}/{name}"
                ),
                "digest": None,
            }
        return asset

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        parts = path.strip("/").split("/")
        asked = ""
        if "repos" in parts:
            index = parts.index("repos")
            asked = "/".join(parts[index + 1:index + 3])
        self.requested_repos.append(asked)
        if asked != self.repo:
            return httpx.Response(
                404, json={"message": f"no repository {asked!r} on {self.forge}"}
            )
        if request.method == "GET" and "/releases/tags/" in path:
            if self.release is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=self.release)
        if request.method == "GET" and path.endswith("/releases"):
            return httpx.Response(
                200, json=[] if self.release is None else [self.release]
            )
        if (
            request.method == "GET"
            and path.endswith(f"/releases/{RELEASE_ID}/assets")
        ):
            assets = list((self.release or {}).get("assets") or [])
            page = request.url.params.get("page", "1")
            return httpx.Response(200, json=assets if page == "1" else [])
        if request.method == "PATCH" and path.endswith(f"/releases/{RELEASE_ID}"):
            self.release = {**(self.release or {}), **json.loads(request.content)}
            return httpx.Response(200, json=self.release)
        if request.method == "POST" and path.endswith(
            f"/releases/{RELEASE_ID}/assets"
        ):
            name = request.url.params.get("name")
            data = request.content
            self.uploaded.append(name)
            asset = self._asset(name, data)
            self.asset_bytes[name] = data
            self.release = {
                **(self.release or {}),
                "assets": [*((self.release or {}).get("assets") or []), asset],
            }
            return httpx.Response(201, json=asset)
        if request.method == "POST" and path.endswith("/releases"):
            if self.release is not None:
                return httpx.Response(self.create_status, json={"message": "exists"})
            body = json.loads(request.content)
            self.release = {
                "id": RELEASE_ID,
                "tag_name": TAG,
                "name": body.get("name"),
                "body": body.get("body") or "",
                "draft": body.get("draft", False),
                "prerelease": body.get("prerelease", False),
                "created_at": None,
                "assets": [],
            }
            return httpx.Response(201, json=self.release)
        return httpx.Response(
            500, json={"message": f"unexpected {request.method} {path}"}
        )

    def asset_names(self) -> list[str]:
        return [asset["name"] for asset in (self.release or {}).get("assets") or []]


class DownloadTransport:
    """Serve the download URLs of both forges to the shipped downloader.

    The GitHub asset's API ``url`` and the Forgejo asset's
    ``browser_download_url`` answer the same bytes; each records the
    credential it received, so the test can prove the GitHub read used the
    GitHub token and the Forgejo read used the Forgejo token.
    """

    def __init__(self, forgejo: FakeDestination, github: FakeDestination):
        self.forgejo = forgejo
        self.github = github
        self.downloads: list[tuple[str, str | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.downloads.append(
            (request.url.host, request.headers.get("authorization"))
        )
        if request.url.host == "api.github.invalid":
            asset_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, content=self.github.asset_id_bytes[asset_id])
        if request.url.host == "forge.invalid":
            name = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, content=self.forgejo.asset_bytes[name])
        return httpx.Response(404, json={"message": "unexpected host"})


def _adapter(dest: FakeDestination):
    if dest.forge == "forgejo":
        adapter = ForgejoAdapter(
            base_url="https://forge.invalid", token="forgejo-token",
            webhook_secret="s",
        )
        adapter._client = httpx.AsyncClient(
            base_url="https://forge.invalid/api/v1",
            transport=httpx.MockTransport(dest.handler),
        )
    else:
        adapter = GithubAdapter(token="github-token", webhook_secret="s")
        adapter._client = httpx.AsyncClient(
            base_url="https://api.github.invalid",
            transport=httpx.MockTransport(dest.handler),
        )
    adapter._fake = dest
    return adapter


def _config() -> OpsEngineConfig:
    return OpsEngineConfig.load(
        {
            "orgs": {
                "capacium": {
                    "repositories": {
                        "capacium": {
                            "destinations": [
                                {
                                    "forge": "forgejo",
                                    "repo": REPO,
                                    "role": "release",
                                    "credential_variable": "FORGEJO_TOKEN",
                                },
                                {
                                    "forge": "github",
                                    "repo": REPO,
                                    "role": "release",
                                    "credential_variable": "GITHUB_TOKEN",
                                },
                            ],
                            "release": {
                                "enabled": True,
                                "forgejo_assets": "pull",
                                "manifest": MANIFEST,
                            },
                        }
                    }
                }
            }
        }
    )


@pytest.mark.asyncio
async def test_the_pull_verifies_the_piece_after_upload_with_the_shipped_downloader(
    tmp_path,
):
    """The pull reads the Forgejo asset back by browser_download_url.

    The shipped downloader is used end to end (no injected fake): the piece is
    read from the GitHub release with the GitHub token, uploaded, then read
    back from the Forgejo release with the Forgejo token through its
    ``browser_download_url``, and the bytes match. Against the base commit the
    read-back raises ``KeyError: 'url'`` and the destination ends failed.
    """
    forgejo = FakeDestination(forge="forgejo", repo=REPO, preloaded=CONTENT)
    github = FakeDestination(
        forge="github", repo=REPO, preloaded={**CONTENT, GITHUB_BINARY: GITHUB_BYTES}
    )
    transport = DownloadTransport(forgejo, github)
    downloader = HttpxAssetDownloader(
        client=httpx.AsyncClient(transport=httpx.MockTransport(transport.handler))
    )
    art = tmp_path / "dist"
    art.mkdir()
    for name, data in CONTENT.items():
        (art / name).write_bytes(data)
    audit: list[str] = []

    report = await ReleaseHandler.publish_release(
        _config(),
        REPO,
        TAG,
        NOTES,
        art,
        name_template=TEMPLATE,
        adapters=[_adapter(forgejo), _adapter(github)],
        skip_existing=True,
        audit=audit,
        repo_dir=None,
        release_body=BODY,
        asset_downloader=downloader,
    )

    label = f"forgejo:{REPO}"
    assert report.failed == [], report.failure_details
    assert GITHUB_BINARY in forgejo.asset_names()
    assert report.pulled[label] == [f"{GITHUB_BINARY} {_sha256(GITHUB_BYTES)}"]

    # The GitHub read carried the GitHub token; the Forgejo read-back carried
    # the Forgejo token — never the other forge's credential.
    assert ("api.github.invalid", "token github-token") in transport.downloads
    assert ("forge.invalid", "token forgejo-token") in transport.downloads
    assert ("forge.invalid", "token github-token") not in transport.downloads
    assert ("api.github.invalid", "token forgejo-token") not in transport.downloads
