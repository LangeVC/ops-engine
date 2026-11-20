"""REA-017: the engine pulls GitHub-built assets into the Forgejo release.

A GitHub-built binary stays on GitHub only unless the repository declares
``release.forgejo_assets: pull`` in its ``.ops.yaml`` (D4): the default is
``off`` for every repository, capacium included. With ``pull`` the engine,
once the github-produced asset is present on GitHub, downloads it with its
own GitHub credential, checks the SHA-256 against the digest the GitHub API
reports, attaches it to the Forgejo release and re-downloads it from Forgejo
to check the bytes again — Forgejo reports no digest of its own. A mismatch
fails by name and attaches nothing.

Criterion 1: With the switch off nothing is downloaded and the Forgejo
release carries only forgejo-produced assets.

Criterion 2: With pull a github-produced asset appears on the Forgejo
release with a SHA-256 equal to the GitHub digest; a mismatch fails by name
and attaches nothing.

Criterion 3: The pull runs only after the asset is present on GitHub, and
the Forgejo free-space reading is part of the report.

The fake destinations are ``httpx`` transports behind the real adapters; the
download and the free-space reading are injectable collaborators, exactly
like the REA-016 channel reader, so the tests touch no network and use no
token beyond the one the fake adapters already carry.
"""

import hashlib
import json

import httpx
import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import OpsEngineConfig
from ops_engine.modules.release import ReleaseHandler

REPO = "langevc/ops-engine"
TAG = "v3.5.1"
VERSION = "3.5.1"
TEMPLATE = "Ops Engine {tag_name}"
RELEASE_NAME = "Ops Engine v3.5.1"
NOTES = "- A feature.\n\n- A fix.\n"
BODY = f"## Ops Engine {TAG}\n\n{NOTES}\n"
RELEASE_ID = 77

FILES = [
    "ops_engine-3.5.1-py3-none-any.whl",
    "ops_engine-3.5.1.tar.gz",
    "SHA256SUMS",
]
CONTENT = {name: f"built bytes of {name}\n".encode() for name in FILES}

# A github-built binary the Forgejo build never carries.
GITHUB_BINARY = "ops-engine-tool-3.5.1-linux-amd64.tar.gz"
GITHUB_BYTES = b"github built bytes\n"

FREE_BYTES = 987654321

# `release.manifest`: a github-produced release-object asset that only the
# GitHub destination must hold — the D4 default this task makes optional.
MANIFEST = {
    "assets": [
        {
            "type": "github_release_tarball",
            "name": "ops-engine-tool-<version>-linux-amd64.tar.gz",
            "producer": "github",
            "destinations": [f"github:{REPO}"],
        }
    ]
}


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _config(
    forge: str,
    *,
    second: str | None = None,
    manifest=None,
    forgejo_assets=None,
) -> OpsEngineConfig:
    destinations = [
        {
            "forge": forge,
            "repo": REPO,
            "role": "release",
            "credential_variable": "OPS_TOKEN",
        }
    ]
    if second is not None:
        destinations.append(
            {
                "forge": second,
                "repo": REPO,
                "role": "release",
                "credential_variable": "SECOND_TOKEN",
            }
        )
    release: dict = {"enabled": True}
    if manifest is not None:
        release["manifest"] = manifest
    if forgejo_assets is not None:
        release["forgejo_assets"] = forgejo_assets
    return OpsEngineConfig.load(
        {
            "orgs": {
                "langevc": {
                    "repositories": {
                        "ops-engine": {
                            "destinations": destinations,
                            "release": release,
                        }
                    }
                }
            }
        }
    )


def _release_config(config):
    return config.get_repo_config("langevc", "ops-engine").release


class FakeDestination:
    """Serve one release by tag, record every call and hold asset bytes.

    ``preloaded`` seeds an existing release (the GitHub workflow attached the
    binary before this run); its assets carry the GitHub digest. The Forgejo
    side reports ``digest: null`` and a size only, the way the host does.
    ``digest_override`` makes one asset's *reported* digest differ from the
    bytes served, the mismatch criterion 2 fails on.
    """

    def __init__(
        self,
        *,
        forge: str,
        timeline: list,
        preloaded=None,
        created_at=None,
        hidden=(),
        create_status=409,
        digest_override=None,
    ):
        self.forge = forge
        self.timeline = timeline
        self.created_at = created_at
        self.hidden = set(hidden)
        self.create_status = create_status
        self.digest_override = dict(digest_override or {})
        self.asset_bytes: dict[str, bytes] = {}
        self.next_id = 900
        self.calls: list[tuple[str, str]] = []
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
                "created_at": created_at,
                "assets": assets,
            }

    def _asset(self, name: str, data: bytes) -> dict:
        self.next_id += 1
        asset = {
            "id": self.next_id,
            "name": name,
            "size": len(data),
            "url": (
                f"https://{self.forge}.invalid/repos/{REPO}"
                f"/releases/assets/{self.next_id}"
            ),
        }
        if self.forge == "github":
            asset["digest"] = self.digest_override.get(name, _sha256(data))
        else:
            asset["digest"] = None
        return asset

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        self.timeline.append((self.forge, request.method, path))
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
            if name not in self.hidden:
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
                "created_at": self.created_at,
                "assets": [],
            }
            return httpx.Response(201, json=self.release)
        return httpx.Response(
            500, json={"message": f"unexpected {request.method} {path}"}
        )

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.calls if m != "GET"]

    def asset_names(self) -> list[str]:
        return [asset["name"] for asset in (self.release or {}).get("assets") or []]


class FakeDownloader:
    """Serve the bytes a destination holds; record every read in order.

    Only a read exists: it is the engine's download with the destination's
    own credential, and it records the token the adapter carried so a test can
    prove the GitHub read used the engine's GitHub credential.
    """

    def __init__(self, timeline: list):
        self.timeline = timeline
        self.reads: list[tuple[str, str]] = []
        self.tokens: list[tuple[str, str]] = []

    async def download(self, adapter, repo: str, asset: dict) -> bytes:
        fake = adapter._fake
        name = asset["name"]
        self.reads.append((fake.forge, name))
        self.tokens.append((fake.forge, getattr(adapter, "token", "")))
        self.timeline.append((fake.forge, "DOWNLOAD", name))
        return fake.asset_bytes[name]


class FakeFreeSpaceReader:
    """Answer the Forgejo host's free space without touching the network."""

    def __init__(self, value=FREE_BYTES):
        self.value = value
        self.reads: list[str] = []

    async def free_bytes(self, destination):
        self.reads.append(f"{destination.forge}:{destination.repo}")
        return self.value


def _forgejo(dest: FakeDestination) -> ForgejoAdapter:
    adapter = ForgejoAdapter(
        base_url="https://forge.invalid", token="t", webhook_secret="s"
    )
    adapter._client = httpx.AsyncClient(
        base_url="https://forge.invalid/api/v1",
        transport=httpx.MockTransport(dest.handler),
    )
    adapter._fake = dest
    return adapter


def _github(dest: FakeDestination) -> GithubAdapter:
    adapter = GithubAdapter(token="t", webhook_secret="s")
    adapter._client = httpx.AsyncClient(
        base_url="https://api.github.invalid",
        transport=httpx.MockTransport(dest.handler),
    )
    adapter._fake = dest
    return adapter


def _make(dest: FakeDestination):
    return _forgejo(dest) if dest.forge == "forgejo" else _github(dest)


def _artifacts(tmp_path) -> None:
    art = tmp_path / "dist"
    art.mkdir(exist_ok=True)
    for name, data in CONTENT.items():
        (art / name).write_bytes(data)


async def _publish(
    destinations,
    tmp_path,
    *,
    config,
    downloader=None,
    free_space_reader=None,
):
    _artifacts(tmp_path)
    audit: list[str] = []
    report = await ReleaseHandler.publish_release(
        config,
        REPO,
        TAG,
        NOTES,
        tmp_path / "dist",
        name_template=TEMPLATE,
        adapters=[_make(dest) for dest in destinations],
        skip_existing=True,
        audit=audit,
        release_body=BODY,
        asset_downloader=downloader,
        free_space_reader=free_space_reader,
    )
    return report, audit


def _both(timeline: list, *, github_preloaded=None, digest_override=None):
    forgejo = FakeDestination(forge="forgejo", timeline=timeline)
    github = FakeDestination(
        forge="github",
        timeline=timeline,
        preloaded=github_preloaded,
        digest_override=digest_override,
    )
    return [forgejo, github], forgejo, github


# --- Criterion 1: switch off -> nothing downloaded, only forgejo assets ----


@pytest.mark.asyncio
async def test_with_the_switch_off_nothing_is_downloaded(tmp_path):
    """Criterion 1: With the switch off nothing is downloaded and the Forgejo
    release carries only forgejo-produced assets.

    The github-produced binary is on the GitHub release (another workflow put
    it there). With the switch absent the engine downloads nothing and the
    Forgejo release holds exactly the build's own files."""
    timeline: list = []
    destinations, forgejo, _ = _both(
        timeline, github_preloaded={**CONTENT, GITHUB_BINARY: GITHUB_BYTES}
    )
    downloader = FakeDownloader(timeline)
    config = _config("forgejo", second="github", manifest=MANIFEST)

    assert _release_config(config).forgejo_assets == "off"

    report, _ = await _publish(
        destinations, tmp_path, config=config, downloader=downloader
    )

    assert downloader.reads == [], "with the switch off nothing is downloaded"
    assert sorted(forgejo.asset_names()) == sorted(FILES), (
        "the Forgejo release must carry only forgejo-produced assets"
    )
    assert getattr(report, "pulled", None) == {}
    assert report.failed == []


# --- Criterion 2: pull -> the asset lands with the GitHub digest -----------


@pytest.mark.asyncio
async def test_with_pull_the_github_asset_appears_on_forgejo_with_the_github_digest(
    tmp_path,
):
    """Criterion 2: With pull a github-produced asset appears on the Forgejo
    release with a SHA-256 equal to the GitHub digest.

    The download is made with the engine's own GitHub credential; the digest
    is checked before the upload and the uploaded bytes are re-downloaded from
    Forgejo, which reports no digest of its own, and checked again."""
    timeline: list = []
    destinations, forgejo, _ = _both(
        timeline, github_preloaded={**CONTENT, GITHUB_BINARY: GITHUB_BYTES}
    )
    downloader = FakeDownloader(timeline)
    config = _config(
        "forgejo", second="github", manifest=MANIFEST, forgejo_assets="pull"
    )

    report, audit = await _publish(
        destinations, tmp_path, config=config, downloader=downloader
    )

    label = f"forgejo:{REPO}"
    assert report.failed == [], report.failure_details
    assert GITHUB_BINARY in forgejo.asset_names()
    assert report.pulled[label] == [f"{GITHUB_BINARY} {_sha256(GITHUB_BYTES)}"]
    # The download is made with the GitHub adapter's own credential.
    assert ("github", "t") in downloader.tokens
    # Both the pre-upload and the after-upload check read the same bytes.
    assert downloader.reads == [
        ("github", GITHUB_BINARY),
        ("forgejo", GITHUB_BINARY),
    ]
    assert [line for line in audit if "result=pulled" in line]


@pytest.mark.asyncio
async def test_a_digest_mismatch_fails_by_name_and_attaches_nothing(tmp_path):
    """Criterion 2: a mismatch fails by name and attaches nothing.

    GitHub reports a digest that does not match the bytes it serves; the
    engine refuses to attach them and names the file and the destination."""
    timeline: list = []
    destinations, forgejo, _ = _both(
        timeline,
        github_preloaded={**CONTENT, GITHUB_BINARY: GITHUB_BYTES},
        digest_override={GITHUB_BINARY: "sha256:" + "0" * 64},
    )
    downloader = FakeDownloader(timeline)
    config = _config(
        "forgejo", second="github", manifest=MANIFEST, forgejo_assets="pull"
    )

    report, _ = await _publish(
        destinations, tmp_path, config=config, downloader=downloader
    )

    label = f"forgejo:{REPO}"
    assert report.failed == [label]
    assert GITHUB_BINARY in report.failure_details[label]
    assert label in report.failure_details[label]
    assert sorted(forgejo.asset_names()) == sorted(FILES), (
        "a mismatch attaches nothing"
    )
    assert not report.pulled.get(label)
    assert sorted(forgejo.uploaded) == sorted(FILES), (
        "the mismatched bytes are never uploaded: only the build's own files are"
    )


# --- Criterion 3: pull only after GitHub presence; free-space in report ----


@pytest.mark.asyncio
async def test_the_pull_runs_only_after_the_asset_is_present_on_github(tmp_path):
    """Criterion 3: The pull runs only after the asset is present on GitHub.

    While the binary is still on its way the engine downloads nothing, the
    piece stays pending, and the Forgejo release is not given a file no
    digest vouches for."""
    timeline: list = []
    destinations, forgejo, _ = _both(
        timeline, github_preloaded=dict(CONTENT)
    )
    downloader = FakeDownloader(timeline)
    config = _config(
        "forgejo", second="github", manifest=MANIFEST, forgejo_assets="pull"
    )

    report, _ = await _publish(
        destinations, tmp_path, config=config, downloader=downloader
    )

    assert downloader.reads == [], "nothing is downloaded before GitHub has it"
    assert any(entry[1] == "DOWNLOAD" for entry in timeline) is False
    assert report.pending.get(f"github:{REPO}"), (
        "the absent github-produced asset stays pending"
    )
    assert GITHUB_BINARY not in forgejo.asset_names()
    assert report.failed == []


@pytest.mark.asyncio
async def test_the_pull_reads_github_before_downloading(tmp_path):
    """Criterion 3: the GitHub release is read — its asset list carries the
    digest — before any byte is downloaded, and the download is the GitHub
    one."""
    timeline: list = []
    destinations, _, _ = _both(
        timeline, github_preloaded={**CONTENT, GITHUB_BINARY: GITHUB_BYTES}
    )
    downloader = FakeDownloader(timeline)
    config = _config(
        "forgejo", second="github", manifest=MANIFEST, forgejo_assets="pull"
    )

    await _publish(destinations, tmp_path, config=config, downloader=downloader)

    first_download = next(
        index for index, entry in enumerate(timeline) if entry[1] == "DOWNLOAD"
    )
    assert timeline[first_download][0] == "github"
    listed = [
        index
        for index, entry in enumerate(timeline)
        if entry[0] == "github" and entry[1] == "GET"
        and entry[2].endswith(f"/releases/{RELEASE_ID}/assets")
    ]
    assert listed, "the GitHub asset list is read first"
    assert listed[0] < first_download


@pytest.mark.asyncio
async def test_a_later_run_pulls_the_piece_the_first_run_left_pending(tmp_path):
    """Criterion 3: the pull follows the GitHub presence, not the first run.

    The Forgejo release already exists (a previous run created it while the
    binary was still on its way), so this run skips it — and still pulls the
    piece now that GitHub holds it, with the same pre-upload digest check."""
    timeline: list = []
    forgejo = FakeDestination(
        forge="forgejo", timeline=timeline, preloaded=dict(CONTENT)
    )
    github = FakeDestination(
        forge="github",
        timeline=timeline,
        preloaded={**CONTENT, GITHUB_BINARY: GITHUB_BYTES},
    )
    downloader = FakeDownloader(timeline)
    config = _config(
        "forgejo", second="github", manifest=MANIFEST, forgejo_assets="pull"
    )

    report, _ = await _publish(
        [forgejo, github], tmp_path, config=config, downloader=downloader
    )

    label = f"forgejo:{REPO}"
    assert report.failed == [], report.failure_details
    assert GITHUB_BINARY in forgejo.asset_names()
    assert report.pulled[label] == [f"{GITHUB_BINARY} {_sha256(GITHUB_BYTES)}"]
    assert downloader.reads == [
        ("github", GITHUB_BINARY),
        ("forgejo", GITHUB_BINARY),
    ]


@pytest.mark.asyncio
async def test_the_forgejo_free_space_reading_is_part_of_the_report(tmp_path):
    """Criterion 3: The Forgejo free-space reading is part of the report.

    The reading comes from the injectable reader (an HTTP source in
    production, never ssh) and names the destination it describes."""
    timeline: list = []
    destinations, _, _ = _both(
        timeline, github_preloaded={**CONTENT, GITHUB_BINARY: GITHUB_BYTES}
    )
    downloader = FakeDownloader(timeline)
    free_space = FakeFreeSpaceReader()
    config = _config(
        "forgejo", second="github", manifest=MANIFEST, forgejo_assets="pull"
    )

    report, _ = await _publish(
        destinations,
        tmp_path,
        config=config,
        downloader=downloader,
        free_space_reader=free_space,
    )

    label = f"forgejo:{REPO}"
    assert report.free_space[label] == f"free_bytes={FREE_BYTES}"
    assert free_space.reads == [label]


# --- The switch: default off, unknown refused, per repository --------------


def test_the_switch_defaults_to_off_and_refuses_an_unknown_value():
    """The switch is ``off`` unless declared and only ``off``/``pull`` load.

    It stays out of ``model_fields`` like ``assets`` and ``manifest``, so an
    existing release block that never mentions it keeps loading."""
    from ops_engine.config_loader import ReleaseConfig, OpsYamlError

    assert _release_config(_config("forgejo")).forgejo_assets == "off"
    assert (
        _release_config(
            _config("forgejo", forgejo_assets="pull")
        ).forgejo_assets
        == "pull"
    )
    assert "forgejo_assets" not in ReleaseConfig.model_fields

    with pytest.raises(Exception) as exc:
        _config("forgejo", forgejo_assets="sync")
    assert "release.forgejo_assets" in str(exc.value)

    # A Layer-3 `.ops.yaml` sets the switch per repository, naming the file.
    import tempfile
    from pathlib import Path

    from ops_engine.config_loader import load_ops_yaml

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / ".ops.yaml"
        path.write_text("release:\n  forgejo_assets: pull\n", encoding="utf-8")
        assert load_ops_yaml(directory).release.forgejo_assets == "pull"
        path.write_text("release:\n  forgejo_assets: sync\n", encoding="utf-8")
        with pytest.raises(OpsYamlError) as bad:
            load_ops_yaml(directory)
        assert "release.forgejo_assets" in str(bad.value)


# --- The default downloader: credentialed, read-only -----------------------


@pytest.mark.asyncio
async def test_the_default_downloader_uses_the_adapters_credential_and_only_reads():
    """The shipped downloader issues one credentialed GET per asset and
    nothing else: it is a read, never a write on the source forge."""
    from ops_engine.modules import asset_pull

    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (request.method, request.url.path, request.headers.get("authorization"))
        )
        return httpx.Response(200, content=GITHUB_BYTES)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GithubAdapter(token="t", webhook_secret="s")
    downloader = asset_pull.HttpxAssetDownloader(client=client)

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
            "token t",
        )
    ]
