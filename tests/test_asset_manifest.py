"""REA-016: the release manifest declares producer and destination per piece.

Each declared asset or channel (PyPI, ghcr, Homebrew tap, winget) carries its
producer (``forgejo`` or ``github``) and the destinations that must hold it.
A github-produced piece is pending until the deadline and is not an error; after
the deadline the run is red and the message names the piece, the producer and
the destination.

Criterion 1: a github-produced asset absent within the deadline leaves the
release pending (the report names producer and since when) and is not an error.

Criterion 2: after the deadline the run is red and the message names the asset,
the producer and the destination.

Criterion 3: the deadline default is 60 minutes and a repository override in
``.ops.yaml`` is honoured. RED PROOF: against 2f5c255 a missing github-produced
asset is not reported at all.

The manifest is an additive, optional extension of the REA-010
``release.assets`` declaration: an absent block means every asset is
forgejo-produced and required at once, which is exactly the REA-014 behaviour.
Channels are read read-only, the way capacium's ``verify-channels`` step reads
them: nothing is written and no release object is touched.

The fake destination is an ``httpx`` transport behind the real adapters; every
release test drives ``ReleaseHandler.publish_release`` — the call shape the
shipped workflow uses.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import OpsEngineConfig, OpsYamlError
from ops_engine.modules.release import ReleaseHandler

REPO = "langevc/ops-engine"
TAG = "v3.5.1"
VERSION = "3.5.1"
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

# A github-built binary the Forgejo build never carries; the GitHub workflow
# attaches it to the GitHub release on its own schedule (D4/D5).
GITHUB_BINARY = "ops-engine-tool-3.5.1-linux-amd64.tar.gz"
GITHUB_BYTES = b"github built bytes\n"


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _minutes_ago(minutes: int) -> datetime:
    """A whole-minute instant, so ISO round-trips are exact."""
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).replace(
        second=0, microsecond=0
    )


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# `release.manifest` declarations: a github-produced release-object asset that
# only the GitHub destination must hold.
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

DECLARED = [
    {"type": "github_release_zip", "name": "ops_engine-<version>-py3-none-any.whl"},
    {"type": "github_release_tarball", "name": "ops_engine-<version>.tar.gz"},
    {"type": "github_release_tarball", "name": "SHA256SUMS"},
]


def _config(
    forge: str,
    *,
    second: str | None = None,
    assets=None,
    manifest=None,
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
    release = {"enabled": True}
    if assets is not None:
        release["assets"] = assets
    if manifest is not None:
        release["manifest"] = manifest
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
    ``created_at`` is the release object's own creation time, the clock the
    pending deadline runs from.
    """

    def __init__(self, *, forge: str, hidden=(), created_at=None, create_status=409):
        self.forge = forge
        self.release: dict | None = None
        self.hidden = set(hidden)
        self.created_at = created_at
        self.create_status = create_status
        self.calls: list[tuple[str, str]] = []
        self.uploaded: list[dict] = []

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
            assets = list((self.release or {}).get("assets") or [])
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
                "created_at": self.created_at,
                "assets": [],
            }
            return httpx.Response(201, json=self.release)
        return httpx.Response(
            500, json={"message": f"unexpected {request.method} {path}"}
        )

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.calls if m != "GET"]


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


async def _publish(
    destinations, tmp_path, *, config, repo_dir=None, extra=None, channel_reader=None
):
    _artifacts(tmp_path, extra)
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
        channel_reader=channel_reader,
    )
    return report, audit


def _pending_lines(audit):
    return [line for line in audit if "result=pending" in line]


def _failed_lines(audit):
    return [line for line in audit if "result=manifest-failed" in line]


# --- Criterion 1: absent within the deadline -> pending, not an error ------


@pytest.mark.parametrize("forge", ["forgejo", "github"])
@pytest.mark.asyncio
async def test_a_github_produced_asset_absent_within_the_deadline_is_pending(
    forge, tmp_path
):
    """Criterion 1: the github-built binary is not in this build and not on
    the destination yet. Within the 60-minute deadline the release is pending,
    the report names the producer and since when, and nothing failed."""
    since = _minutes_ago(5)
    dest = FakeDestination(forge=forge, created_at=_iso(since))
    report, audit = await _publish(
        [dest], tmp_path, config=_config(forge, manifest=MANIFEST)
    )

    label = f"github:{REPO}"
    assert report.failed == [], "a github-produced piece within the deadline is not an error"
    assert report.partial is False
    assert report.pending[label] == [
        f"{GITHUB_BINARY} producer=github since={since.isoformat()}"
    ]
    lines = _pending_lines(audit)
    assert len(lines) == 1
    assert GITHUB_BINARY in lines[0]
    assert "producer=github" in lines[0]
    assert f"since={since.isoformat()}" in lines[0]
    assert label in lines[0]
    assert not _failed_lines(audit)


@pytest.mark.asyncio
async def test_a_github_produced_asset_that_arrived_is_verified_not_pending(
    tmp_path,
):
    """Criterion 1, the terminal case: once the piece is on the destination it
    is verified with its SHA-256 like any other asset, and nothing is pending."""
    dest = FakeDestination(forge="github", created_at=_iso(_minutes_ago(5)))
    report, audit = await _publish(
        [dest],
        tmp_path,
        config=_config("github", manifest=MANIFEST),
        extra={GITHUB_BINARY: GITHUB_BYTES},
    )

    label = f"github:{REPO}"
    assert report.failed == [] and report.pending == {}
    assert report.verified[label] == [f"{GITHUB_BINARY} {_sha256(GITHUB_BYTES)}"]
    verified = [line for line in audit if "result=verified" in line]
    assert len(verified) == 1
    assert f"{GITHUB_BINARY} {_sha256(GITHUB_BYTES)}" in verified[0]


# --- Criterion 2: after the deadline -> red, naming piece, producer, dest --


@pytest.mark.asyncio
async def test_a_github_produced_asset_absent_after_the_deadline_is_red(tmp_path):
    """Criterion 2: 90 minutes after the release was created the default
    60-minute deadline has passed; the run is red and the message names the
    asset, the producer and the destination."""
    since = _minutes_ago(90)
    dest = FakeDestination(forge="github", created_at=_iso(since))
    report, audit = await _publish(
        [dest], tmp_path, config=_config("github", manifest=MANIFEST)
    )

    label = f"github:{REPO}"
    assert report.failed == [label]
    assert report.partial is True
    assert report.pending == {}
    detail = report.failure_details[label]
    assert GITHUB_BINARY in detail
    assert "producer=github" in detail
    assert label in detail
    lines = _failed_lines(audit)
    assert len(lines) == 1
    assert GITHUB_BINARY in lines[0] and "producer=github" in lines[0]
    assert label in lines[0]


# --- Criterion 3: the deadline default and the repository override ----------


def test_the_default_deadline_is_sixty_minutes():
    """Criterion 3: with no override the manifest's deadline is 60 minutes."""
    config = _config("github", manifest=MANIFEST)
    manifest = config.get_repo_config("langevc", "ops-engine").release.manifest
    assert manifest.deadline_minutes == 60


@pytest.mark.asyncio
async def test_a_repository_override_is_honoured(tmp_path):
    """Criterion 3: a `.ops.yaml` that sets a shorter deadline turns the same
    age red that the default would leave pending, and a longer one keeps it
    pending."""
    since = _minutes_ago(20)
    body = (
        "release:\n"
        "  manifest:\n"
        "    deadline_minutes: {deadline}\n"
        "    assets:\n"
        "      - type: github_release_tarball\n"
        "        name: 'ops-engine-tool-<version>-linux-amd64.tar.gz'\n"
        "        producer: github\n"
        "        destinations: ['github:langevc/ops-engine']\n"
    )
    (tmp_path / ".ops.yaml").write_text(body.format(deadline=10), encoding="utf-8")
    short = FakeDestination(forge="github", created_at=_iso(since))
    report, _ = await _publish(
        [short], tmp_path, config=_config("github"), repo_dir=tmp_path
    )
    assert report.failed == [f"github:{REPO}"]

    (tmp_path / ".ops.yaml").write_text(body.format(deadline=120), encoding="utf-8")
    long = FakeDestination(forge="github", created_at=_iso(since))
    report, _ = await _publish(
        [long], tmp_path, config=_config("github"), repo_dir=tmp_path
    )
    assert report.failed == []
    assert f"github:{REPO}" in report.pending


@pytest.mark.asyncio
async def test_the_default_deadline_leaves_the_same_age_pending(tmp_path):
    """Criterion 3: 20 minutes old is within the 60-minute default."""
    dest = FakeDestination(forge="github", created_at=_iso(_minutes_ago(20)))
    report, _ = await _publish(
        [dest], tmp_path, config=_config("github", manifest=MANIFEST)
    )
    assert report.failed == []
    assert f"github:{REPO}" in report.pending


# --- Criterion 3: the RED proof --------------------------------------------


@pytest.mark.asyncio
async def test_red_proof_a_missing_github_produced_asset_is_not_reported_at_the_base(
    tmp_path,
):
    """Criterion 3 RED PROOF: the GitHub build attaches a binary this Forgejo
    build never carries. At 2f5c255 the ``manifest`` key is dropped (nothing
    reads it) and the run reports no pending piece and no failure; with the
    change the release is pending and names the piece."""
    since = _minutes_ago(5)
    dest = FakeDestination(forge="github", created_at=_iso(since))
    report, _ = await _publish(
        [dest], tmp_path, config=_config("github", manifest=MANIFEST)
    )

    reported = getattr(report, "pending", None)
    assert reported is not None, "the manifest is not read at all: nothing can be pending"
    assert reported.get(f"github:{REPO}"), (
        "a missing github-produced asset must be reported as pending"
    )


# --- Backward compatibility: no manifest, and forgejo-produced pieces ------


@pytest.mark.asyncio
async def test_an_absent_manifest_keeps_the_release_014_behaviour(tmp_path):
    """An absent manifest block means every asset is forgejo-produced and
    required at once: a missing declared file still fails immediately and no
    pending state appears."""
    dest = FakeDestination(forge="forgejo", hidden={"SHA256SUMS"})
    report, audit = await _publish(
        [dest], tmp_path, config=_config("forgejo", assets=DECLARED)
    )

    label = f"forgejo:{REPO}"
    assert report.failed == [label]
    assert report.pending == {}
    assert "SHA256SUMS" in report.failure_details[label]
    assert [line for line in audit if "result=verify-failed" in line]


@pytest.mark.asyncio
async def test_a_forgejo_produced_manifest_asset_is_required_at_once(tmp_path):
    """A forgejo-produced piece carries no grace: absent from the build and
    the destination, it fails the run immediately, by name."""
    manifest = {
        "assets": [
            {
                "type": "github_release_tarball",
                "name": "never-built-<version>.tar.gz",
                "producer": "forgejo",
            }
        ]
    }
    dest = FakeDestination(forge="forgejo", created_at=_iso(_minutes_ago(5)))
    report, _ = await _publish(
        [dest], tmp_path, config=_config("forgejo", manifest=manifest)
    )

    label = f"forgejo:{REPO}"
    assert report.failed == [label]
    assert report.pending == {}
    assert "never-built-3.5.1.tar.gz" in report.failure_details[label]


# --- Producer and destination: a piece is checked only where it must be ----


@pytest.mark.asyncio
async def test_a_github_destination_only_piece_does_not_fail_forgejo(tmp_path):
    """D4: a GitHub-built binary stays on GitHub. The manifest names only the
    github destination, so the forgejo destination is not asked for it."""
    forgejo_dest = FakeDestination(forge="forgejo", created_at=_iso(_minutes_ago(90)))
    github_dest = FakeDestination(forge="github", created_at=_iso(_minutes_ago(90)))
    report, _ = await _publish(
        [forgejo_dest, github_dest],
        tmp_path,
        config=_config("forgejo", second="github", manifest=MANIFEST),
    )

    assert report.failed == [f"github:{REPO}"]
    assert f"forgejo:{REPO}" in report.published


@pytest.mark.asyncio
async def test_an_unnamed_destination_defaults_to_the_producers_own_forge(
    tmp_path,
):
    """An entry without ``destinations`` is expected where its producer
    publishes: a github-produced piece on the github destination only (D4)."""
    manifest = {
        "assets": [
            {
                "type": "github_release_tarball",
                "name": "ops-engine-tool-<version>-linux-amd64.tar.gz",
                "producer": "github",
            }
        ]
    }
    forgejo_dest = FakeDestination(forge="forgejo", created_at=_iso(_minutes_ago(90)))
    github_dest = FakeDestination(forge="github", created_at=_iso(_minutes_ago(90)))
    report, _ = await _publish(
        [forgejo_dest, github_dest],
        tmp_path,
        config=_config("forgejo", second="github", manifest=manifest),
    )

    assert report.failed == [f"github:{REPO}"]


# --- The manifest is declared in the release block and validated -----------


def test_a_malformed_manifest_is_refused_naming_the_file_and_key(tmp_path):
    """An unknown producer is a refusal, never a silently ignored key, and the
    message names the `.ops.yaml` file and the manifest key."""
    (tmp_path / ".ops.yaml").write_text(
        "release:\n"
        "  manifest:\n"
        "    assets:\n"
        "      - type: github_release_tarball\n"
        "        name: 'tool.tar.gz'\n"
        "        producer: gitlab\n",
        encoding="utf-8",
    )
    from ops_engine.config_loader import load_ops_yaml

    with pytest.raises(OpsYamlError) as exc:
        load_ops_yaml(tmp_path)
    message = str(exc.value)
    assert ".ops.yaml" in message
    assert "release.manifest" in message


def test_a_ghcr_image_is_a_channel_not_a_release_object_asset():
    """REA-014 owns the release-object read-back; the channels (PyPI, ghcr,
    Homebrew tap, winget) are declared under ``manifest.channels``. Declaring
    a ghcr image as a release asset is refused and names the channel form."""
    with pytest.raises(Exception) as exc:
        _config(
            "forgejo",
            manifest={
                "assets": [
                    {"type": "ghcr_image", "name": "langevc/engine", "producer": "forgejo"}
                ]
            },
        )
    assert "release.manifest" in str(exc.value)
    assert "channels" in str(exc.value)


# --- Channels are read read-only -------------------------------------------


class _FakeChannelReader:
    """A read-only channel reader: it can only answer presence."""

    def __init__(self, present: set[str], *, manual: bool = False):
        self.present = present
        self.manual = manual
        self.reads: list[tuple[str, str, str]] = []

    async def present_at(self, channel: str, name: str, version: str):
        self.reads.append((channel, name, version))
        if self.manual and channel == "winget":
            return None
        return f"{channel}:{name}" in self.present


def _manifest_object(config):
    return config.get_repo_config("langevc", "ops-engine").release.manifest


@pytest.mark.asyncio
async def test_a_channel_absent_within_the_deadline_is_pending():
    """Criterion 1 for a channel: the PyPI package a github workflow publishes
    is not there yet; within the deadline it is pending, naming the producer
    and since when, and is not an error."""
    from ops_engine.modules import asset_manifest

    config = _config(
        "forgejo",
        manifest={
            "channels": [
                {"channel": "pypi", "name": "ops-engine", "producer": "github"}
            ]
        },
    )
    since = _minutes_ago(5)
    reader = _FakeChannelReader(set())
    verdict = await asset_manifest.evaluate_channels(
        _manifest_object(config), TAG, reader=reader, since=since
    )

    assert verdict.failed == []
    assert [piece.as_text() for piece in verdict.pending] == [
        f"pypi:ops-engine producer=github since={since.isoformat()}"
    ]
    assert reader.reads == [("pypi", "ops-engine", VERSION)]


@pytest.mark.asyncio
async def test_a_channel_absent_after_the_deadline_is_red_naming_producer():
    """Criterion 2 for a channel: past the deadline the channel is red and the
    piece, its producer and the channel are named."""
    from ops_engine.modules import asset_manifest

    config = _config(
        "forgejo",
        manifest={
            "channels": [
                {"channel": "homebrew", "name": "langevc/homebrew-tap", "producer": "github"}
            ]
        },
    )
    reader = _FakeChannelReader(set())
    verdict = await asset_manifest.evaluate_channels(
        _manifest_object(config), TAG, reader=reader, since=_minutes_ago(90)
    )

    assert verdict.pending == []
    assert len(verdict.failed) == 1
    text = verdict.failed[0].as_text()
    assert "langevc/homebrew-tap" in text
    assert "producer=github" in text
    assert "channel:homebrew" in text


@pytest.mark.asyncio
async def test_a_forgejo_produced_channel_is_required_at_once():
    """Red proof for the producer rule on the channel path: a forgejo-produced
    PyPI package carries no grace. Absent, it is red even inside the deadline,
    while the same absence a github produces stays pending."""
    from ops_engine.modules import asset_manifest

    config = _config(
        "forgejo",
        manifest={
            "channels": [
                {"channel": "pypi", "name": "ops-engine", "producer": "forgejo"}
            ]
        },
    )
    verdict = await asset_manifest.evaluate_channels(
        _manifest_object(config),
        TAG,
        reader=_FakeChannelReader(set()),
        since=_minutes_ago(5),
    )

    assert verdict.pending == []
    assert len(verdict.failed) == 1
    assert "pypi:ops-engine" in verdict.failed[0].as_text()


@pytest.mark.asyncio
async def test_a_present_channel_is_satisfied_and_winget_is_manual():
    """A channel the reader proves present is satisfied; winget has no
    read-only probe (capacium records it as a manual ``:grey_question:``), so
    an unknown answer is never a failure."""
    from ops_engine.modules import asset_manifest

    config = _config(
        "forgejo",
        manifest={
            "channels": [
                {"channel": "pypi", "name": "ops-engine", "producer": "forgejo"},
                {"channel": "winget", "name": "OpsEngine.OpsEngine", "producer": "github"},
            ]
        },
    )
    reader = _FakeChannelReader({"pypi:ops-engine"}, manual=True)
    verdict = await asset_manifest.evaluate_channels(
        _manifest_object(config), TAG, reader=reader, since=_minutes_ago(90)
    )

    assert verdict.failed == [] and verdict.pending == []
    assert verdict.satisfied == ["pypi:ops-engine"]
    assert verdict.manual == ["winget:OpsEngine.OpsEngine"]


@pytest.mark.asyncio
async def test_the_default_channel_reader_only_issues_reads():
    """The shipped reader is read-only: PyPI and ghcr are probed with GET only
    and no write method exists on it."""
    from ops_engine.modules import asset_manifest

    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/pypi/ops-engine/json":
            return httpx.Response(200, json={"info": {"version": VERSION}})
        if request.url.path == "/token":
            return httpx.Response(200, json={"token": "anon"})
        if "/manifests/" in request.url.path:
            return httpx.Response(200, json={"schemaVersion": 2})
        return httpx.Response(404, json={"message": "not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reader = asset_manifest.HttpxChannelReader(client=client)
    assert await reader.present_at("pypi", "ops-engine", VERSION) is True
    assert await reader.present_at("ghcr", "langevc/engine", VERSION) is True
    assert seen and all(method == "GET" for method, _ in seen)


# --- Regression: an unread destination is never red --------------------------


@pytest.mark.asyncio
async def test_an_unvisited_destination_is_pending_not_red_from_anothers_clock(
    tmp_path,
):
    """A manifest destination this run never reads has no evidence against it.
    The run's own release is 90 minutes old (past the deadline), but that
    clock belongs to a different destination: the unvisited github destination
    is pending and the destination that was read is still published."""
    forgejo_dest = FakeDestination(forge="forgejo", created_at=_iso(_minutes_ago(90)))
    report, audit = await _publish(
        [forgejo_dest], tmp_path, config=_config("forgejo", manifest=MANIFEST)
    )

    label = f"github:{REPO}"
    assert report.failed == []
    assert report.partial is False
    assert label in report.pending
    assert f"forgejo:{REPO}" in report.published
    assert not _failed_lines(audit)


@pytest.mark.asyncio
async def test_a_naive_created_at_is_read_as_utc_and_judged(tmp_path):
    """Forgejo may render a release timestamp without a zone. That must not
    crash the deadline (naive minus aware), and the age still decides the
    piece: 90 minutes naive-UTC is red, 5 minutes is pending."""
    old = _minutes_ago(90).replace(tzinfo=None).isoformat()
    dest = FakeDestination(forge="github", created_at=old)
    report, _ = await _publish(
        [dest], tmp_path, config=_config("github", manifest=MANIFEST)
    )
    assert report.failed == [f"github:{REPO}"]

    fresh = _minutes_ago(5).replace(tzinfo=None).isoformat()
    dest = FakeDestination(forge="github", created_at=fresh)
    report, _ = await _publish(
        [dest], tmp_path, config=_config("github", manifest=MANIFEST)
    )
    assert report.failed == []
    assert report.pending[f"github:{REPO}"][0].startswith(GITHUB_BINARY)


# --- Regression: declared channels are judged in the release path ------------


@pytest.mark.asyncio
async def test_declared_channels_are_judged_in_the_release_path(tmp_path):
    """`manifest.channels` is not bookkeeping: the shipped release path judges
    it read-only. A present channel is satisfied; an unprobeable winget is
    manual, never a failure."""
    manifest = {
        "channels": [
            {"channel": "pypi", "name": "ops-engine", "producer": "github"},
            {"channel": "winget", "name": "OpsEngine.OpsEngine", "producer": "github"},
        ]
    }
    dest = FakeDestination(forge="forgejo", created_at=_iso(_minutes_ago(5)))
    reader = _FakeChannelReader({"pypi:ops-engine"}, manual=True)
    report, audit = await _publish(
        [dest],
        tmp_path,
        config=_config("forgejo", manifest=manifest),
        channel_reader=reader,
    )

    assert ("pypi", "ops-engine", VERSION) in reader.reads
    assert report.failed == []
    assert report.failed == [] and report.partial is False
    assert [line for line in audit if "result=channel-satisfied" in line]
    assert [line for line in audit if "result=channel-manual" in line]


@pytest.mark.asyncio
async def test_a_failed_channel_turns_the_release_red_naming_producer(tmp_path):
    """A github-produced channel absent after the deadline is red in the same
    report, and the detail names the channel, the producer and the channel
    form."""
    manifest = {
        "channels": [
            {"channel": "homebrew", "name": "langevc/homebrew-tap", "producer": "github"}
        ]
    }
    dest = FakeDestination(forge="forgejo", created_at=_iso(_minutes_ago(90)))
    report, audit = await _publish(
        [dest],
        tmp_path,
        config=_config("forgejo", manifest=manifest),
        channel_reader=_FakeChannelReader(set()),
    )

    label = "homebrew:langevc/homebrew-tap"
    assert label in report.failed
    detail = report.failure_details[label]
    assert "producer=github" in detail and "channel:homebrew" in detail
    assert [line for line in audit if f"dest={label}" in line
            and "result=manifest-failed" in line]
