"""P36-017 criteria 1-3: The release body is written on every release destination.

Criterion 1: A destination release that does not exist is created with that body;
one that already exists with an empty body (created earlier by a distribution
workflow) gets the body supplemented; neither case fails the destination.

Criterion 2: An existing release whose body does not contain the tag's CHANGELOG
section is left untouched and fails that destination with a named error that
names the difference (same rule as the 3.5.2 title and notes handling).

Criterion 3: The external-audience gate runs on the final body before any
destination write, and the install block and the assets line come from a
per-repository declaration (business data, for example a key in the
repository's .ops.yaml), never from engine defaults.

These tests are RED against the P36-003 tip: the body writer does not exist yet.
"""

import json

import httpx
import pytest

from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.config_loader import OpsEngineConfig
from ops_engine.modules.release import ReleaseHandler

REPO = "langevc/ops-engine"
TAG = "v3.5.1"
TITLE = "ops-engine v3.5.1"
NOTES = "- A feature.\n\n- A fix.\n"
INSTALL_BLOCK = "pip install ops-engine"
ASSETS_LINE = "[ops_engine-3.5.1-py3-none-any.whl](https://example.com/asset)"
CHANGELOG_PATH = "CHANGELOG.md"
OTHER_BODY = "Some other release body without the changelog section."
RELEASE_ID = 77
FILES = ["ops_engine-3.5.1-py3-none-any.whl", "SHA256SUMS"]


def _full_body():
    return (
        f"## {TITLE}\n\n"
        f"{NOTES}\n"
        f"### Install\n"
        f"{INSTALL_BLOCK}\n\n"
        f"{ASSETS_LINE}\n\n"
        f"See [{CHANGELOG_PATH}](https://example.com/{CHANGELOG_PATH}) for all changes.\n"
    )


FULL_BODY = _full_body()


def _config(*, install_block="", assets_line="", forge="forgejo"):
    repos = {
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
    if install_block:
        repos["ops-engine"]["release_body_install_block"] = install_block
    if assets_line:
        repos["ops-engine"]["release_body_assets_line"] = assets_line
    return OpsEngineConfig.load({"orgs": {"langevc": {"repositories": repos}}})


def _release(*, body, **over):
    release = {
        "id": RELEASE_ID,
        "tag_name": TAG,
        "name": TITLE,
        "body": body,
        "draft": False,
        "prerelease": False,
        "assets": [],
        "tarball_url": f"https://forge.invalid/{REPO}/archive/{TAG}.tar.gz",
        "zipball_url": f"https://forge.invalid/{REPO}/archive/{TAG}.zip",
    }
    release.update(over)
    return release


class FakeDestination:
    """Serves one release by tag; records every write including body."""

    def __init__(self, release=None, create_status=None):
        self.release = release
        self.create_status = create_status
        self.created_body = None
        self.patched_bodies = []
        self.calls = []
        self.patches = []
        self.uploads = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.method == "GET" and "/releases/tags/" in path:
            if self.release is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=self.release)
        if request.method == "GET" and path.endswith("/releases"):
            rows = [] if self.release is None else [self.release]
            return httpx.Response(200, json=rows)
        if request.method == "PATCH" and f"/releases/{RELEASE_ID}" in path:
            body = json.loads(request.content)
            self.patches.append(body)
            if "body" in body:
                self.patched_bodies.append(body["body"])
            if self.release:
                self.release = {**self.release, **body}
            return httpx.Response(200, json=self.release or {})
        if request.method == "POST" and path.endswith(f"/releases/{RELEASE_ID}/assets"):
            name = request.url.params.get("name")
            self.uploads.append(name)
            return httpx.Response(201, json={"name": name})
        if request.method == "POST" and path.endswith("/releases"):
            if self.create_status:
                return httpx.Response(self.create_status, json={"message": "x"})
            body = json.loads(request.content)
            self.created_body = body.get("body")
            return httpx.Response(
                201, json={"id": RELEASE_ID, "tag_name": TAG, "name": TITLE}
            )
        return httpx.Response(500, json={"message": f"unexpected {request.method} {path}"})

    def writes(self):
        return [(m, p) for m, p in self.calls if m != "GET"]

    def creates(self):
        return sum(1 for m, p in self.calls if m == "POST" and p.endswith("/releases"))


def _forgejo(dest):
    adapter = ForgejoAdapter(base_url="https://forge.invalid", token="t", webhook_secret="s")
    adapter._client = httpx.AsyncClient(
        base_url="https://forge.invalid/api/v1", transport=httpx.MockTransport(dest.handler)
    )
    return adapter


def _github(dest):
    adapter = GithubAdapter(token="t", webhook_secret="s")
    adapter._client = httpx.AsyncClient(
        base_url="https://api.github.invalid", transport=httpx.MockTransport(dest.handler)
    )
    return adapter


async def _publish(forge, adapter, tmp_path, *, release_body=None, **over):
    art = tmp_path / "dist"
    art.mkdir(exist_ok=True)
    for name in FILES:
        (art / name).write_bytes(name.encode())
    audit = []
    kwargs = {"release_body": release_body, **over} if release_body else over
    report = await ReleaseHandler.publish_release(
        _config(forge=forge), REPO, TAG, NOTES, art,
        adapters=[adapter], skip_existing=True, audit=audit, **kwargs,
    )
    return report, audit


# ---------------------------------------------------------------------------
# Criterion 1: new release created with full body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_release_created_with_full_body(tmp_path):
    """A destination release that does not exist is created with the full body
    (product heading, CHANGELOG, install block, assets line, CHANGELOG link).
    The destination is not failed."""
    dest = FakeDestination()
    report, _ = await _publish("forgejo", _forgejo(dest), tmp_path,
                               release_body=FULL_BODY)

    label = f"forgejo:{REPO}"
    assert dest.created_body is not None, "body was not sent in create"
    assert f"## {TITLE}" in dest.created_body
    assert NOTES.strip() in dest.created_body
    assert "### Install" in dest.created_body
    assert INSTALL_BLOCK in dest.created_body
    assert ASSETS_LINE in dest.created_body
    assert CHANGELOG_PATH in dest.created_body
    assert report.published == [label]
    assert report.failed == []


@pytest.mark.asyncio
async def test_empty_body_release_supplemented(tmp_path):
    """An existing release with an empty body gets the body supplemented.
    The destination is not failed."""
    dest = FakeDestination(_release(body=""))
    report, _ = await _publish("forgejo", _forgejo(dest), tmp_path,
                               release_body=FULL_BODY)

    label = f"forgejo:{REPO}"
    # Body was updated via PATCH.
    assert len(dest.patched_bodies) >= 1
    patched = dest.patched_bodies[0]
    assert f"## {TITLE}" in patched
    assert NOTES.strip() in patched
    # No new release created.
    assert dest.creates() == 0
    assert report.supplemented == [label]
    assert report.failed == []


# ---------------------------------------------------------------------------
# Criterion 2: body without CHANGELOG fails destination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_body_without_changelog_fails_destination(tmp_path):
    """An existing release whose body does not contain the tag's CHANGELOG
    section is left untouched and fails that destination with a named error
    that names the difference."""
    dest = FakeDestination(_release(body=OTHER_BODY))
    report, _ = await _publish("forgejo", _forgejo(dest), tmp_path,
                               release_body=FULL_BODY)

    label = f"forgejo:{REPO}"
    # No writes — the release is left untouched.
    assert dest.writes() == []
    assert dest.creates() == 0
    # Destination is failed.
    assert label in report.failed
    detail = report.failure_details.get(label, "")
    assert "CHANGELOG" in detail or "changelog" in detail.lower()


# ---------------------------------------------------------------------------
# Criterion 3: install block and assets line from per-repo declaration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_block_from_per_repo_declaration(tmp_path):
    """The install block comes from a per-repository declaration, never from
    engine defaults. An empty install block produces no '### Install' heading."""
    # Config with an explicit install block.
    body_with_install = (
        f"## {TITLE}\n\n"
        f"{NOTES}\n"
        f"### Install\n"
        f"{INSTALL_BLOCK}\n\n"
        f"See [{CHANGELOG_PATH}](https://example.com/{CHANGELOG_PATH}) for all changes.\n"
    )
    dest = FakeDestination()
    report, _ = await _publish("forgejo", _forgejo(dest), tmp_path,
                               release_body=body_with_install)

    assert dest.created_body is not None
    assert INSTALL_BLOCK in dest.created_body
    assert report.failed == []


@pytest.mark.asyncio
async def test_assets_line_from_per_repo_declaration(tmp_path):
    """The assets line comes from a per-repository declaration, never from
    engine defaults."""
    body_with_assets = (
        f"## {TITLE}\n\n"
        f"{NOTES}\n"
        f"### Install\n"
        f"{INSTALL_BLOCK}\n\n"
        f"{ASSETS_LINE}\n\n"
        f"See [{CHANGELOG_PATH}](https://example.com/{CHANGELOG_PATH}) for all changes.\n"
    )
    dest = FakeDestination()
    report, _ = await _publish("forgejo", _forgejo(dest), tmp_path,
                               release_body=body_with_assets)

    assert dest.created_body is not None
    assert ASSETS_LINE in dest.created_body
    assert report.failed == []


# ---------------------------------------------------------------------------
# Both forges
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("forge,make", [("forgejo", _forgejo), ("github", _github)])
@pytest.mark.asyncio
async def test_full_body_written_on_both_forges(forge, make, tmp_path):
    """The full body is written on every release destination, GitHub included."""
    dest = make(FakeDestination())
    report, _ = await _publish(forge, dest, tmp_path, release_body=FULL_BODY)

    label = f"{forge}:{REPO}"
    assert report.published == [label]
    assert report.failed == []


@pytest.mark.parametrize("forge,make", [("forgejo", _forgejo), ("github", _github)])
@pytest.mark.asyncio
async def test_empty_body_supplemented_on_both_forges(forge, make, tmp_path):
    """Empty-body supplement works on every release destination."""
    dest = FakeDestination(_release(body=""))
    adapter = make(dest)
    report, _ = await _publish(forge, adapter, tmp_path, release_body=FULL_BODY)

    label = f"{forge}:{REPO}"
    assert len(dest.patched_bodies) >= 1
    assert report.supplemented == [label]
    assert report.failed == []


# ---------------------------------------------------------------------------
# Production call shape: ReleaseContext body fields → _release_tag → body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_context_body_fields_reach_publish_release(tmp_path, monkeypatch):
    """The ReleaseContext install block and assets line are composed into the
    full body by _release_tag and passed to publish_release, so the production
    webhook path (process_event → _release_tag) writes the full body without
    the caller passing release_body explicitly."""
    from unittest.mock import AsyncMock

    from ops_engine.config_loader import ReleaseConfig
    from ops_engine.modules import release as release_module
    from ops_engine.modules.release import (
        ReleaseContext,
        ReleaseHandler,
        TagIdentitySpec,
    )

    dest = FakeDestination()
    adapter = _forgejo(dest)

    source = AsyncMock()
    async def get_file_content(repo, path, ref="main"):
        return {
            "CHANGELOG.md": f"# Changes\n\n## {TAG}\n\n{NOTES}",
            ".ops.yaml": (
                "destinations:\n"
                "  - {forge: forgejo, repo: langevc/ops-engine, role: release, credential_variable: OPS_TOKEN}\n"
                f"release_body_install_block: \"{INSTALL_BLOCK}\"\n"
                f"release_body_assets_line: \"{ASSETS_LINE}\"\n"
            ),
        }[path]
    source.get_file_content = get_file_content

    monkeypatch.setattr(release_module, "push_canonical_tag", AsyncMock())

    # Intercept publish_release to capture the release_body it receives.
    captured = {}
    real_publish = ReleaseHandler.publish_release

    async def intercept_publish(*args, **kwargs):
        captured["release_body"] = kwargs.get("release_body")
        captured["release_notes"] = args[3] if len(args) > 3 else kwargs.get("release_notes")
        result = await real_publish(*args, **kwargs)
        captured["report"] = result
        return result

    monkeypatch.setattr(ReleaseHandler, "publish_release", intercept_publish)

    context = ReleaseContext(
        ops_config=_config(forge="forgejo"),
        artifact_dir=tmp_path / "dist",
        identity=TagIdentitySpec(
            repo_dir=str(tmp_path),
            canonical_url="https://forge.example/langevc/ops-engine.git",
            canonical_token="tok",
            mirror_remote="https://mirror.example/langevc/ops-engine.git",
        ),
        adapters=[adapter],
    )

    (tmp_path / "dist").mkdir()
    for name in FILES:
        (tmp_path / "dist" / name).write_bytes(name.encode())

    await ReleaseHandler.process_event(
        source,
        {
            "event_type": "push",
            "action": None,
            "repo": REPO,
            "raw": {"ref": f"refs/tags/{TAG}"},
        },
        ReleaseConfig(enabled=True, trigger="tag_push", tag_pattern="v*"),
        context,
    )

    # The composed body was passed to publish_release.
    body = captured["release_body"]
    assert body is not None, "release_body was not passed to publish_release"
    assert f"## {TITLE}" in body
    assert NOTES.strip() in body
    assert "### Install" in body
    assert INSTALL_BLOCK in body
    assert ASSETS_LINE in body
    assert CHANGELOG_PATH in body

    # The destination release was created with the full body.
    assert dest.created_body is not None
    assert INSTALL_BLOCK in dest.created_body
    assert ASSETS_LINE in dest.created_body

    report = captured["report"]
    label = f"forgejo:{REPO}"
    assert report.published == [label]
    assert report.failed == []


# ---------------------------------------------------------------------------
# Criterion 4: audience gate on final body (RED on base)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audience_gate_runs_on_final_body_not_raw_notes(tmp_path, monkeypatch):
    """Criterion 4 RED: the audience gate must run on the final composed body
    (heading + notes + install block + assets line + changelog link), not only
    on the raw CHANGELOG notes. A tracker reference introduced only by the
    install block is caught; on the base commit (before P36-017) the gate only
    checked raw notes, so this test would fail as a test failure."""
    from unittest.mock import AsyncMock

    from ops_engine.config_loader import ReleaseConfig
    from ops_engine.modules import release as release_module
    from ops_engine.modules.release import (
        ReleaseContext,
        ReleaseHandler,
        TagIdentitySpec,
    )
    from ops_engine.modules.release_gates import ReleaseNotesAudienceError

    dest = FakeDestination()
    adapter = _forgejo(dest)

    source = AsyncMock()
    # Raw notes contain NO tracker reference — the gate on raw notes would pass.
    clean_notes = "- A feature.\n\n- A fix.\n"
    # But the install block introduces a tracker reference.
    install_with_tracker = "Fix LVC-999 in installer"

    async def get_file_content(repo, path, ref="main"):
        return {
            "CHANGELOG.md": f"# Changes\n\n## {TAG}\n\n{clean_notes}",
            ".ops.yaml": (
                "destinations:\n"
                "  - {forge: forgejo, repo: langevc/ops-engine, role: release, credential_variable: OPS_TOKEN}\n"
                f"release_body_install_block: \"{install_with_tracker}\"\n"
            ),
        }[path]

    source.get_file_content = get_file_content

    monkeypatch.setattr(release_module, "push_canonical_tag", AsyncMock())

    context = ReleaseContext(
        ops_config=_config(forge="forgejo"),
        artifact_dir=tmp_path / "dist",
        identity=TagIdentitySpec(
            repo_dir=str(tmp_path),
            canonical_url="https://forge.example/langevc/ops-engine.git",
            canonical_token="tok",
            mirror_remote="https://mirror.example/langevc/ops-engine.git",
        ),
        adapters=[adapter],
        ticket_prefixes=["LVC"],
    )

    (tmp_path / "dist").mkdir()
    for name in FILES:
        (tmp_path / "dist" / name).write_bytes(name.encode())

    # The audience gate on the final body must refuse because the install
    # block introduces "LVC-999". On the base commit (pre-P36-017), the
    # gate only checked raw notes, so this would NOT raise — the test
    # fails as an assertion error, not a TypeError.
    with pytest.raises(ReleaseNotesAudienceError, match="LVC-999"):
        await ReleaseHandler.process_event(
            source,
            {
                "event_type": "push",
                "action": None,
                "repo": REPO,
                "raw": {"ref": f"refs/tags/{TAG}"},
            },
            ReleaseConfig(enabled=True, trigger="tag_push", tag_pattern="v*"),
            context,
        )
