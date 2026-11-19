"""REA-014: the engine verifies the asset set on every destination.

After create/converge/attach the engine reads the release back from each
destination and compares it to the declared assets (``release.assets``,
REA-010) and the build on disk. This is the check RFL-040 did not make.

Criterion 1: a destination missing a declared asset fails the run and the
message names the file and the destination.

Criterion 2: a run with every file present on both destinations reports no
failure and prints the name and SHA-256 of each asset per destination.

Criterion 3: RED PROOF — the 2f5c255 code exits 0 on a release without the
declared assets (``ReleaseConfig`` drops the key, so nothing is verified); the
new code fails it by name and destination.

The release CLI — and with it ``exit 0`` — is REA-015; here the report's
``failed`` list is the outcome the exit code will be derived from, exactly as
REA-013 records it.

A declaration of ``ghcr_image`` or ``capacium_exchange`` is not an asset of the
release object (it is published elsewhere), so it is never read back. Only
``github_release_zip`` and ``github_release_tarball`` declarations are
verified, with ``<version>``/``<tag>`` rendered from the tag.

The fake destination is an ``httpx`` transport behind the real adapters; every
test drives ``ReleaseHandler.publish_release`` — the call shape the shipped
workflow uses.
"""

import hashlib
import json
from collections import Counter

import httpx
import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import OpsEngineConfig
from ops_engine.modules.release import ReleaseHandler

REPO = "langevc/ops-engine"
TAG = "v3.5.1"
TEMPLATE = "Ops Engine {tag_name}"
NOTES = "- A feature.\n\n- A fix.\n"
BODY = f"## Ops Engine {TAG}\n\n{NOTES}\n"
RELEASE_ID = 77

FILES = [
    "ops_engine-3.5.1-py3-none-any.whl",
    "ops_engine-3.5.1.tar.gz",
    "SHA256SUMS",
]
CONTENT = {name: f"built bytes of {name}\n".encode() for name in FILES}


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# The declarations use the placeholder form the fleet writes in .ops.yaml;
# ``<version>`` renders from the tag and resolves to a build file.
DECLARED = [
    {"type": "github_release_zip", "name": "ops_engine-<version>-py3-none-any.whl"},
    {"type": "github_release_tarball", "name": "ops_engine-<version>.tar.gz"},
    {"type": "github_release_tarball", "name": "SHA256SUMS"},
]


def _config(forge: str, *, second: str | None = None, assets=None) -> OpsEngineConfig:
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
    release = {"enabled": True}
    if assets is not None:
        release["assets"] = assets
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


class FakeDestination:
    """Serve one release by tag and record every write.

    ``hidden`` names answer the asset upload with 201 but never appear in the
    release's asset list, the way a lost attach looks from the read side.
    ``tamper`` overrides the digest/size an uploaded asset reports.
    """

    def __init__(self, *, forge: str, hidden=(), tamper=None, create_status=409):
        self.forge = forge
        self.release: dict | None = None
        self.hidden = set(hidden)
        self.tamper = tamper or {}
        self.create_status = create_status
        self.calls: list[tuple[str, str]] = []
        self.uploaded: list[dict] = []
        self.read_assets: list[list[dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.method == "GET" and "/releases/tags/" in path:
            if self.release is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=self.release)
        if request.method == "GET" and path.endswith("/releases"):
            return httpx.Response(200, json=[] if self.release is None else [self.release])
        if (
            request.method == "GET"
            and path.endswith(f"/releases/{RELEASE_ID}/assets")
        ):
            # Assets come back on the first page only: the adapters walk pages
            # until one is empty.
            assets = list((self.release or {}).get("assets") or [])
            self.read_assets.append(assets)
            page = request.url.params.get("page", "1")
            return httpx.Response(200, json=assets if page == "1" else [])
        if request.method == "PATCH" and path.endswith(f"/releases/{RELEASE_ID}"):
            body = json.loads(request.content)
            self.release = {**(self.release or {}), **body}
            return httpx.Response(200, json=self.release)
        if request.method == "POST" and path.endswith(
            f"/releases/{RELEASE_ID}/assets"
        ):
            name = request.url.params.get("name")
            data = request.content
            asset = {"name": name, "size": len(data)}
            asset["digest"] = _sha256(data) if self.forge == "github" else None
            asset.update(self.tamper.get(name, {}))
            if name not in self.hidden:
                self.uploaded.append(asset)
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
                "assets": [],
            }
            return httpx.Response(201, json=self.release)
        return httpx.Response(
            500, json={"message": f"unexpected {request.method} {path}"}
        )

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.calls if m != "GET"]

    def creates(self) -> int:
        return sum(1 for m, p in self.calls if m == "POST" and p.endswith("/releases"))


def _forgejo(dest: FakeDestination) -> ForgejoAdapter:
    adapter = ForgejoAdapter(
        base_url="https://forge.invalid", token="t", webhook_secret="s"
    )
    adapter._client = httpx.AsyncClient(
        base_url="https://forge.invalid/api/v1",
        transport=httpx.MockTransport(dest.handler),
    )
    return adapter


def _github(dest: FakeDestination) -> GithubAdapter:
    adapter = GithubAdapter(token="t", webhook_secret="s")
    adapter._client = httpx.AsyncClient(
        base_url="https://api.github.invalid",
        transport=httpx.MockTransport(dest.handler),
    )
    return adapter


def _make(dest: FakeDestination):
    return _forgejo(dest) if dest.forge == "forgejo" else _github(dest)


def _artifacts(tmp_path, extra: dict[str, bytes] | None = None) -> None:
    art = tmp_path / "dist"
    art.mkdir(exist_ok=True)
    for name, data in CONTENT.items():
        (art / name).write_bytes(data)
    for name, data in (extra or {}).items():
        (art / name).write_bytes(data)


async def _publish(destinations, tmp_path, *, config, repo_dir=None):
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
        repo_dir=repo_dir,
        release_body=BODY,
    )
    return report, audit


# --- Criterion 2: everything present -> no failure, name + SHA-256 ----------


@pytest.mark.parametrize("forge", ["forgejo", "github"])
@pytest.mark.asyncio
async def test_every_declared_file_present_reports_name_and_sha256(
    forge, tmp_path
):
    """Criterion 2: the build carries every declared file, publication attaches
    them, and the read-back names each file and its SHA-256 on the destination.
    """
    dest = FakeDestination(forge=forge)
    report, audit = await _publish(
        [dest], tmp_path, config=_config(forge, assets=DECLARED)
    )

    label = f"{forge}:{REPO}"
    assert report.failed == [] and report.partial is False
    entries = report.verified[label]
    assert sorted(entries) == sorted(
        f"{name} {_sha256(CONTENT[name])}" for name in FILES
    )
    # The run summary names the file and its SHA-256 for the destination.
    verified_lines = [line for line in audit if "result=verified" in line]
    assert len(verified_lines) == 1
    for name in FILES:
        assert f"{name} {_sha256(CONTENT[name])}" in verified_lines[0]
    assert label in verified_lines[0]


@pytest.mark.asyncio
async def test_every_declared_file_present_on_both_destinations(tmp_path):
    """Criterion 2 across both forges, in destination order."""
    first = FakeDestination(forge="forgejo")
    second = FakeDestination(forge="github")
    report, audit = await _publish(
        [first, second],
        tmp_path,
        config=_config("forgejo", second="github", assets=DECLARED),
    )

    assert report.failed == [] and report.partial is False
    assert report.verified.keys() == {
        f"forgejo:{REPO}",
        f"github:{REPO}",
    }
    verified_lines = [line for line in audit if "result=verified" in line]
    assert len(verified_lines) == 2
    assert verified_lines[0].index(f"forgejo:{REPO}") >= 0
    assert verified_lines[1].index(f"github:{REPO}") >= 0


# --- Criterion 1: a missing declared asset fails by file and destination ----


@pytest.mark.parametrize(
    "forge,make",
    [("forgejo", _forgejo), ("github", _github)],
)
@pytest.mark.asyncio
async def test_missing_declared_asset_fails_naming_file_and_destination(
    forge, make, tmp_path
):
    """Criterion 1: the destination never shows the declared file, so the run
    fails and the message names the file and the destination."""
    dest = FakeDestination(forge=forge, hidden={"SHA256SUMS"})
    report, audit = await _publish(
        [dest], tmp_path, config=_config(forge, assets=DECLARED)
    )

    label = f"{forge}:{REPO}"
    assert report.failed == [label]
    assert report.partial is True
    detail = report.failure_details[label]
    assert "SHA256SUMS" in detail
    assert "verification failed" in detail
    assert report.published == []
    failed_lines = [line for line in audit if "result=verify-failed" in line]
    assert len(failed_lines) == 1
    assert label in failed_lines[0] and "SHA256SUMS" in failed_lines[0]


@pytest.mark.asyncio
async def test_a_missing_file_fails_only_the_destination_that_lacks_it(tmp_path):
    """Criterion 1: verification is per destination; the one that has every
    file is not failed by the other's gap."""
    first = FakeDestination(forge="forgejo")
    second = FakeDestination(forge="github", hidden={"SHA256SUMS"})
    report, _ = await _publish(
        [first, second],
        tmp_path,
        config=_config("forgejo", second="github", assets=DECLARED),
    )

    assert report.failed == [f"github:{REPO}"]
    assert report.verified.keys() == {f"forgejo:{REPO}"}


@pytest.mark.asyncio
async def test_a_declared_file_absent_from_the_build_fails_by_name(tmp_path):
    """Criterion 1: a declared file the build does not carry cannot be
    attached, so the read-back finds it missing and fails by name."""
    assets = [
        *DECLARED,
        {"type": "github_release_zip", "name": "not-built-<version>.zip"},
    ]
    dest = FakeDestination(forge="forgejo")
    report, _ = await _publish([dest], tmp_path, config=_config("forgejo", assets=assets))

    label = f"forgejo:{REPO}"
    assert report.failed == [label]
    assert "not-built-3.5.1.zip" in report.failure_details[label]


# --- Criterion 1: a file of another digest fails rather than passing ---------


@pytest.mark.asyncio
async def test_a_different_digest_fails_naming_file_and_destination(tmp_path):
    """The destination reports a SHA-256 that is not the build's: the run
    fails and names the file and both digests."""
    wrong = "sha256:" + "0" * 64
    dest = FakeDestination(
        forge="github", tamper={FILES[0]: {"digest": wrong}}
    )
    report, _ = await _publish(
        [dest], tmp_path, config=_config("github", assets=DECLARED)
    )

    label = f"github:{REPO}"
    detail = report.failure_details[label]
    assert report.failed == [label]
    assert FILES[0] in detail and wrong in detail
    assert _sha256(CONTENT[FILES[0]]) in detail


@pytest.mark.asyncio
async def test_a_different_size_on_forgejo_fails_naming_file_and_destination(tmp_path):
    """Forgejo exposes no digest (measured), so the size decides; a size that
    differs from the build fails by name."""
    dest = FakeDestination(
        forge="forgejo",
        tamper={FILES[1]: {"size": len(CONTENT[FILES[1]]) + 1}},
    )
    report, _ = await _publish(
        [dest], tmp_path, config=_config("forgejo", assets=DECLARED)
    )

    label = f"forgejo:{REPO}"
    detail = report.failure_details[label]
    assert report.failed == [label]
    assert FILES[1] in detail and "size" in detail


# --- Criterion 3: the RED proof --------------------------------------------


@pytest.mark.asyncio
async def test_red_proof_a_release_without_the_declared_assets_does_not_exit_0(
    tmp_path,
):
    """Criterion 3: a declared file the build does not carry leaves the release
    without it. At 2f5c255 the ``assets`` key is dropped by the config model,
    nothing is verified, and the run reports no failure (exit 0). With the
    change the run fails by name and destination.
    """
    assets = [{"type": "github_release_tarball", "name": "release-<version>.tar.xz"}]
    dest = FakeDestination(forge="forgejo")
    report, _ = await _publish([dest], tmp_path, config=_config("forgejo", assets=assets))

    label = f"forgejo:{REPO}"
    assert report.partial is True, "the run must not look clean"
    assert report.failed == [label]
    assert "release-3.5.1.tar.xz" in report.failure_details[label]


# --- Declarations come from the .ops.yaml at the tag -----------------------


@pytest.mark.asyncio
async def test_declared_assets_are_read_from_the_layer3_ops_yaml(tmp_path):
    """The declarations are the release block of the ``.ops.yaml`` merged over
    Layer 2 — the same file the publish path resolves destinations from."""
    (tmp_path / ".ops.yaml").write_text(
        "release:\n"
        "  enabled: true\n"
        "  assets:\n"
        "    - type: github_release_tarball\n"
        "      name: 'ops_engine-<version>.tar.gz'\n"
        "    - type: github_release_zip\n"
        "      name: 'absent-<version>.zip'\n",
        encoding="utf-8",
    )
    dest = FakeDestination(forge="forgejo")
    report, _ = await _publish(
        [dest], tmp_path, config=_config("forgejo"), repo_dir=tmp_path
    )

    label = f"forgejo:{REPO}"
    assert report.failed == [label]
    assert "absent-3.5.1.zip" in report.failure_details[label]
    assert "ops_engine-3.5.1.tar.gz" not in report.failure_details[label]


# --- No declaration, or no release-object declaration: nothing to verify ----


@pytest.mark.asyncio
async def test_an_undeclared_run_verifies_nothing(tmp_path):
    """Backwards compatibility: without ``release.assets`` the run behaves as
    before — one publish line, no verification line, no failure."""
    dest = FakeDestination(forge="forgejo")
    report, audit = await _publish([dest], tmp_path, config=_config("forgejo"))

    assert report.failed == []
    assert report.verified == {}
    assert audit == [f"release-publish tag={TAG} dest=forgejo:{REPO} result=published"]


@pytest.mark.asyncio
async def test_assets_none_and_non_release_assets_verify_nothing(tmp_path):
    """``assets: none`` means no requirement; ``ghcr_image`` and
    ``capacium_exchange`` are not release-object assets (REA-016 owns their
    fan-out), so neither fails a run."""
    for assets in (
        "none",
        [
            {"type": "ghcr_image", "name": "ghcr.io/langevc/engine:<tag>"},
            {
                "type": "capacium_exchange",
                "via": "Capacium/capacium-action-publish@v1",
            },
        ],
    ):
        dest = FakeDestination(forge="forgejo")
        report, audit = await _publish(
            [dest], tmp_path, config=_config("forgejo", assets=assets)
        )
        assert report.failed == []
        assert report.verified == {}
        assert all("result=verified" not in line for line in audit)


# --- The read side is the shipped asset list, not a second download --------


@pytest.mark.asyncio
async def test_verification_reads_the_asset_list_and_downloads_nothing(tmp_path):
    """The read-back is ``list_release_assets`` (REA-013) on the release found
    by tag; no asset is downloaded, and every upload happened exactly once."""
    dest = FakeDestination(forge="github")
    report, _ = await _publish(
        [dest], tmp_path, config=_config("github", assets=DECLARED)
    )

    assert dest.read_assets, "the asset list was never read back"
    assert all(name in {a["name"] for a in dest.uploaded} for name in FILES)
    assert Counter(a["name"] for a in dest.uploaded) == Counter(
        {name: 1 for name in FILES}
    )
    assert report.failed == []
