"""OE-201: the pull pairs a Forgejo destination with its GitHub destination
by repository path, not by exact string.

A repository has one path but two spellings: capacium's Forgejo destination is
``capacium/capacium`` while the GitHub repository is ``Capacium/capacium``.
Before this task ``_pull_github_assets`` paired the two only when the strings
were equal, so with the real spellings nothing paired, the GitHub adapter was
never read, and no github-produced piece was ever attached to the Forgejo
release.

The red proof below uses the real spellings and six github-produced pieces,
with the pull armed. Against the base commit nothing is attached (the GitHub
adapter is never called); with the fix every piece is downloaded from the
GitHub release, checked against the GitHub digest and attached to the Forgejo
release.

The fake destinations are ``httpx`` transports behind the real adapters and
the downloader is injected, exactly like ``tests/test_asset_pull.py``: no
network, no token beyond the fakes' own.
"""

import hashlib
import json

import httpx
import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import OpsEngineConfig
from ops_engine.modules.release import ReleaseHandler

# The real spellings of one repository: Forgejo lower-cases the org, GitHub
# keeps its display case.
FORGEJO_REPO = "capacium/capacium"
GITHUB_REPO = "Capacium/capacium"
TAG = "v3.7.4"
VERSION = "3.7.4"
TEMPLATE = "Ops Engine {tag_name}"
RELEASE_NAME = "Ops Engine v3.7.4"
NOTES = "- A feature.\n\n- A fix.\n"
BODY = f"## Ops Engine {TAG}\n\n{NOTES}\n"
RELEASE_ID = 4242

# The Forgejo build's own files (the release carries these either way).
FILES = [
    "capacium-3.7.4-py3-none-any.whl",
    "capacium-3.7.4.tar.gz",
    "SHA256SUMS",
]
CONTENT = {name: f"forgejo built bytes of {name}\n".encode() for name in FILES}

# Six github-produced pieces, only the GitHub release carries them.
GITHUB_PIECES = {
    f"capacium-{VERSION}-{target}": f"github built bytes for {target}\n".encode()
    for target in (
        "linux-amd64",
        "linux-arm64",
        "darwin-amd64",
        "darwin-arm64",
        "windows-amd64",
        "windows-arm64",
    )
}

MANIFEST = {
    "assets": [
        {
            "type": "github_release_tarball",
            "name": f"capacium-<version>-{target}",
            "producer": "github",
            "destinations": [f"forgejo:{FORGEJO_REPO}", f"github:{GITHUB_REPO}"],
        }
        for target in (
            "linux-amd64",
            "linux-arm64",
            "darwin-amd64",
            "darwin-arm64",
            "windows-amd64",
            "windows-arm64",
        )
    ]
}


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _config() -> OpsEngineConfig:
    """The pull armed, both destinations spelled as the forges spell them."""
    return OpsEngineConfig.load(
        {
            "orgs": {
                "capacium": {
                    "repositories": {
                        "capacium": {
                            "destinations": [
                                {
                                    "forge": "forgejo",
                                    "repo": FORGEJO_REPO,
                                    "role": "release",
                                    "credential_variable": "FORGEJO_TOKEN",
                                },
                                {
                                    "forge": "github",
                                    "repo": GITHUB_REPO,
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


class FakeDestination:
    """One forge's release endpoint, addressed by the path it was asked for.

    The GitHub side holds the six pieces with a digest; the Forgejo side holds
    only the build's own files and reports ``digest: null``. Both answer
    ``/repos/<path>/...``: a request for a path this forge does not know is a
    404, so a wrong spelling shows up as a missing release, never as a silent
    success.
    """

    def __init__(self, *, forge: str, repo: str, preloaded=None, create_status=409):
        self.forge = forge
        self.repo = repo
        self.create_status = create_status
        self.asset_bytes: dict[str, bytes] = {}
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
        asset = {
            "id": self.next_id,
            "name": name,
            "size": len(data),
            "url": (
                f"https://{self.forge}.invalid/repos/{self.repo}"
                f"/releases/assets/{self.next_id}"
            ),
            "digest": _sha256(data) if self.forge == "github" else None,
        }
        return asset

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        # .../repos/<owner>/<name>/... -> "<owner>/<name>"; the base URL may
        # carry an ``/api/v1`` prefix.
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


class FakeDownloader:
    """Serve a destination's bytes; record which forge each read came from."""

    def __init__(self):
        self.reads: list[tuple[str, str]] = []

    async def download(self, adapter, repo: str, asset: dict) -> bytes:
        fake = adapter._fake
        self.reads.append((fake.forge, asset["name"]))
        return fake.asset_bytes[asset["name"]]


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


def _artifacts(tmp_path) -> None:
    art = tmp_path / "dist"
    art.mkdir(exist_ok=True)
    for name, data in CONTENT.items():
        (art / name).write_bytes(data)


@pytest.mark.asyncio
async def test_the_pull_pairs_the_real_spellings_and_attaches_every_piece(tmp_path):
    """The six github-produced pieces land on Forgejo with the GitHub digest.

    A Forgejo destination ``capacium/capacium`` and a GitHub destination
    ``Capacium/capacium`` pair by repository path. Every piece missing on
    Forgejo is read from the GitHub release, its SHA-256 checked against the
    GitHub digest before anything is uploaded, and attached to the Forgejo
    release.

    Against the base commit nothing pairs: the GitHub adapter is never called,
    no piece is downloaded and the Forgejo release keeps only the build's own
    files, so the assertion on the attached pieces fails.
    """
    forgejo = FakeDestination(forge="forgejo", repo=FORGEJO_REPO, preloaded=CONTENT)
    github = FakeDestination(
        forge="github", repo=GITHUB_REPO, preloaded=GITHUB_PIECES
    )
    downloader = FakeDownloader()
    _artifacts(tmp_path)
    audit: list[str] = []

    report = await ReleaseHandler.publish_release(
        _config(),
        FORGEJO_REPO,
        TAG,
        NOTES,
        tmp_path / "dist",
        name_template=TEMPLATE,
        adapters=[_adapter(forgejo), _adapter(github)],
        skip_existing=True,
        audit=audit,
        repo_dir=None,
        release_body=BODY,
        asset_downloader=downloader,
    )

    piece_names = set(GITHUB_PIECES)

    # The pieces missing on Forgejo are attached to the Forgejo release —
    # against the base commit nothing is attached, so this fails first.
    assert piece_names <= set(forgejo.asset_names()), (
        f"nothing attached to the Forgejo release: {forgejo.asset_names()}"
    )
    assert set(forgejo.uploaded) == piece_names

    # Every piece read came from the GitHub destination, and the GitHub
    # adapter was called with the GitHub destination's own spelling.
    assert {name for forge, name in downloader.reads if forge == "github"} == piece_names
    assert GITHUB_REPO in github.requested_repos
    assert FORGEJO_REPO not in github.requested_repos

    # Nothing failed and nothing is pending: the pieces are not "on their way".
    assert report.failed == [], report.failure_details
    assert report.pending == {}

    # Each attached piece's SHA-256 equals the GitHub digest.
    assert report.pulled == {
        f"forgejo:{FORGEJO_REPO}": [
            f"{name} {_sha256(data)}" for name, data in GITHUB_PIECES.items()
        ]
    }
