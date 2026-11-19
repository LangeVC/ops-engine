"""REA-013: assets are attached idempotently to every destination.

The engine attaches each file of the build to the release on every
destination. A name already present with the same SHA-256 is a no-op; the
same name with another digest fails by name and overwrites nothing.

Criterion 1: attaching twice leaves one asset per name and the run reports no
failure (the release CLI — and with it ``exit 0`` — is REA-015; here the
report's ``failed`` list is the outcome the exit code will be derived from).

Criterion 2: the same name with a different digest fails the destination by
name, and the existing asset is unchanged: no upload, no patch, no delete.

Criterion 3: RED PROOF — a release created without assets, then the attach
run, ends with all files present on both fake destinations; at 2f5c255 the
same flow ends with none. The route is the empty-body convergence of REA-012:
at 2f5c255 that branch patches only the body and uploads nothing, so the red
run of this file fails there.

Measured forge facts (``release-asset-comparison.md``, captured 2026-07-28):
GitHub release-asset objects expose ``digest`` as ``sha256:<hex>``; Forgejo
asset objects expose ``digest`` as ``null``. Both expose ``size``. A Forgejo
asset therefore cannot be compared by digest; the comparison falls back to
``size``, and an asset that carries neither (the bare ``{"name": ...}`` shape
every older fixture uses) cannot be proven different and stays a no-op. No
asset is ever downloaded to compute a digest (REL-010): the build's own bytes
are on disk, so its SHA-256 is free.

The fake destination is an ``httpx`` transport behind the real adapters; every
test drives ``ReleaseHandler.publish_release`` — the call shape the shipped
workflow uses.
"""

import hashlib
from collections import Counter

import httpx
import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import OpsEngineConfig
from ops_engine.modules.release import ReleaseHandler

REPO = "langevc/ops-engine"
TAG = "v3.5.1"
GATED_TITLE = "Ops Engine v3.5.1"
TEMPLATE = "Ops Engine {tag_name}"
NOTES = "- A feature.\n\n- A fix.\n"
BODY = f"## {GATED_TITLE}\n\n{NOTES}\n"
FILES = [
    "ops_engine-3.5.1-py3-none-any.whl",
    "ops_engine-3.5.1.tar.gz",
    "SHA256SUMS",
    "SHA256SUMS.sig",
]
CONTENT = {name: f"built bytes of {name}\n".encode() for name in FILES}
RELEASE_ID = 77


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _config(forge: str) -> OpsEngineConfig:
    return OpsEngineConfig.load(
        {
            "orgs": {
                "langevc": {
                    "repositories": {
                        "ops-engine": {
                            "destinations": [
                                {
                                    "forge": forge,
                                    "repo": REPO,
                                    "role": "release",
                                    "credential_variable": "OPS_TOKEN",
                                }
                            ]
                        }
                    }
                }
            }
        }
    )


def _release(**over) -> dict:
    """An existing release of the tag: the gated title, an empty body (the
    release the pipeline created before the assets were attached)."""
    release = {
        "id": RELEASE_ID,
        "tag_name": TAG,
        "name": GATED_TITLE,
        "body": "",
        "draft": False,
        "prerelease": False,
        "assets": [],
        "tarball_url": f"https://forge.invalid/{REPO}/archive/{TAG}.tar.gz",
        "zipball_url": f"https://forge.invalid/{REPO}/archive/{TAG}.zip",
    }
    release.update(over)
    return release


class FakeDestination:
    """Serve one release by tag and stamp attached assets the way its forge
    does: GitHub a ``sha256:`` digest, Forgejo ``digest: null`` plus ``size``.
    Records every write."""

    def __init__(self, release: dict, *, forge: str):
        self.release = release
        self.forge = forge
        self.calls: list[tuple[str, str]] = []
        self.patches: list[dict] = []
        self.uploaded: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json

        path = request.url.path
        self.calls.append((request.method, path))
        if request.method == "GET" and "/releases/tags/" in path:
            return httpx.Response(200, json=self.release)
        if request.method == "GET" and path.endswith("/releases"):
            return httpx.Response(200, json=[self.release])
        if request.method == "GET" and path.endswith(f"/releases/{RELEASE_ID}/assets"):
            return httpx.Response(200, json=self.release["assets"])
        if request.method == "PATCH" and path.endswith(f"/releases/{RELEASE_ID}"):
            body = json.loads(request.content)
            self.patches.append(body)
            self.release = {**self.release, **body}
            return httpx.Response(200, json=self.release)
        if request.method == "POST" and path.endswith(
            f"/releases/{RELEASE_ID}/assets"
        ):
            name = request.url.params.get("name")
            data = request.content
            asset = {"name": name, "size": len(data)}
            if self.forge == "github":
                asset["digest"] = f"sha256:{_sha256(data)}"
            else:
                asset["digest"] = None
            self.uploaded.append(asset)
            self.release["assets"] = [*self.release["assets"], asset]
            return httpx.Response(201, json=asset)
        if request.method == "POST" and path.endswith("/releases"):
            return httpx.Response(409, json={"message": "exists"})
        return httpx.Response(
            500, json={"message": f"unexpected {request.method} {path}"}
        )

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.calls if m != "GET"]

    def creates(self) -> int:
        return sum(1 for m, p in self.calls if m == "POST" and p.endswith("/releases"))

    def uploaded_names(self) -> list[str]:
        return [asset["name"] for asset in self.uploaded]


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


def _artifacts(tmp_path) -> None:
    art = tmp_path / "dist"
    art.mkdir(exist_ok=True)
    for name in FILES:
        (art / name).write_bytes(CONTENT[name])


async def _publish(dest: FakeDestination, tmp_path, *, release_body: str | None = BODY):
    _artifacts(tmp_path)
    audit: list[str] = []
    kwargs = {"release_body": release_body} if release_body is not None else {}
    report = await ReleaseHandler.publish_release(
        _config(dest.forge),
        REPO,
        TAG,
        NOTES,
        tmp_path / "dist",
        name_template=TEMPLATE,
        adapters=[_make(dest)],
        skip_existing=True,
        audit=audit,
        **kwargs,
    )
    return report, audit


FORGES = [("forgejo", _forgejo), ("github", _github)]


# --- criterion 3: a release created without assets is filled -------------


@pytest.mark.parametrize("forge,_make_adapter", FORGES)
@pytest.mark.asyncio
async def test_attach_run_fills_a_release_created_without_assets(
    forge, _make_adapter, tmp_path
):
    """RED PROOF: the release exists with an empty body and no assets; the
    attach run converges it with all four files, exactly once each. At
    2f5c255 the empty-body branch patches only the body, so no file is
    attached and this fails."""
    dest = FakeDestination(_release(), forge=forge)
    report, audit = await _publish(dest, tmp_path)

    label = f"{forge}:{REPO}"
    assert sorted(dest.uploaded_names()) == sorted(FILES)
    assert Counter(dest.uploaded_names()) == Counter({name: 1 for name in FILES})
    assert dest.creates() == 0
    assert not any(m == "DELETE" for m, _ in dest.calls)
    assert report.supplemented == [label]
    assert report.failed == [] and report.partial is False
    assert len(audit) == 1 and "result=supplemented" in audit[0]
    for name in FILES:
        assert name in audit[0]


# --- criterion 1: attaching twice is one asset per name, no failure -------


@pytest.mark.parametrize("forge,_make_adapter", FORGES)
@pytest.mark.asyncio
async def test_attaching_twice_leaves_one_asset_per_name(
    forge, _make_adapter, tmp_path
):
    """Criterion 1: the second attach run sees each name with the recorded
    digest and writes nothing; one asset per name remains, no failure."""
    dest = FakeDestination(_release(), forge=forge)
    await _publish(dest, tmp_path)
    first_uploads = list(dest.uploaded)
    first_writes = len(dest.writes())

    report, audit = await _publish(dest, tmp_path)

    label = f"{forge}:{REPO}"
    assert dest.uploaded == first_uploads
    assert len(dest.writes()) == first_writes
    assert Counter(dest.uploaded_names()) == Counter({name: 1 for name in FILES})
    assert report.failed == [] and report.partial is False
    assert report.skipped == [label]
    assert "result=skipped" in audit[0]
    if forge == "github":
        for asset in dest.uploaded:
            assert asset["digest"] == f"sha256:{_sha256(CONTENT[asset['name']])}"


@pytest.mark.parametrize("forge,_make_adapter", FORGES)
@pytest.mark.asyncio
async def test_assets_with_the_same_digest_are_a_noop(forge, _make_adapter, tmp_path):
    """Criterion 1: a complete release whose recorded digests match the build
    is skipped with no write at all."""
    assets = []
    for name in FILES:
        data = CONTENT[name]
        asset = {"name": name, "size": len(data)}
        asset["digest"] = (
            f"sha256:{_sha256(data)}" if forge == "github" else None
        )
        assets.append(asset)
    dest = FakeDestination(_release(body=BODY, assets=assets), forge=forge)

    report, audit = await _publish(dest, tmp_path)

    assert dest.writes() == []
    assert report.skipped == [f"{forge}:{REPO}"]
    assert report.failed == []
    assert "result=skipped" in audit[0]


# --- criterion 2: same name, another digest fails by name, unchanged ------


@pytest.mark.asyncio
async def test_github_same_name_other_digest_fails_by_name_and_changes_nothing(
    tmp_path,
):
    """Criterion 2: GitHub exposes the digest, so a name present with another
    SHA-256 is a named failure and no write touches the existing asset."""
    other = "sha256:" + "0" * 64
    dest = FakeDestination(
        _release(
            body=BODY,
            assets=[
                {"name": FILES[0], "size": len(CONTENT[FILES[0]]), "digest": other}
            ],
        ),
        forge="github",
    )

    report, audit = await _publish(dest, tmp_path)

    label = f"github:{REPO}"
    detail = report.failure_details[report.failed[0]]
    assert report.failed == [label]
    assert report.supplemented == [] and report.skipped == []
    assert FILES[0] in detail
    assert other in detail
    assert f"sha256:{_sha256(CONTENT[FILES[0]])}" in detail
    # Nothing was written: the existing asset and the missing files are kept.
    assert dest.writes() == []
    assert dest.uploaded == []
    assert dest.release["assets"] == [
        {"name": FILES[0], "size": len(CONTENT[FILES[0]]), "digest": other}
    ]
    assert len(audit) == 1 and "result=" in audit[0]


@pytest.mark.asyncio
async def test_forgejo_same_name_other_size_fails_by_name_and_changes_nothing(
    tmp_path,
):
    """Criterion 2 on Forgejo: the forge reports ``digest: null`` (measured),
    so the comparison uses ``size``; a size that differs from the build fails
    the destination by name and writes nothing."""
    dest = FakeDestination(
        _release(
            body=BODY,
            assets=[
                {
                    "name": FILES[0],
                    "size": len(CONTENT[FILES[0]]) + 1,
                    "digest": None,
                }
            ],
        ),
        forge="forgejo",
    )

    report, _ = await _publish(dest, tmp_path)

    label = f"forgejo:{REPO}"
    detail = report.failure_details[report.failed[0]]
    assert report.failed == [label]
    assert FILES[0] in detail and "size" in detail
    assert dest.writes() == [] and dest.uploaded == []


@pytest.mark.asyncio
async def test_digest_conflict_is_named_next_to_a_title_difference(tmp_path):
    """Criterion 2: the judge that reports what differs names the conflicting
    asset beside the title difference, and still writes nothing."""
    other = "sha256:" + "1" * 64
    dest = FakeDestination(
        _release(
            name="Something else v3.5.1",
            body=BODY,
            assets=[
                {"name": FILES[1], "size": len(CONTENT[FILES[1]]), "digest": other}
            ],
        ),
        forge="github",
    )

    report, _ = await _publish(dest, tmp_path)

    detail = report.failure_details[report.failed[0]]
    assert report.failed == [f"github:{REPO}"]
    assert "Something else v3.5.1" in detail
    assert FILES[1] in detail
    assert dest.writes() == []


# --- compatibility: an asset that carries no digest cannot be proven ------


@pytest.mark.parametrize("forge,_make_adapter", FORGES)
@pytest.mark.asyncio
async def test_an_asset_with_neither_digest_nor_size_is_treated_as_present(
    forge, _make_adapter, tmp_path
):
    """The bare ``{"name": ...}`` shape every older fixture uses cannot prove
    a difference, and nothing is downloaded to try (REL-010): it is a no-op."""
    dest = FakeDestination(
        _release(body=BODY, assets=[{"name": name} for name in FILES]),
        forge=forge,
    )

    report, _ = await _publish(dest, tmp_path)

    assert dest.writes() == [] and dest.uploaded == []
    assert report.skipped == [f"{forge}:{REPO}"]
    assert report.failed == []
